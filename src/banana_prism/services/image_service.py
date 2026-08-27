"""Qt event-loop based image generation/edit job service."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit

from PySide6.QtCore import QByteArray, QObject, QThread, QTimer, QUrl, Signal
from PySide6.QtNetwork import (
    QHostInfo,
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from banana_prism.constants import (
    MAX_IMAGE_PIXELS,
    MAX_RESPONSE_BYTES,
    REMOTE_IMAGE_TIMEOUT_MS,
    REQUEST_TIMEOUT_MS,
)
from banana_prism.models import JobState
from banana_prism.services.api_client import (
    ApiClient,
    ApiClientError,
    ApiProtocolError,
    ApiRequest,
    ApiResult,
    UnsafeImageUrlError,
    ensure_public_addresses,
    validate_image_bytes,
    validate_remote_image_url_syntax,
)
from banana_prism.services.log_service import redact_sensitive_text


_API_ERROR_PREFIX = "BANANAPRISM_API_ERROR:"
_OPENROUTER_4K_EDIT_TIMEOUT_MS = 900_000


class TerminalState(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    TEXT_ONLY = "text_only"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class StartResult:
    accepted: bool
    job_id: str | None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.accepted


HostLookup = Callable[
    [str, Callable[[Sequence[str] | Exception], None]],
    Any,
]


class ImageService(QObject):
    """Run one cancellable request at a time without blocking the UI thread.

    Every accepted ``job_id`` emits exactly one of ``success``, ``error``,
    ``text_only`` or ``cancelled``.  The service clears its active job before that
    signal is emitted, so queue controllers may safely start the next job directly
    from a terminal signal handler.
    """

    started = Signal(str)
    state_changed = Signal(str, object)  # job_id, JobState
    progress = Signal(str, int, int)  # job_id, received bytes, total (-1 unknown)
    # Detailed transport progress for UIs that want to distinguish the request
    # upload, provider-side generation wait, and response download.  ``progress``
    # intentionally remains unchanged for existing consumers.
    progress_stage = Signal(str, str, int, int)  # job_id, stage, current, total
    success = Signal(str, object)  # job_id, ApiResult
    error = Signal(str, str)
    text_only = Signal(str, str)
    cancelled = Signal(str)
    terminal = Signal(str, object)  # job_id, TerminalState

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        network_manager: QNetworkAccessManager | None = None,
        host_lookup: HostLookup | None = None,
        request_timeout_ms: int = REQUEST_TIMEOUT_MS,
        openrouter_4k_edit_timeout_ms: int = _OPENROUTER_4K_EDIT_TIMEOUT_MS,
        remote_timeout_ms: int = REMOTE_IMAGE_TIMEOUT_MS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_image_pixels: int = MAX_IMAGE_PIXELS,
        max_redirects: int = 5,
    ) -> None:
        super().__init__(parent)
        self._manager = network_manager or QNetworkAccessManager(self)
        self._host_lookup = host_lookup or self._qt_host_lookup
        self._request_timeout_ms = max(1, int(request_timeout_ms))
        self._openrouter_4k_edit_timeout_ms = max(
            self._request_timeout_ms, int(openrouter_4k_edit_timeout_ms)
        )
        self._remote_timeout_ms = max(1, int(remote_timeout_ms))
        self._max_response_bytes = max(1, int(max_response_bytes))
        self._max_image_pixels = max(1, int(max_image_pixels))
        self._max_redirects = max(0, int(max_redirects))

        self._state = JobState.IDLE
        self._job_id: str | None = None
        self._api_request: ApiRequest | None = None
        self._reply: QNetworkReply | Any | None = None
        self._buffer = bytearray()
        self._stage = ""
        self._terminal_emitted = False
        self._parsed_result: ApiResult | None = None
        self._remote_original_url: str | None = None
        self._ssl_error_detail = ""
        self._active_request_timeout_ms = self._request_timeout_ms
        self._has_total_deadline = False
        self._progress_stage = ""
        self._upload_progress_connected = False
        self._last_upload_bytes = 0
        self._last_download_bytes = 0

        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self._on_deadline)

    @property
    def state(self) -> JobState:
        return self._state

    @property
    def current_job_id(self) -> str | None:
        return self._job_id

    @property
    def busy(self) -> bool:
        return self._job_id is not None

    def start(self, request: ApiRequest) -> StartResult:
        self._assert_owning_thread()
        if self._job_id is not None:
            return StartResult(False, self._job_id, "busy")
        if not isinstance(request, ApiRequest):
            raise TypeError("request must be an ApiRequest")

        job_id = uuid.uuid4().hex
        self._job_id = job_id
        self._api_request = request
        self._terminal_emitted = False
        self._parsed_result = None
        self._remote_original_url = None
        self._ssl_error_detail = ""
        self._set_state(JobState.RUNNING)
        if self._job_id != job_id:
            return StartResult(True, job_id)
        self.started.emit(job_id)
        if self._job_id != job_id:
            return StartResult(True, job_id)
        try:
            self._start_api_request(request)
        except Exception as exc:
            self._finish(TerminalState.ERROR, self._safe_error(exc))
        return StartResult(True, job_id)

    def start_generation(self, **kwargs: Any) -> StartResult:
        return self.start(ApiClient.build_generation_request(**kwargs))

    def start_edit(self, **kwargs: Any) -> StartResult:
        return self.start(ApiClient.build_edit_request(**kwargs))

    def cancel(self, job_id: str | None = None) -> bool:
        self._assert_owning_thread()
        if self._job_id is None or (job_id is not None and job_id != self._job_id):
            return False
        self._set_state(JobState.CANCELLING)
        self._finish(TerminalState.CANCELLED)
        return True

    def close(self) -> None:
        """Cancel active work; child Qt objects are then safely deletable."""

        if self._job_id is not None:
            self.cancel(self._job_id)

    def _assert_owning_thread(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("ImageService must be used from its owning Qt thread")

    def _set_state(self, state: JobState) -> None:
        self._state = state
        if self._job_id:
            self.state_changed.emit(self._job_id, state)

    def _make_request(self, url: QUrl | str, timeout_ms: int) -> QNetworkRequest:
        request = QNetworkRequest(QUrl(url) if isinstance(url, str) else url)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
        )
        request.setTransferTimeout(timeout_ms)
        return request

    def _start_api_request(self, spec: ApiRequest) -> None:
        is_long_edit = self._is_openrouter_4k_edit(spec)
        timeout_ms = (
            self._openrouter_4k_edit_timeout_ms
            if is_long_edit
            else self._request_timeout_ms
        )
        self._active_request_timeout_ms = timeout_ms
        self._has_total_deadline = True
        request = self._make_request(spec.url, timeout_ms)
        for name, value in spec.headers.items():
            request.setRawHeader(name.encode("ascii"), value.encode("utf-8"))
        self._stage = "api"
        self._progress_stage = "uploading"
        self._last_upload_bytes = 0
        self._last_download_bytes = 0
        self._buffer.clear()
        body = spec.body
        job_id = self._job_id
        if job_id:
            self.progress_stage.emit(job_id, "uploading", 0, len(body))
            if self._job_id != job_id:
                return
        reply = self._manager.post(request, QByteArray(body))
        self._attach_reply(reply, timeout_ms)
        if self._job_id is not None:
            # Absolute API deadline. Activity refreshes only the separate
            # inactivity timer and can never make a request run forever.
            self._deadline.start(timeout_ms)

    @staticmethod
    def _is_openrouter_4k_edit(spec: ApiRequest) -> bool:
        """Return whether *spec* is the slow OpenRouter Images edit profile.

        ``ApiRequest`` deliberately has no transport-policy fields.  The shape is
        nevertheless unambiguous: 4K uses OpenRouter's Images API and an edit has
        ordered ``input_references``.  Generation requests and every AiHubMix
        request keep the normal five-minute timeout.
        """

        provider = getattr(spec.provider, "value", str(spec.provider)).casefold()
        payload = spec.payload
        references = payload.get("input_references")
        return (
            provider == "openrouter"
            and str(payload.get("resolution", "")).upper() == "4K"
            and isinstance(references, list)
            and bool(references)
        )

    def _attach_reply(self, reply: Any, timeout_ms: int) -> None:
        if self._job_id is None:
            try:
                reply.abort()
            finally:
                reply.deleteLater()
            return
        self._reply = reply
        reply.readyRead.connect(lambda r=reply: self._read_available(r))
        reply.finished.connect(lambda r=reply: self._reply_finished(r))
        if self._stage == "api" and hasattr(reply, "uploadProgress"):
            reply.uploadProgress.connect(
                lambda sent, total, r=reply: self._emit_upload_progress(
                    r, sent, total
                )
            )
            self._upload_progress_connected = True
        if hasattr(reply, "downloadProgress"):
            reply.downloadProgress.connect(
                lambda received, total, r=reply: self._emit_progress(r, received, total)
            )
        if hasattr(reply, "metaDataChanged"):
            reply.metaDataChanged.connect(lambda r=reply: self._metadata_changed(r))
        if hasattr(reply, "sslErrors"):
            reply.sslErrors.connect(
                lambda errors, r=reply: self._remember_ssl_errors(r, errors)
            )
        self._timeout.start(timeout_ms)

    def _touch_transport_timeout(self, reply: Any) -> None:
        """Refresh the inactivity timer when bytes are actually moving."""

        if reply is not self._reply or self._job_id is None:
            return
        timeout_ms = (
            self._remote_timeout_ms
            if self._stage in {"remote", "resolving"}
            else self._active_request_timeout_ms
        )
        self._timeout.start(timeout_ms)

    def _emit_upload_progress(self, reply: Any, sent: int, total: int) -> None:
        if reply is not self._reply or self._job_id is None:
            return
        job_id = self._job_id
        sent_value = int(sent)
        total_value = int(total)
        if sent_value > self._last_upload_bytes:
            self._last_upload_bytes = sent_value
            self._touch_transport_timeout(reply)
        self._progress_stage = "uploading"
        self.progress_stage.emit(
            job_id, "uploading", sent_value, total_value
        )
        if reply is not self._reply or self._job_id != job_id:
            return
        if total_value >= 0 and sent_value >= total_value:
            self._progress_stage = "generating"
            self.progress_stage.emit(job_id, "generating", 0, -1)

    def _remember_ssl_errors(self, reply: Any, errors: Any) -> None:
        if reply is not self._reply or self._job_id is None:
            return
        details: list[str] = []
        for error in list(errors or ())[:3]:
            try:
                details.append(str(error.errorString()))
            except (AttributeError, RuntimeError):
                details.append(str(error))
        self._ssl_error_detail = self._safe_error("; ".join(details))

    def _emit_progress(self, reply: Any, received: int, total: int) -> None:
        if reply is self._reply and self._job_id:
            job_id = self._job_id
            received_value = int(received)
            total_value = int(total)
            if received_value > self._last_download_bytes:
                self._last_download_bytes = received_value
                self._touch_transport_timeout(reply)
            self._progress_stage = "downloading"
            self.progress_stage.emit(
                job_id, "downloading", received_value, total_value
            )
            if reply is self._reply and self._job_id == job_id:
                self.progress.emit(job_id, received_value, total_value)

    def _metadata_changed(self, reply: Any) -> None:
        if reply is not self._reply or self._job_id is None:
            return
        raw_length = bytes(reply.rawHeader(b"Content-Length"))
        if raw_length:
            try:
                length = int(raw_length)
            except ValueError:
                self._finish(TerminalState.ERROR, "Invalid Content-Length header")
                return
            if length < 0 or length > self._max_response_bytes:
                self._finish(
                    TerminalState.ERROR,
                    "Network response exceeds the configured size limit",
                )

    def _read_available(self, reply: Any) -> None:
        if reply is not self._reply or self._job_id is None:
            return
        chunk = bytes(reply.readAll())
        if chunk:
            job_id = self._job_id
            self._touch_transport_timeout(reply)
            next_size = len(self._buffer) + len(chunk)
            self._buffer.extend(chunk)
            self._last_download_bytes = max(self._last_download_bytes, next_size)
            if self._progress_stage != "downloading":
                self._progress_stage = "downloading"
                self.progress_stage.emit(
                    job_id, "downloading", next_size, -1
                )
                if reply is not self._reply or self._job_id != job_id:
                    return
        if len(self._buffer) > self._max_response_bytes:
            self._finish(
                TerminalState.ERROR,
                "Network response exceeds the configured size limit",
            )

    def _reply_finished(self, reply: Any) -> None:
        if reply is not self._reply or self._job_id is None:
            return
        self._read_available(reply)
        if reply is not self._reply:  # size validation may have terminated the job
            return
        self._timeout.stop()
        status_value = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        status = int(status_value) if status_value is not None else 0
        network_error = reply.error() if hasattr(reply, "error") else 0
        network_detail = self._ssl_error_detail or (
            reply.errorString() if hasattr(reply, "errorString") else ""
        )
        stage = self._stage
        data = bytes(self._buffer)

        if stage == "remote":
            target = reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute)
            if target and status in {301, 302, 303, 307, 308}:
                original = self._remote_original_url or ""
                next_url = QUrl(original).resolved(QUrl(target)).toString()
                redirects = int(reply.property("banana_redirect_count") or 0)
                self._release_reply(reply)
                if redirects >= self._max_redirects:
                    self._finish(TerminalState.ERROR, "Too many remote image redirects")
                else:
                    self._begin_remote_download(next_url, redirects + 1)
                return

        self._release_reply(reply)

        # Qt reports many HTTP 4xx/5xx replies as Content* network errors too.
        # The HTTP response is the authoritative diagnosis whenever a status is
        # available; checking reply.error() first used to hide useful 401/402/429
        # provider messages behind a generic "network request failed" notice.
        if stage == "api" and status and status != 200:
            self._finish(
                TerminalState.ERROR,
                self._provider_error(
                    "http",
                    ApiClient._error_detail(data),
                    status=status,
                ),
            )
            return

        if stage == "remote" and status and status != 200:
            detail = ApiClient._error_detail(data)
            suffix = f": {self._safe_error(detail)}" if detail else ""
            self._finish(
                TerminalState.ERROR,
                f"Remote image returned HTTP {status}{suffix}",
            )
            return

        if network_error not in (0, QNetworkReply.NetworkError.NoError):
            self._finish(
                TerminalState.ERROR,
                self._provider_error(
                    self._network_error_category(network_error), network_detail
                ),
            )
            return

        if stage == "api" and not status:
            self._finish(
                TerminalState.ERROR,
                self._provider_error("protocol", "No HTTP status was received"),
            )
            return

        try:
            if stage == "api":
                assert self._api_request is not None
                parsed = ApiClient.parse_response(
                    provider=self._api_request.provider,
                    body=data,
                    status_code=status,
                    max_response_bytes=self._max_response_bytes,
                    max_image_pixels=self._max_image_pixels,
                )
                self._parsed_result = parsed
                if parsed.image is not None:
                    self._finish(TerminalState.SUCCESS, parsed)
                elif parsed.remote_image_url:
                    self._begin_remote_download(parsed.remote_image_url, 0)
                elif parsed.text:
                    self._finish(TerminalState.TEXT_ONLY, parsed.text)
                else:
                    self._finish(
                        TerminalState.ERROR,
                        "Provider returned neither an image nor text",
                    )
            else:
                self._complete_remote_download(data, status, reply)
        except Exception as exc:
            if stage == "api" and isinstance(exc, ApiProtocolError):
                self._finish(
                    TerminalState.ERROR,
                    self._provider_error("response", exc),
                )
            else:
                self._finish(TerminalState.ERROR, self._safe_error(exc))

    def _begin_remote_download(self, url: str, redirect_count: int) -> None:
        if self._job_id is None:
            return
        try:
            host, _port = validate_remote_image_url_syntax(url)
        except UnsafeImageUrlError as exc:
            self._finish(TerminalState.ERROR, self._safe_error(exc))
            return
        job_id = self._job_id
        if redirect_count == 0:
            # The API call has completed. Give the separately validated remote
            # image fetch its own bounded deadline; redirects do not reset it.
            self._deadline.stop()
            self._active_request_timeout_ms = self._remote_timeout_ms
            self._has_total_deadline = True
            self._deadline.start(self._remote_timeout_ms)
            self._last_download_bytes = 0
        self._stage = "resolving"
        self._progress_stage = "downloading"
        self.progress_stage.emit(job_id, "downloading", 0, -1)
        self._timeout.start(self._remote_timeout_ms)

        def resolved(result: Sequence[str] | Exception) -> None:
            if self._job_id != job_id or self._terminal_emitted:
                return
            if isinstance(result, Exception):
                self._finish(TerminalState.ERROR, self._safe_error(result))
                return
            try:
                addresses = ensure_public_addresses(result)
                self._start_pinned_download(url, host, addresses[0], redirect_count)
            except Exception as exc:
                self._finish(TerminalState.ERROR, self._safe_error(exc))

        try:
            self._host_lookup(host, resolved)
        except Exception as exc:
            self._finish(TerminalState.ERROR, self._safe_error(exc))

    @staticmethod
    def _qt_host_lookup(
        host: str, callback: Callable[[Sequence[str] | Exception], None]
    ) -> None:
        def completed(info: QHostInfo) -> None:
            if info.error() != QHostInfo.HostInfoError.NoError:
                callback(UnsafeImageUrlError("Remote image host could not be resolved"))
                return
            callback([address.toString() for address in info.addresses()])

        QHostInfo.lookupHost(host, completed)

    def _start_pinned_download(
        self, original_url: str, original_host: str, address: str, redirect_count: int
    ) -> None:
        parsed = urlsplit(original_url)
        self._timeout.stop()
        pinned = QUrl(original_url)
        pinned.setHost(address)
        request = self._make_request(pinned, self._remote_timeout_ms)

        host_header = original_host
        if parsed.port and parsed.port != 443:
            host_header = f"{host_header}:{parsed.port}"
        request.setRawHeader(b"Host", host_header.encode("idna"))
        # Keep TLS certificate verification/SNI bound to the original hostname
        # while the request URL is pinned to the already-vetted IP address.
        request.setPeerVerifyName(original_host)

        self._stage = "remote"
        self._remote_original_url = original_url
        self._buffer.clear()
        reply = self._manager.get(request)
        reply.setProperty("banana_redirect_count", redirect_count)
        self._attach_reply(reply, self._remote_timeout_ms)

    def _complete_remote_download(self, data: bytes, status: int, reply: Any) -> None:
        if status != 200:
            raise ApiClientError(f"Remote image returned HTTP {status}")
        content_type = bytes(reply.rawHeader(b"Content-Type")).decode(
            "ascii", "ignore"
        )
        if not content_type.lower().startswith("image/"):
            raise ApiClientError("Remote response Content-Type is not an image")
        image = validate_image_bytes(
            data,
            content_type,
            max_bytes=self._max_response_bytes,
            max_pixels=self._max_image_pixels,
        )
        previous = self._parsed_result or ApiResult()
        result = ApiResult(
            text=previous.text,
            image=image,
            input_tokens=previous.input_tokens,
            output_tokens=previous.output_tokens,
        )
        self._finish(TerminalState.SUCCESS, result)

    def _on_timeout(self) -> None:
        if self._job_id is not None:
            timeout_ms = (
                self._remote_timeout_ms
                if self._stage in {"remote", "resolving"}
                else self._active_request_timeout_ms
            )
            self._finish(
                TerminalState.ERROR,
                self._provider_error("timeout", timeout_ms=timeout_ms),
            )

    def _on_deadline(self) -> None:
        if self._job_id is not None and self._has_total_deadline:
            self._finish(
                TerminalState.ERROR,
                self._provider_error(
                    "timeout", timeout_ms=self._active_request_timeout_ms
                ),
            )

    def _release_reply(self, reply: Any) -> None:
        if reply is not self._reply:
            return
        upload_progress_connected = self._upload_progress_connected
        self._upload_progress_connected = False
        self._last_upload_bytes = 0
        self._last_download_bytes = 0
        self._reply = None
        self._buffer.clear()
        try:
            reply.readyRead.disconnect()
            reply.finished.disconnect()
            if upload_progress_connected and hasattr(reply, "uploadProgress"):
                reply.uploadProgress.disconnect()
            if hasattr(reply, "downloadProgress"):
                reply.downloadProgress.disconnect()
            if hasattr(reply, "metaDataChanged"):
                reply.metaDataChanged.disconnect()
            if hasattr(reply, "sslErrors"):
                reply.sslErrors.disconnect()
        except (RuntimeError, TypeError):
            pass
        reply.deleteLater()

    def _finish(self, terminal: TerminalState, payload: Any = None) -> None:
        if self._job_id is None or self._terminal_emitted:
            return
        self._terminal_emitted = True
        job_id = self._job_id
        reply = self._reply
        upload_progress_connected = self._upload_progress_connected
        self._reply = None
        self._timeout.stop()
        self._deadline.stop()
        self._buffer.clear()
        self._api_request = None
        self._parsed_result = None
        self._remote_original_url = None
        self._ssl_error_detail = ""
        self._active_request_timeout_ms = self._request_timeout_ms
        self._has_total_deadline = False
        self._progress_stage = ""
        self._upload_progress_connected = False
        self._last_upload_bytes = 0
        self._last_download_bytes = 0
        self._stage = ""
        self._job_id = None
        self._state = JobState.IDLE

        if reply is not None:
            try:
                reply.readyRead.disconnect()
                reply.finished.disconnect()
                if upload_progress_connected and hasattr(reply, "uploadProgress"):
                    reply.uploadProgress.disconnect()
                if hasattr(reply, "downloadProgress"):
                    reply.downloadProgress.disconnect()
                if hasattr(reply, "metaDataChanged"):
                    reply.metaDataChanged.disconnect()
                if hasattr(reply, "sslErrors"):
                    reply.sslErrors.disconnect()
            except (RuntimeError, TypeError):
                pass
            try:
                reply.abort()
            except RuntimeError:
                pass
            reply.deleteLater()

        # IDLE is announced before the terminal event; starting the next job from
        # any terminal handler is therefore deterministic and never sees "busy".
        self.state_changed.emit(job_id, JobState.IDLE)
        if terminal is TerminalState.SUCCESS:
            self.success.emit(job_id, payload)
        elif terminal is TerminalState.TEXT_ONLY:
            self.text_only.emit(job_id, str(payload or ""))
        elif terminal is TerminalState.CANCELLED:
            self.cancelled.emit(job_id)
        else:
            self.error.emit(job_id, str(payload or "Unknown error"))
        self.terminal.emit(job_id, terminal)

    def _provider_error(
        self,
        category: str,
        detail: Any = "",
        *,
        status: int = 0,
        timeout_ms: int = 0,
    ) -> str:
        provider = (
            self._api_request.provider.value if self._api_request is not None else "api"
        )
        payload: dict[str, Any] = {
            "provider": provider,
            "category": category,
        }
        cleaned = self._safe_error(detail) if str(detail).strip() else ""
        if cleaned and cleaned != "Unknown error":
            payload["detail"] = cleaned
        if status:
            payload["status"] = int(status)
        if timeout_ms:
            payload["timeout_ms"] = int(timeout_ms)
        return _API_ERROR_PREFIX + json.dumps(
            payload, ensure_ascii=True, separators=(",", ":")
        )

    @staticmethod
    def _network_error_category(error: Any) -> str:
        if error in {
            QNetworkReply.NetworkError.HostNotFoundError,
            QNetworkReply.NetworkError.ProxyNotFoundError,
        }:
            return "dns"
        if error == QNetworkReply.NetworkError.SslHandshakeFailedError:
            return "tls"
        if error in {
            QNetworkReply.NetworkError.TimeoutError,
            QNetworkReply.NetworkError.ProxyTimeoutError,
        }:
            return "timeout"
        if error in {
            QNetworkReply.NetworkError.ConnectionRefusedError,
            QNetworkReply.NetworkError.ProxyConnectionRefusedError,
        }:
            return "refused"
        if error in {
            QNetworkReply.NetworkError.ProxyAuthenticationRequiredError,
            QNetworkReply.NetworkError.AuthenticationRequiredError,
        }:
            return "authentication"
        if error in {
            QNetworkReply.NetworkError.RemoteHostClosedError,
            QNetworkReply.NetworkError.TemporaryNetworkFailureError,
            QNetworkReply.NetworkError.NetworkSessionFailedError,
            QNetworkReply.NetworkError.ProxyConnectionClosedError,
            QNetworkReply.NetworkError.OperationCanceledError,
        }:
            return "connection"
        return "network"

    def _safe_error(self, error: Any) -> str:
        sensitive: list[str] = []
        if self._api_request is not None:
            for name, value in self._api_request.headers.items():
                if name.casefold() in {
                    "authorization",
                    "x-goog-api-key",
                    "api-key",
                    "x-api-key",
                }:
                    sensitive.append(value)
                    if value.casefold().startswith("bearer "):
                        sensitive.append(value[7:].strip())
        message = redact_sensitive_text(str(error), tuple(sensitive))
        message = " ".join(message.replace("\x00", " ").split())
        return message[:300] or "Unknown error"


__all__ = ["ImageService", "JobState", "StartResult", "TerminalState"]
