from __future__ import annotations

import base64
import json

from PySide6.QtCore import QByteArray, QBuffer, QCoreApplication, QIODevice, QObject, QUrl, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

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
    downloadProgress = Signal(int, int)
    metaDataChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._body = bytearray()
        self._attributes = {}
        self._headers = {}
        self._error = QNetworkReply.NetworkError.NoError
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
        return "fake network error"

    def abort(self):
        self.aborted = True
        self.finished.emit()

    def deliver(self, body: bytes, *, status: int = 200, headers=None, redirect=None):
        self._body.extend(body)
        self._attributes[QNetworkRequest.Attribute.HttpStatusCodeAttribute] = status
        if redirect is not None:
            self._attributes[QNetworkRequest.Attribute.RedirectionTargetAttribute] = QUrl(redirect)
        for key, value in (headers or {}).items():
            self._headers[key.lower().encode("ascii")] = value.encode("ascii")
        self.metaDataChanged.emit()
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


def request():
    return ApiClient.build_generation_request(
        provider="openrouter",
        api_key="test-key",
        model_id="google/current-ga-image",
        prompt="test",
        image_size="1K",
        aspect_ratio="1:1",
    )


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
    assert errors == [(timed_out.job_id, "Network request timed out")]
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
        b'{"error":{"message":"rejected"}}', status=429
    )
    assert error_events and error_events[-1][0] == second.job_id
    assert "HTTP 429" in error_events[-1][1]


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
