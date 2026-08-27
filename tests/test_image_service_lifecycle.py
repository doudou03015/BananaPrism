from __future__ import annotations

import base64
import json

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QCoreApplication, QIODevice, QObject, QUrl, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from banana_prism.i18n import user_error_text
from banana_prism.models import JobState
from banana_prism.services.api_client import ApiClient
from banana_prism.services.image_service import ImageService, TerminalState


def png_bytes() -> bytes:
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("#f5d547"))
    target = QByteArray()
    buffer = QBuffer(target)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(target)


class FakeReply(QObject):
    readyRead = Signal()
    finished = Signal()
    uploadProgress = Signal(int, int)
    downloadProgress = Signal(int, int)
    metaDataChanged = Signal()
    sslErrors = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._body = bytearray()
        self._attributes = {}
        self._headers = {}
        self._error = QNetworkReply.NetworkError.NoError
        self._error_string = "fake network error"
        self.aborted = False

    def readAll(self):
        value = bytes(self._body)
        self._body.clear()
        return QByteArray(value)

    def attribute(self, key):
        return self._attributes.get(key)

    def rawHeader(self, name):
        key = bytes(name).lower()
        return QByteArray(self._headers.get(key, b""))

    def error(self):
        return self._error

    def errorString(self):
        return self._error_string

    def abort(self):
        self.aborted = True
        self.finished.emit()

    def deliver(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers=None,
        redirect=None,
        error=QNetworkReply.NetworkError.NoError,
        error_string: str = "fake network error",
        ssl_errors=None,
    ):
        self._body.extend(body)
        self._attributes[QNetworkRequest.Attribute.HttpStatusCodeAttribute] = status
        self._error = error
        self._error_string = error_string
        if redirect is not None:
            self._attributes[QNetworkRequest.Attribute.RedirectionTargetAttribute] = QUrl(redirect)
        for key, value in (headers or {}).items():
            self._headers[key.lower().encode("ascii")] = value.encode("ascii")
        self.metaDataChanged.emit()
        if ssl_errors is not None:
            self.sslErrors.emit(ssl_errors)
        self.readyRead.emit()
        self.finished.emit()


class FakeManager(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.posts = []
        self.gets = []

    def post(self, request, body):
        reply = FakeReply()
        self.posts.append((request, bytes(body), reply))
        return reply

    def get(self, request):
        reply = FakeReply()
        self.gets.append((request, reply))
        return reply


def app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def request(provider: str = "openrouter"):
    return ApiClient.build_generation_request(
        provider=provider,
        api_key="test-key",
        model_id="google/current-ga-image",
        prompt="test",
        image_size="1K",
        aspect_ratio="1:1",
    )


def start_openrouter_4k_edit(service: ImageService):
    raw = png_bytes()
    return service.start_edit(
        provider="openrouter",
        api_key="test-key",
        model_id="google/gemini-3.1-flash-image",
        edit_prompt="change only the marked area",
        original_image=raw,
        annotated_image=raw,
        image_size="4K",
        aspect_ratio="1:1",
    )


def test_openrouter_4k_edit_has_long_deadline_but_other_requests_keep_timeout() -> None:
    app()
    manager = FakeManager()
    service = ImageService(
        network_manager=manager,
        request_timeout_ms=300_000,
        openrouter_4k_edit_timeout_ms=900_000,
    )

    edit = start_openrouter_4k_edit(service)
    assert edit.accepted
    edit_request = manager.posts[-1][0]
    assert edit_request.transferTimeout() == 900_000
    assert service._timeout.interval() == 900_000
    assert service._deadline.isActive()
    assert service._deadline.interval() == 900_000
    service.cancel(edit.job_id)

    ordinary = service.start(request())
    assert ordinary.accepted
    ordinary_request = manager.posts[-1][0]
    assert ordinary_request.transferTimeout() == 300_000
    assert service._timeout.interval() == 300_000
    assert service._deadline.isActive()
    assert service._deadline.interval() == 300_000
    service.cancel(ordinary.job_id)

    # A 4K generation uses the Images API too, but only edits carry large input
    # references and receive the extended deadline.
    generation = service.start_generation(
        provider="openrouter",
        api_key="test-key",
        model_id="google/gemini-3.1-flash-image",
        prompt="test",
        image_size="4K",
        aspect_ratio="1:1",
    )
    assert generation.accepted
    assert manager.posts[-1][0].transferTimeout() == 300_000
    assert service._deadline.isActive()
    assert service._deadline.interval() == 300_000
    service.cancel(generation.job_id)


def test_upload_progress_refreshes_timeout_and_reports_all_transport_stages() -> None:
    app()
    manager = FakeManager()
    service = ImageService(
        network_manager=manager,
        request_timeout_ms=300,
        openrouter_4k_edit_timeout_ms=900,
    )
    detailed = []
    compatible = []
    service.progress_stage.connect(
        lambda job, stage, current, total: detailed.append(
            (job, stage, current, total)
        )
    )
    service.progress.connect(
        lambda job, current, total: compatible.append((job, current, total))
    )

    started = start_openrouter_4k_edit(service)
    reply = manager.posts[-1][2]
    body_size = len(manager.posts[-1][1])
    assert detailed[0] == (started.job_id, "uploading", 0, body_size)

    # Simulate the old 300 ms timer being close to expiry. Real upload activity
    # must refresh it with the extended edit timeout, not retain the stale value.
    service._timeout.start(300)
    reply.uploadProgress.emit(body_size // 2, body_size)
    assert service._timeout.interval() == 900
    assert detailed[-1] == (
        started.job_id,
        "uploading",
        body_size // 2,
        body_size,
    )
    assert compatible == []

    # Duplicate/no-growth progress events must not refresh the inactivity timer.
    service._timeout.start(25)
    reply.uploadProgress.emit(body_size // 2, body_size)
    assert service._timeout.interval() == 25

    reply.uploadProgress.emit(body_size, body_size)
    assert detailed[-2] == (started.job_id, "uploading", body_size, body_size)
    assert detailed[-1] == (started.job_id, "generating", 0, -1)

    reply.downloadProgress.emit(4096, 8192)
    assert detailed[-1] == (started.job_id, "downloading", 4096, 8192)
    assert compatible[-1] == (started.job_id, 4096, 8192)
    service.cancel(started.job_id)


def test_long_edit_deadline_and_late_reply_still_emit_one_terminal() -> None:
    app()
    manager = FakeManager()
    service = ImageService(
        network_manager=manager,
        request_timeout_ms=300,
        openrouter_4k_edit_timeout_ms=900,
    )
    terminals = []
    errors = []
    service.terminal.connect(lambda job, state: terminals.append((job, state)))
    service.error.connect(lambda job, message: errors.append((job, message)))

    started = start_openrouter_4k_edit(service)
    reply = manager.posts[-1][2]
    service._on_deadline()

    assert terminals == [(started.job_id, TerminalState.ERROR)]
    assert len(errors) == 1
    assert '"timeout_ms":900' in errors[0][1]
    assert service.state is JobState.IDLE

    reply.deliver(b"{}")
    assert terminals == [(started.job_id, TerminalState.ERROR)]
    assert len(errors) == 1


def test_cancel_from_initial_upload_stage_does_not_post_a_zombie_request() -> None:
    app()
    manager = FakeManager()
    service = ImageService(network_manager=manager)
    terminals = []
    service.progress_stage.connect(
        lambda job, stage, current, total: service.cancel(job)
        if stage == "uploading"
        else None
    )
    service.terminal.connect(lambda job, state: terminals.append((job, state)))

    started = start_openrouter_4k_edit(service)

    assert started.accepted
    assert manager.posts == []
    assert terminals == [(started.job_id, TerminalState.CANCELLED)]
    assert service.state is JobState.IDLE


def test_start_busy_cancel_and_late_reply_are_exactly_once() -> None:
    app()
    manager = FakeManager()
    service = ImageService(network_manager=manager)
    terminals = []
    cancelled = []
    errors = []
    service.terminal.connect(lambda job, state: terminals.append((job, state)))
    service.cancelled.connect(cancelled.append)
    service.error.connect(lambda job, message: errors.append((job, message)))

    first = service.start(request())
    second = service.start(request())
    assert first.accepted and first.job_id
    assert not second.accepted and second.reason == "busy"
    assert second.job_id == first.job_id
    assert service.state is JobState.RUNNING
    reply = manager.posts[0][2]
    assert service.cancel(first.job_id)
    assert service.state is JobState.IDLE
    assert cancelled == [first.job_id]
    assert terminals == [(first.job_id, TerminalState.CANCELLED)]

    # A queued Qt signal from an aborted reply cannot create a second terminal.
    reply.deliver(b"{}")
    assert terminals == [(first.job_id, TerminalState.CANCELLED)]
    assert not errors


def test_cancel_from_started_signal_never_posts_a_zombie_request() -> None:
    app()
    manager = FakeManager()
    service = ImageService(network_manager=manager)
    terminal = []
    service.started.connect(lambda job_id: service.cancel(job_id))
    service.terminal.connect(lambda job_id, state: terminal.append((job_id, state)))
    result = service.start(request())
    assert result.accepted
    assert manager.posts == []
    assert terminal == [(result.job_id, TerminalState.CANCELLED)]
    assert service.state is JobState.IDLE


def test_timeout_and_cancel_both_allow_an_immediate_restart() -> None:
    app()
    manager = FakeManager()
    service = ImageService(network_manager=manager)
    errors = []
    terminals = []
    service.error.connect(lambda job, message: errors.append((job, message)))
    service.terminal.connect(lambda job, state: terminals.append((job, state)))

    timed_out = service.start(request())
    service._on_timeout()
    assert errors[0][0] == timed_out.job_id
    assert user_error_text(errors[0][1]) == (
        "OpenRouter 请求超时（300 秒），请稍后重试或检查代理与网络稳定性。"
    )
    assert terminals == [(timed_out.job_id, TerminalState.ERROR)]
    assert service.state is JobState.IDLE

    cancelled = service.start(request())
    assert cancelled.accepted and cancelled.job_id != timed_out.job_id
    assert service.cancel(cancelled.job_id)
    restarted = service.start(request())
    assert restarted.accepted and restarted.job_id not in {
        timed_out.job_id,
        cancelled.job_id,
    }
    service.cancel(restarted.job_id)


def test_inline_success_and_start_next_from_terminal_handler() -> None:
    app()
    manager = FakeManager()
    service = ImageService(network_manager=manager)
    raw = png_bytes()
    response = {
        "choices": [
            {
                "message": {
                    "content": "caption",
                    "images": [{"image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}}],
                }
            }
        ]
    }
    next_results = []
    received = []

    def on_success(job_id, result):
        received.append((job_id, result))
        next_results.append(service.start(request()))

    service.success.connect(on_success)
    first = service.start(request())
    manager.posts[0][2].deliver(json.dumps(response).encode())
    assert received[0][0] == first.job_id
    assert received[0][1].image.data == raw
    assert next_results[0].accepted
    assert service.current_job_id == next_results[0].job_id
    assert service.state is JobState.RUNNING
    service.cancel()


def test_text_only_and_http_error_terminal_paths() -> None:
    app()
    manager = FakeManager()
    service = ImageService(network_manager=manager)
    text_events = []
    error_events = []
    service.text_only.connect(lambda job, text: text_events.append((job, text)))
    service.error.connect(lambda job, text: error_events.append((job, text)))

    first = service.start(request())
    manager.posts[-1][2].deliver(
        b'{"choices":[{"message":{"content":"explanation only"}}]}'
    )
    assert text_events == [(first.job_id, "explanation only")]

    second = service.start(request())
    manager.posts[-1][2].deliver(
        b'{"error":{"message":"rejected"}}',
        status=429,
        error=QNetworkReply.NetworkError.ContentAccessDenied,
    )
    assert error_events and error_events[-1][0] == second.job_id
    assert user_error_text(error_events[-1][1]) == "OpenRouter 返回 HTTP 429：rejected"


@pytest.mark.parametrize(
    ("network_error", "error_string", "expected"),
    [
        (
            QNetworkReply.NetworkError.HostNotFoundError,
            "Host openrouter.ai not found",
            "OpenRouter DNS 解析失败",
        ),
        (
            QNetworkReply.NetworkError.SslHandshakeFailedError,
            "SSL handshake failed",
            "OpenRouter TLS/SSL 握手失败",
        ),
        (
            QNetworkReply.NetworkError.TimeoutError,
            "Connection timed out",
            "OpenRouter 请求超时",
        ),
        (
            QNetworkReply.NetworkError.ConnectionRefusedError,
            "Connection refused",
            "OpenRouter 连接被拒绝",
        ),
        (
            QNetworkReply.NetworkError.RemoteHostClosedError,
            "Remote host closed the connection",
            "OpenRouter 连接中断",
        ),
    ],
)
def test_network_errors_keep_provider_and_actionable_category(
    network_error, error_string: str, expected: str
) -> None:
    app()
    manager = FakeManager()
    service = ImageService(network_manager=manager)
    errors = []
    service.error.connect(lambda job, message: errors.append((job, message)))

    started = service.start(request())
    manager.posts[-1][2].deliver(
        b"",
        status=0,
        error=network_error,
        error_string=error_string,
    )

    assert errors[0][0] == started.job_id
    display = user_error_text(errors[0][1])
    assert expected in display
    assert error_string in display or network_error == QNetworkReply.NetworkError.TimeoutError


def test_ssl_signal_detail_and_http_error_body_are_sanitized() -> None:
    class FakeSslError:
        def errorString(self) -> str:
            return "certificate expired for api.openrouter.test"

    app()
    manager = FakeManager()
    service = ImageService(network_manager=manager)
    errors = []
    service.error.connect(lambda job, message: errors.append(message))

    service.start(request())
    manager.posts[-1][2].deliver(
        b"",
        status=0,
        error=QNetworkReply.NetworkError.SslHandshakeFailedError,
        error_string="generic TLS failure",
        ssl_errors=[FakeSslError()],
    )
    assert "certificate expired" in user_error_text(errors[-1])

    service.start(request())
    manager.posts[-1][2].deliver(
        b'{"error":{"message":"quota denied; echoed test-key\\r\\nretry"}}',
        status=402,
        error=QNetworkReply.NetworkError.ContentAccessDenied,
    )
    display = user_error_text(errors[-1])
    assert display.startswith("OpenRouter 返回 HTTP 402：quota denied")
    assert "test-key" not in errors[-1]
    assert "[REDACTED]" in display
    assert "\n" not in display and "\r" not in display


def test_remote_download_is_pinned_and_private_redirect_is_rejected() -> None:
    app()
    manager = FakeManager()
    lookups = []

    def public_lookup(host, callback):
        lookups.append(host)
        callback(["93.184.216.34"])

    service = ImageService(network_manager=manager, host_lookup=public_lookup)
    errors = []
    service.error.connect(lambda job, text: errors.append((job, text)))
    first = service.start(request())
    response = {
        "choices": [
            {
                "message": {
                    "images": [{"image_url": {"url": "https://cdn.example.test/a.png"}}]
                }
            }
        ]
    }
    manager.posts[0][2].deliver(json.dumps(response).encode())
    assert lookups == ["cdn.example.test"]
    assert manager.gets[0][0].url().host() == "93.184.216.34"
    manager.gets[0][1].deliver(
        b"", status=302, redirect="https://127.0.0.1/private.png"
    )
    assert errors and errors[-1][0] == first.job_id
    assert "non-public" in errors[-1][1]


def test_remote_download_validates_content_type_and_image() -> None:
    app()
    manager = FakeManager()
    service = ImageService(
        network_manager=manager,
        host_lookup=lambda host, callback: callback(["93.184.216.34"]),
    )
    successes = []
    service.success.connect(lambda job, result: successes.append((job, result)))
    started = service.start(request())
    response = {
        "choices": [
            {"message": {"images": [{"url": "https://cdn.example.test/a.png"}]}}
        ]
    }
    manager.posts[0][2].deliver(json.dumps(response).encode())
    raw = png_bytes()
    manager.gets[0][1].deliver(
        raw,
        headers={"Content-Type": "image/png", "Content-Length": str(len(raw))},
    )
    assert successes[0][0] == started.job_id
    assert successes[0][1].image.data == raw
    assert service.state is JobState.IDLE
