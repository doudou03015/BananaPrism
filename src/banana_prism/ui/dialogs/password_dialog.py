"""Password entry/setup dialog used by the encrypted settings console."""

from __future__ import annotations

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

from banana_prism.i18n import tr


class PasswordDialog(QDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str | None = None,
        prompt: str | None = None,
        confirm: bool = False,
        require_current: bool = False,
        minimum_length: int = 1,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("passwordDialog")
        self.setWindowTitle(title or tr("password.title.security"))
        self.setModal(True)
        self.setMinimumWidth(390)
        self._confirm_required = confirm
        self._current_required = require_current
        self._minimum_length = max(1, int(minimum_length))

        description = QLabel(prompt or tr("password.prompt.unlock_credentials"))
        description.setWordWrap(True)
        description.setObjectName("passwordPrompt")
        self.current_edit = QLineEdit()
        self.current_edit.setObjectName("currentPasswordEdit")
        self.current_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.current_edit.setPlaceholderText(tr("password.placeholder.current"))
        self.password_edit = QLineEdit()
        self.password_edit.setObjectName("passwordEdit")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText(tr("password.placeholder.main"))
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setObjectName("confirmPasswordEdit")
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_edit.setPlaceholderText(tr("password.placeholder.again"))

        form = QFormLayout()
        if require_current:
            form.addRow(tr("password.field.current"), self.current_edit)
        form.addRow(
            tr("password.field.new" if confirm else "password.field.password"),
            self.password_edit,
        )
        if confirm:
            form.addRow(tr("password.field.confirm"), self.confirm_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("common.confirm"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("common.cancel"))
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(description)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setStyleSheet(
            """
            QDialog { background: #0d1422; color: #dce5f6; }
            QLineEdit { background: #0a101c; border: 1px solid #33415e; border-radius: 7px; padding: 8px; }
            """
        )

    def _validate_and_accept(self) -> None:
        if self._current_required and not self.current_edit.text():
            QMessageBox.warning(
                self,
                tr("password.error.missing.title"),
                tr("password.error.current_missing"),
            )
            self.current_edit.setFocus()
            return
        if not self.password_edit.text():
            QMessageBox.warning(
                self,
                tr("password.error.missing.title"),
                tr("password.error.empty"),
            )
            self.password_edit.setFocus()
            return
        if len(self.password_edit.text()) < self._minimum_length:
            QMessageBox.warning(
                self,
                tr("password.error.short.title"),
                tr("password.error.short.body", minimum_length=self._minimum_length),
            )
            self.password_edit.setFocus()
            return
        if self._confirm_required and self.password_edit.text() != self.confirm_edit.text():
            QMessageBox.warning(
                self,
                tr("password.error.mismatch.title"),
                tr("password.error.mismatch.body"),
            )
            self.confirm_edit.selectAll()
            self.confirm_edit.setFocus()
            return
        self.accept()

    def password(self) -> str:
        return self.password_edit.text()

    def current_password(self) -> str:
        return self.current_edit.text()

    @classmethod
    def get_password(
        cls,
        parent: QWidget | None = None,
        *,
        title: str | None = None,
        prompt: str | None = None,
    ) -> tuple[str, bool]:
        dialog = cls(
            parent,
            title=title or tr("password.title.security"),
            prompt=prompt or tr("password.prompt.enter"),
        )
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return (dialog.password() if accepted else "", accepted)


__all__ = ["PasswordDialog"]
