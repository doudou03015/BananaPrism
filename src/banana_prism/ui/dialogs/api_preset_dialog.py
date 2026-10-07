"""Create/edit an API provider preset without exposing stored credentials."""

from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from banana_prism.constants import API_PROVIDERS
from banana_prism.i18n import tr
from banana_prism.models import ApiPreset
from banana_prism.ui.widgets.wheel_safe_combo_box import WheelSafeComboBox


class ApiPresetDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        preset: ApiPreset | None = None,
        credential_present: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("apiPresetDialog")
        self.setWindowTitle(tr("api_preset.title.edit" if preset else "api_preset.title.add"))
        self.setModal(True)
        self.setMinimumWidth(460)
        self._preset_id = preset.preset_id if preset else uuid4().hex
        self._credential_ref = preset.credential_ref if preset else f"api:{self._preset_id}"
        self._credential_present = credential_present

        self.name_edit = QLineEdit(preset.name if preset else "")
        self.name_edit.setObjectName("presetNameEdit")
        self.name_edit.setPlaceholderText(tr("api_preset.placeholder.name"))
        self.provider_combo = WheelSafeComboBox()
        self.provider_combo.setObjectName("providerCombo")
        for provider_id, spec in API_PROVIDERS.items():
            self.provider_combo.addItem(spec.label, provider_id)
        if preset:
            index = self.provider_combo.findData(preset.provider)
            if index >= 0:
                self.provider_combo.setCurrentIndex(index)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setObjectName("apiKeyEdit")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(
            tr(
                "api_preset.placeholder.keep_key"
                if credential_present
                else "api_preset.placeholder.enter_key"
            )
        )
        self.endpoint_label = QLabel()
        self.endpoint_label.setWordWrap(True)
        self.endpoint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.provider_combo.currentIndexChanged.connect(self._refresh_endpoint)
        self._refresh_endpoint()

        form = QFormLayout()
        form.addRow(tr("api_preset.field.name"), self.name_edit)
        form.addRow(tr("api_preset.field.provider"), self.provider_combo)
        form.addRow(tr("api_preset.field.endpoint"), self.endpoint_label)
        form.addRow(tr("common.api_key"), self.api_key_edit)

        note = QLabel(tr("api_preset.note.security"))
        note.setWordWrap(True)
        note.setObjectName("securityNote")
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("common.save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("common.cancel"))
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)
        self.setStyleSheet(
            """
            QDialog { background: #0d1422; color: #dce5f6; }
            QLineEdit, QComboBox { background: #0a101c; border: 1px solid #33415e; border-radius: 7px; padding: 8px; }
            QLabel#securityNote { color: #93a2bd; }
            """
        )

    def _refresh_endpoint(self) -> None:
        spec = API_PROVIDERS.get(str(self.provider_combo.currentData()))
        # The operation endpoint depends on model and resolution. Display the
        # shared API base here instead of promising one operation for all calls.
        base_url = (
            spec.api_url.split("/models/", 1)[0].removesuffix("/chat/completions")
            if spec else ""
        )
        self.endpoint_label.setText(base_url)

    def _validate_and_accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(
                self,
                tr("api_preset.error.missing_name.title"),
                tr("api_preset.error.missing_name.body"),
            )
            self.name_edit.setFocus()
            return
        if self.provider_combo.currentData() is None:
            QMessageBox.warning(
                self,
                tr("api_preset.error.missing_provider.title"),
                tr("api_preset.error.missing_provider.body"),
            )
            return
        if not self._credential_present and not self.api_key_edit.text().strip():
            QMessageBox.warning(
                self,
                tr("api_preset.error.missing_key.title"),
                tr("api_preset.error.missing_key.body"),
            )
            self.api_key_edit.setFocus()
            return
        self.accept()

    def preset(self) -> ApiPreset:
        return ApiPreset(
            preset_id=self._preset_id,
            name=self.name_edit.text().strip(),
            provider=str(self.provider_combo.currentData()),
            credential_ref=self._credential_ref,
        )

    def api_key(self) -> str:
        return self.api_key_edit.text().strip()


__all__ = ["ApiPresetDialog"]
