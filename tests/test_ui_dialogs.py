from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QMessageBox

from banana_prism.models import ApiPreset, EditRequest, GenerationRequest
from banana_prism.ui.dialogs.console_dialog import ConsoleDialog
from banana_prism.ui.dialogs.preflight_dialog import PreflightDialog
from banana_prism.ui.widgets.log_panel import LogPanel
from banana_prism.ui.widgets.text_result_panel import TextResultPanel


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_preflight_keeps_exact_request_and_all_network_fields(app: QApplication) -> None:
    request = GenerationRequest("prompt", "model/id", "short", "2K", "16:9", "p", "openrouter")
    dialog = PreflightDialog(request, preset_name="Production")
    assert dialog.request is request
    assert dialog.summary == {
        "操作": "生成图片",
        "API 预设": "Production",
        "预设 ID": "p",
        "服务商": "openrouter",
        "模型": "short  ·  model/id",
        "尺寸": "2K",
        "比例": "16:9",
    }


def test_edit_preflight_declares_png_yellow_guide_and_source_metadata(app: QApplication) -> None:
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("black"))
    request = EditRequest(
        b"source", b"guide", "replace", "m", "short", "4K", "3:4", "p", "aihubmix",
        source_fmt="webp", source_dpi=(144.0, 144.0),
        wire_source_fmt="png",
    )
    dialog = PreflightDialog(request, source_image=image, annotated_image=image)
    assert dialog.request is request
    assert dialog.summary["尺寸"] == "4K"
    assert dialog.summary["比例"] == "3:4"
    assert dialog.summary["原文件 / 编辑输出格式"] == "WEBP"
    assert dialog.summary["上传源图格式"] == "PNG"
    assert dialog.summary["源 DPI"] == "144×144"
    assert dialog.summary["标注格式"] == "PNG"


class ConsoleSettingsFake:
    def __init__(self) -> None:
        self.values = {"active_preset_id": "", "save_dir": "D:/images"}
        self.presets = ()
        self.saved: list[dict] = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    def upsert_api_preset(self, **kwargs):
        self.saved.append(kwargs)


def test_console_uses_secure_preset_contract_without_key_in_table(app: QApplication) -> None:
    settings = ConsoleSettingsFake()
    dialog = ConsoleDialog(settings)
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "🔑 API 配置", "📊 统计", "⚙ 配置", "🗂 系统"
    ]
    preset = ApiPreset("p", "Private", "openrouter", "api:p")
    dialog._save_preset(preset, "top-secret")
    assert settings.saved[-1]["api_key"] == "top-secret"
    assert "top-secret" not in " ".join(
        dialog.preset_table.item(0, column).text() for column in range(dialog.preset_table.columnCount())
    )
    assert dialog.prompt_length_spin.minimum() == 10
    assert dialog.prompt_length_spin.maximum() == 100


def test_console_reports_grouped_settings_failure_without_claiming_success(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSettings(ConsoleSettingsFake):
        def set_many(self, values):
            raise OSError("disk full")

    settings = FailingSettings()
    dialog = ConsoleDialog(settings)
    dialog.output_dir_edit.setText("D:/new-location")
    emitted = []
    shown = []
    dialog.settingsChanged.connect(lambda: emitted.append(True))
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, title, body: shown.append((title, body)),
    )

    dialog.apply_settings()

    assert not emitted
    assert shown and "disk full" in shown[0][1]
    assert settings.values["save_dir"] == "D:/images"


@pytest.mark.parametrize("action", ["delete", "activate"])
def test_console_preset_mutation_failure_keeps_committed_state(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    class FailingPresetSettings(ConsoleSettingsFake):
        def __init__(self) -> None:
            super().__init__()
            self.values["active_preset_id"] = "p1"
            self.presets = (
                ApiPreset("p1", "One", "openrouter", "api:p1"),
                ApiPreset("p2", "Two", "aihubmix", "api:p2"),
            )

        def remove_api_preset(self, _preset_id: str) -> None:
            raise OSError("delete denied")

        def set_active_preset(self, _preset_id: str) -> None:
            raise OSError("activate denied")

    settings = FailingPresetSettings()
    dialog = ConsoleDialog(settings)
    dialog.preset_table.selectRow(0 if action == "delete" else 1)
    emitted: list[bool] = []
    shown: list[tuple[str, str]] = []
    dialog.presetsChanged.connect(lambda: emitted.append(True))
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, title, body: shown.append((title, body)),
    )

    if action == "delete":
        dialog._delete_preset()
        expected_error = "delete denied"
    else:
        dialog._set_active()
        expected_error = "activate denied"

    assert not emitted
    assert shown and expected_error in shown[0][1]
    assert dialog._active_id == "p1"
    assert [preset.preset_id for preset in dialog._presets] == ["p1", "p2"]


def test_text_and_log_panels_expose_copyable_plain_text(app: QApplication) -> None:
    log = LogPanel()
    log.append_log("error", "network failed")
    assert "network failed" in log.text()
    panel = TextResultPanel()
    panel.set_text("model explanation")
    assert panel.text() == "model explanation"
    panel.set_expanded(False)
    assert not panel.is_expanded()
