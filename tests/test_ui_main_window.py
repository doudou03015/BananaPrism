from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image as PILImage
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QPoint, Signal
from PySide6.QtGui import QColor, QCloseEvent, QImage, QImageReader
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QScrollArea

from banana_prism.constants import MODELS
from banana_prism import __version__
from banana_prism.models import ApiPreset, QueueStatus
from banana_prism.services.file_service import FileService
from banana_prism.services.log_service import REDACTION, register_process_secret
from banana_prism.ui.main_window import MainWindow


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def png_bytes(width: int = 32, height: int = 24) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#284b78"))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def image_bytes(fmt: str, width: int = 32, height: int = 24) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#284b78"))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, fmt.upper(), 95 if fmt.lower() in {"jpg", "jpeg"} else -1)
    return bytes(data)


class FakeSettings:
    def __init__(self) -> None:
        self.values = {
            "active_preset_id": "p1",
            "last_model_index": 0,
            "last_size_index": 0,
            "last_ratio": "1:1",
            "confirm_requests": True,
            "today_gen": {"count": 0},
        }
        self.presets = (
            ApiPreset("p1", "Preset One", "openrouter", "api:p1"),
            ApiPreset("p2", "Preset Two", "aihubmix", "api:p2"),
        )
        self.keys = {"p1": "key-one", "p2": "key-two"}
        self.today_generation_count = 0

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def set(self, key: str, value) -> None:
        self.values[key] = value

    def get_api_key(self, preset_id: str) -> str | None:
        return self.keys.get(preset_id)

    def increment_generation_count(self) -> int:
        self.today_generation_count += 1
        return self.today_generation_count


class FakeImageService(QObject):
    success = Signal(str, object)
    error = Signal(str, str)
    text_only = Signal(str, str)
    cancelled = Signal(str)
    progress = Signal(str, int, int)

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.calls: list[tuple[str, dict]] = []
        self.current_job_id: str | None = None
        self.counter = 0

    def _start(self, kind: str, kwargs: dict):
        if self.busy:
            return SimpleNamespace(accepted=False, job_id=self.current_job_id, reason="busy")
        self.counter += 1
        self.current_job_id = f"job-{self.counter}"
        self.busy = True
        self.calls.append((kind, kwargs))
        return SimpleNamespace(accepted=True, job_id=self.current_job_id, reason="")

    def start_generation(self, **kwargs):
        return self._start("generation", kwargs)

    def start_edit(self, **kwargs):
        return self._start("edit", kwargs)

    def complete(self, *, text: str = "ok") -> None:
        job = self.current_job_id
        self.busy = False
        self.current_job_id = None
        image = SimpleNamespace(data=png_bytes(), fmt="png", width=32, height=24)
        self.success.emit(job, SimpleNamespace(image=image, text=text, input_tokens=2, output_tokens=3))

    def fail(self, message: str = "boom") -> None:
        job = self.current_job_id
        self.busy = False
        self.current_job_id = None
        self.error.emit(job, message)

    def only_text(self, text: str = "no image") -> None:
        job = self.current_job_id
        self.busy = False
        self.current_job_id = None
        self.text_only.emit(job, text)

    def cancel(self, job_id: str | None = None) -> bool:
        if not self.busy or (job_id and job_id != self.current_job_id):
            return False
        job = self.current_job_id
        self.busy = False
        self.current_job_id = None
        self.cancelled.emit(job)
        return True


class FakeStorage:
    def __init__(self) -> None:
        self.generations = []
        self.edits = []
        self.imports = []

    def auto_save(self, result):
        self.generations.append(result)
        return Path("D:/saved/generated.png")

    def auto_save_edit(self, result):
        self.edits.append(result)
        return Path(f"D:/saved/edited.{result.fmt}")

    def save_imported(self, data: bytes, fmt: str):
        self.imports.append((data, fmt))
        return Path(f"D:/saved/imported.{fmt}")


class FailingImportStorage(FakeStorage):
    def save_imported(self, data: bytes, fmt: str):
        raise OSError("disk full")


class FakeLog:
    def __init__(self) -> None:
        self.records = []

    def write(self, level: str, message: str) -> bool:
        self.records.append((level, message))
        return True


class FakeAuth:
    def __init__(self) -> None:
        self.password: str | None = None

    def has_password(self) -> bool:
        return self.password is not None

    def set_password(self, password: str) -> None:
        self.password = password

    def verify_password(self, password: str) -> bool:
        return password == self.password


def _window() -> tuple[MainWindow, FakeSettings, FakeImageService]:
    settings = FakeSettings()
    service = FakeImageService()
    return MainWindow(settings, service, None), settings, service


def _process(app: QApplication) -> None:
    for _ in range(3):
        app.processEvents()


def test_visible_shared_parameters_are_dispatched_exactly_without_generation_preflight(
    app: QApplication,
) -> None:
    window, _settings, service = _window()
    window._size_combo.setCurrentText("2K")
    ratio = next(button for button in window._ratio_group.buttons() if button.text() == "4:3")
    ratio.click()
    window._prompt_edit.setPlainText("a glass banana in a prism")
    assert window.begin_generation()
    request = window._current_job.request
    kind, kwargs = service.calls[-1]
    assert kind == "generation"
    assert kwargs["image_size"] == request.size == "2K"
    assert kwargs["aspect_ratio"] == request.ratio == "4:3"
    assert kwargs["provider"] == request.provider == "openrouter"
    assert not window._size_combo.isHidden()
    assert all(not button.isHidden() for button in window._ratio_group.buttons())
    service.cancel()


def test_default_sidebar_controls_fit_inside_horizontal_viewport(app: QApplication) -> None:
    window, _settings, _service = _window()
    window.resize(1510, 920)
    window.show()
    _process(app)
    scroll = window.findChild(QScrollArea, "controlScroll")
    assert scroll is not None
    viewport = scroll.viewport()
    for control in (window._preset_combo, window._model_combo, *window._ratio_group.buttons()):
        top_left = control.mapTo(viewport, QPoint(0, 0))
        assert top_left.x() >= 0
        assert top_left.x() + control.width() <= viewport.width()
    window.close()


def test_legacy_25_model_exposes_only_supported_1k_size(app: QApplication) -> None:
    window, _settings, _service = _window()
    legacy_index = next(
        index
        for index, model in enumerate(MODELS)
        if model.model_id == "google/gemini-2.5-flash-image"
    )
    window._size_combo.setCurrentText("4K")
    window._model_combo.setCurrentIndex(legacy_index)

    assert [window._size_combo.itemText(i) for i in range(window._size_combo.count())] == ["1K"]
    assert window._size_combo.currentText() == "1K"


def test_edit_mandatory_preflight_sends_same_request_and_png_guide(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    window, _settings, service = _window()
    source = png_bytes(96, 72)
    window._work_bytes = source
    window._work_fmt = "png"
    window._work_source_dpi = (144.0, 144.0)
    window._preview.set_image(source)
    canvas = window._preview.canvas
    canvas.set_tool("brush")
    canvas.set_brush_radius(8)
    canvas._stroke_to(canvas.rect().center().toPointF())
    canvas._finish_stroke()
    window._edit_prompt.setPlainText("turn this area into polished gold")
    window._size_combo.setCurrentText("4K")
    next(button for button in window._ratio_group.buttons() if button.text() == "3:2").click()

    captured = []

    def accept(dialog) -> int:
        captured.append(dialog.request)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("banana_prism.ui.main_window.PreflightDialog.exec", accept)
    assert window.begin_edit()
    kind, kwargs = service.calls[-1]
    assert kind == "edit"
    assert kwargs["annotated_image"].startswith(b"\x89PNG\r\n\x1a\n")
    assert kwargs["image_size"] == "4K"
    assert kwargs["aspect_ratio"] == "3:2"
    assert captured[0] is window._current_job.request
    assert captured[0].source_dpi == (144.0, 144.0)
    assert captured[0].wire_source_fmt == "png"
    service.cancel()


def test_queue_freezes_preset_and_pauses_after_three_consecutive_failures(app: QApplication) -> None:
    window, _settings, service = _window()
    for index in range(4):
        window._prompt_edit.setPlainText(f"queued prompt {index}")
        window._on_add_queue()
    window._preset_combo.setCurrentIndex(window._preset_combo.findData("p2"))

    window._on_start_queue()
    assert service.calls[-1][1]["provider"] == "openrouter"
    for expected_failures in range(1, 4):
        service.fail(f"failure {expected_failures}")
        _process(app)
        assert sum(task.status == QueueStatus.FAILED for task in window._queue) == expected_failures
    assert not window._queue_running
    assert window._queue[3].status == QueueStatus.PENDING
    assert "连续失败 3 次" in window._log_panel.text()


def test_queue_freezes_preset_id_but_pairs_current_provider_with_latest_key(
    app: QApplication,
) -> None:
    window, settings, service = _window()
    window._prompt_edit.setPlainText("queued with a stable preset id")
    window._on_add_queue()
    frozen = window._queue[0]

    settings.presets = (
        ApiPreset("p1", "Preset One Updated", "aihubmix", "api:p1"),
        settings.presets[1],
    )
    settings.keys["p1"] = "latest-aihubmix-key"
    window._on_start_queue()

    kind, kwargs = service.calls[-1]
    assert kind == "generation"
    assert frozen.preset_id == "p1"
    assert kwargs["provider"] == "aihubmix"
    assert kwargs["api_key"] == "latest-aihubmix-key"
    service.cancel()


def test_ten_queue_items_run_strictly_once_and_serially(app: QApplication) -> None:
    window, _settings, service = _window()
    for index in range(10):
        window._prompt_edit.setPlainText(f"serial item {index}")
        window._on_add_queue()

    window._on_start_queue()
    for expected_calls in range(1, 11):
        assert len(service.calls) == expected_calls
        assert sum(task.status == QueueStatus.RUNNING for task in window._queue) == 1
        service.complete(text=f"done {expected_calls}")
        _process(app)

    assert len(service.calls) == 10
    assert all(task.status == QueueStatus.DONE for task in window._queue)
    assert not window._queue_running


def test_text_only_advances_queue_and_cancelled_is_retryable(app: QApplication) -> None:
    window, _settings, service = _window()
    for prompt in ("first", "second"):
        window._prompt_edit.setPlainText(prompt)
        window._on_add_queue()
    window._on_start_queue()
    service.only_text("I cannot draw that")
    _process(app)
    assert window._queue[0].status == QueueStatus.FAILED
    assert len(service.calls) == 2
    window._on_stop_queue()
    assert window._queue[1].status == QueueStatus.CANCELLED
    window._on_retry_queue()
    assert window._queue[0].status in {QueueStatus.PENDING, QueueStatus.RUNNING}


@pytest.mark.parametrize("mutation", ["remove_previous", "clear_completed"])
def test_queue_terminal_tracks_running_task_identity_after_rows_move(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    window, _settings, service = _window()
    for prompt in ("first", "second"):
        window._prompt_edit.setPlainText(prompt)
        window._on_add_queue()
    window._on_start_queue()
    service.complete(text="first done")
    _process(app)
    running = window._queue[1]
    assert window._queue[0].status == QueueStatus.DONE
    assert running.status == QueueStatus.RUNNING

    if mutation == "clear_completed":
        window._on_clear_queue()
    else:
        class RemoveMenu:
            def __init__(self, *_args) -> None:
                self.actions: list[object] = []

            def addAction(self, _text: str) -> object:
                action = object()
                self.actions.append(action)
                return action

            def exec(self, *_args) -> object:
                return self.actions[0]

        monkeypatch.setattr("banana_prism.ui.main_window.QMenu", RemoveMenu)
        first_item = window._queue_list.item(0)
        position = window._queue_list.visualItemRect(first_item).center()
        window._queue_context_menu(position)

    assert window._queue == [running]
    service.complete(text="second done")
    _process(app)

    assert running.status == QueueStatus.DONE
    assert window._queue == [running]
    assert not window._queue_running


def test_close_cancels_active_job_before_later_close(app: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    window, _settings, service = _window()
    window._prompt_edit.setPlainText("running")
    assert window.begin_generation(confirm=False)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    event = QCloseEvent()
    window.closeEvent(event)
    _process(app)
    assert not service.busy
    assert window._current_job is None
    assert window._close_confirmed


def test_generation_embeds_dpi_and_dispatches_concrete_auto_save(app: QApplication) -> None:
    settings = FakeSettings()
    service = FakeImageService()
    storage = FakeStorage()
    log = FakeLog()
    window = MainWindow(settings, service, storage, log_service=log)
    window._size_combo.setCurrentText("2K")
    window._prompt_edit.setPlainText("dpi test")
    assert window.begin_generation()
    service.complete()

    assert len(storage.generations) == 1
    result = storage.generations[0]
    assert result.output_dpi == (150.0, 150.0)
    decoded = QImage.fromData(result.image_bytes)
    assert decoded.dotsPerMeterX() * 0.0254 == pytest.approx(150.0, abs=0.1)
    assert result.saved_path == "D:\\saved\\generated.png" or result.saved_path == "D:/saved/generated.png"
    assert any("已保存" in message for _level, message in log.records)


def test_generation_saves_and_reports_matching_json_sidecar(
    app: QApplication, tmp_path: Path
) -> None:
    settings = FakeSettings()
    service = FakeImageService()
    storage = FileService(save_dir=tmp_path)
    log = FakeLog()
    window = MainWindow(settings, service, storage, log_service=log)
    window._prompt_edit.setPlainText("legacy sidecar compatibility")

    assert window.begin_generation()
    service.complete(text="provider explanation")

    assert window._last_result is not None
    image_path = Path(window._last_result.saved_path or "")
    sidecar_path = image_path.with_suffix(".json")
    assert image_path.is_file()
    assert sidecar_path.is_file()
    assert image_path.stem == sidecar_path.stem
    assert image_path.parent == sidecar_path.parent
    assert any(
        "参数 JSON 已同步保存" in message and str(sidecar_path) in message
        for _level, message in log.records
    )


def test_edit_reencodes_to_source_jpeg_at_300_dpi_and_uses_edit_save(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = FakeSettings()
    service = FakeImageService()
    storage = FakeStorage()
    window = MainWindow(settings, service, storage)
    source = image_bytes("JPEG", 80, 60)
    window._work_bytes = source
    window._work_fmt = "jpeg"
    window._preview.set_image(source)
    canvas = window._preview.canvas
    canvas.set_tool("brush")
    canvas._stroke_to(canvas.rect().center().toPointF())
    canvas._finish_stroke()
    window._edit_prompt.setPlainText("edit")
    monkeypatch.setattr(
        "banana_prism.ui.main_window.PreflightDialog.exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    assert window.begin_edit()
    service.complete()

    assert len(storage.edits) == 1
    result = storage.edits[0]
    assert result.fmt == "jpeg"
    assert result.image_bytes.startswith(b"\xff\xd8\xff")
    assert result.output_dpi == (300.0, 300.0)
    assert QImage.fromData(result.image_bytes).dotsPerMeterX() * 0.0254 == pytest.approx(300.0, abs=0.1)


def test_webp_edit_reports_encoder_dpi_readback_instead_of_claiming_300(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = FakeStorage()
    service = FakeImageService()
    window = MainWindow(FakeSettings(), service, storage)
    source = image_bytes("WEBP", 80, 60)
    window._work_bytes = source
    window._work_fmt = "webp"
    window._preview.set_image(source)
    canvas = window._preview.canvas
    canvas.set_tool("brush")
    canvas._stroke_to(canvas.rect().center().toPointF())
    canvas._finish_stroke()
    window._edit_prompt.setPlainText("edit")
    monkeypatch.setattr(
        "banana_prism.ui.main_window.PreflightDialog.exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )

    assert window.begin_edit()
    service.complete()

    result = storage.edits[0]
    decoded = QImage.fromData(result.image_bytes)
    actual = (
        decoded.dotsPerMeterX() * 0.0254,
        decoded.dotsPerMeterY() * 0.0254,
    )
    expected = tuple(
        300.0 if abs(value - 300.0) <= 0.1 else round(value, 4)
        for value in actual
    )
    assert result.fmt == "webp"
    assert result.output_dpi == expected


def test_import_is_immediately_archived_without_losing_work_image(
    app: QApplication, tmp_path: Path
) -> None:
    settings = FakeSettings()
    storage = FakeStorage()
    window = MainWindow(settings, FakeImageService(), storage)
    source = tmp_path / "source.png"
    source.write_bytes(png_bytes(45, 30))
    assert window.import_image(str(source))
    assert storage.imports == [(source.read_bytes(), "png")]
    assert window._preview.has_image()
    assert window._work_saved_path == "D:\\saved\\imported.png" or window._work_saved_path == "D:/saved/imported.png"
    assert "已归档" in window._source_label.text()


@pytest.mark.parametrize("orientation", range(2, 9))
def test_exif_oriented_jpeg_uses_lossless_normalised_working_coordinates(
    app: QApplication,
    tmp_path: Path,
    orientation: int,
) -> None:
    source = tmp_path / f"orientation-{orientation}.jpg"
    pil = PILImage.new("RGB", (40, 20), "black")
    for x in range(40):
        for y in range(20):
            pil.putpixel((x, y), (x * 5, y * 10, (x + y) * 3))
    exif = PILImage.Exif()
    exif[274] = orientation
    pil.save(source, "JPEG", quality=95, subsampling=0, exif=exif)
    original = source.read_bytes()

    expected_reader = QImageReader(str(source))
    expected_reader.setAutoTransform(True)
    expected = expected_reader.read()
    assert not expected.isNull()

    storage = FakeStorage()
    window = MainWindow(FakeSettings(), FakeImageService(), storage)
    assert window.import_image(str(source))

    working = QImage.fromData(window._work_bytes)
    assert not working.isNull()
    assert working.size() == expected.size()
    for point in ((0, 0), (working.width() - 1, 0), (0, working.height() - 1)):
        assert working.pixelColor(*point) == expected.pixelColor(*point)
    assert window._work_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert window._work_fmt == "jpeg"
    assert storage.imports[-1] == (window._work_bytes, "png")
    assert source.read_bytes() == original


def test_import_archive_failure_keeps_loaded_work_image(app: QApplication, tmp_path: Path) -> None:
    window = MainWindow(FakeSettings(), FakeImageService(), FailingImportStorage())
    source = tmp_path / "still-usable.png"
    source.write_bytes(png_bytes(22, 18))
    assert window.import_image(str(source))
    assert window._preview.has_image()
    assert window._work_bytes == source.read_bytes()
    assert window._work_saved_path is None
    assert "归档失败" in window._log_panel.text()


def test_console_password_setup_then_verifies_every_entry(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = FakeAuth()
    window = MainWindow(FakeSettings(), FakeImageService(), auth_service=auth)

    def setup(dialog) -> int:
        dialog.password_edit.setText("banana7")
        dialog.confirm_edit.setText("banana7")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("banana_prism.ui.main_window.PasswordDialog.exec", setup)
    assert window._console_access_allowed()
    assert auth.password == "banana7"
    monkeypatch.setattr(
        "banana_prism.ui.main_window.PasswordDialog.get_password",
        lambda *args, **kwargs: ("banana7", True),
    )
    assert window._console_access_allowed()


def test_workspace_has_version_and_no_visible_menu_or_manual_save(app: QApplication) -> None:
    window, _settings, _service = _window()
    assert f"v{__version__}" in window.windowTitle()
    assert not window.menuBar().actions()
    assert not hasattr(window, "save_action")
    assert window.findChild(type(window._gen_btn), "sidebarImportButton") is not None


def test_provider_echoed_credential_is_redacted_from_every_copyable_ui(
    app: QApplication,
) -> None:
    secret = "opaque-provider-key-12345"
    register_process_secret(secret)
    window, _settings, service = _window()
    window._prompt_edit.setPlainText("safe prompt")
    assert window.begin_generation()
    service.fail(f"provider echoed {secret}")
    _process(app)

    visible = "\n".join(
        (
            window._log_panel.text(),
            window._text_panel.text(),
            window._preview._status.text(),
        )
    )
    assert secret not in visible
    assert REDACTION in visible
