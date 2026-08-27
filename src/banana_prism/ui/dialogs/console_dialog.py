"""Settings console with API presets, application preferences, and security.

Expected settings-service surface (all calls are duck typed):

* ``presets -> Iterable[ApiPreset]`` and ``active_preset_id``
* ``upsert_api_preset(...)`` / ``remove_api_preset(id)``
* ``get(key, default)`` / ``set(key, value)``

Password operations use the separately injected ``AuthService`` with
``has_password()``, ``verify_password()`` and ``set_password()``.

Aliases used by older builds are accepted.  Credentials are passed directly to
the service and are never retained in the table model or written to logs.  An
authenticated console may decrypt the selected credential into a short-lived,
masked line edit so the user can reveal or copy it explicitly.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from banana_prism.constants import API_PROVIDERS
from banana_prism.i18n import tr, user_error_text
from banana_prism.models import ApiPreset
from banana_prism.ui.dialogs.api_preset_dialog import ApiPresetDialog
from banana_prism.ui.dialogs.password_dialog import PasswordDialog


def _normalise_preset(value: Any) -> ApiPreset | None:
    if isinstance(value, ApiPreset):
        return value
    if isinstance(value, dict):
        try:
            return ApiPreset(
                preset_id=str(value["preset_id"]),
                name=str(value.get("name") or value["preset_id"]),
                provider=str(value["provider"]),
                credential_ref=str(value.get("credential_ref") or f"api:{value['preset_id']}"),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None


class ConsoleDialog(QDialog):
    presetsChanged = Signal()
    settingsChanged = Signal()

    def __init__(
        self,
        settings_service: Any = None,
        auth_service: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("consoleDialog")
        self.setWindowTitle(tr("console.title"))
        self.resize(760, 560)
        self._settings = settings_service
        self._auth = auth_service
        self._presets: list[ApiPreset] = []
        self._active_id = ""

        self.tabs = QTabWidget()
        self.tabs.setObjectName("consoleTabs")
        self.tabs.addTab(self._build_presets_tab(), tr("console.tab.api"))
        self.tabs.addTab(self._build_statistics_tab(), tr("console.tab.statistics"))
        self.tabs.addTab(self._build_settings_tab(), tr("console.tab.settings"))
        self.tabs.addTab(self._build_system_tab(), tr("console.tab.system"))

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("console.button.apply"))
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(tr("common.close"))
        buttons.button(QDialogButtonBox.StandardButton.Save).clicked.connect(self.apply_settings)
        buttons.rejected.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(buttons)
        self.setStyleSheet(
            """
            QDialog { background: #0b1220; color: #dce6f8; }
            QTabWidget::pane { border: 1px solid #2c3b59; border-radius: 8px; background: #101827; }
            QTabBar::tab { background: #121c2e; padding: 9px 16px; color: #9cabc5; }
            QTabBar::tab:selected { color: #f5d547; border-bottom: 2px solid #f5d547; }
            QLineEdit, QTableWidget { background: #090f1b; border: 1px solid #2c3b59; border-radius: 7px; padding: 6px; }
            """
        )
        self.reload()

    def _build_presets_tab(self) -> QWidget:
        tab = QWidget()
        self.preset_table = QTableWidget(0, 4)
        self.preset_table.setObjectName("presetTable")
        self.preset_table.setHorizontalHeaderLabels(
            (
                tr("console.table.current"),
                tr("console.table.name"),
                tr("console.table.provider"),
                tr("console.table.credential"),
            )
        )
        self.preset_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.preset_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.preset_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.preset_table.verticalHeader().setVisible(False)
        self.preset_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.preset_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preset_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.preset_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.preset_table.doubleClicked.connect(self._edit_preset)
        self.preset_table.currentCellChanged.connect(self._selected_preset_changed)

        self.api_key_view = QLineEdit()
        self.api_key_view.setObjectName("selectedApiKeyView")
        self.api_key_view.setReadOnly(True)
        self.api_key_view.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_view.setPlaceholderText(tr("console.api.select_preset"))
        self.reveal_api_key_button = QPushButton(tr("console.api.show"))
        self.reveal_api_key_button.setObjectName("revealApiKeyButton")
        self.reveal_api_key_button.setCheckable(True)
        self.reveal_api_key_button.setEnabled(False)
        self.reveal_api_key_button.toggled.connect(self._set_api_key_visible)
        self.copy_api_key_button = QPushButton(tr("common.copy"))
        self.copy_api_key_button.setObjectName("copyApiKeyButton")
        self.copy_api_key_button.setEnabled(False)
        self.copy_api_key_button.clicked.connect(self._copy_api_key)
        credential_row = QHBoxLayout()
        credential_row.addWidget(self.api_key_view, 1)
        credential_row.addWidget(self.reveal_api_key_button)
        credential_row.addWidget(self.copy_api_key_button)
        self.api_key_status = QLabel(tr("console.api.select_preset"))
        self.api_key_status.setObjectName("selectedApiKeyStatus")
        self.api_key_status.setWordWrap(True)
        self.api_key_status.setProperty("hint", True)

        add_button = QPushButton(tr("console.button.add"))
        add_button.setObjectName("addPresetButton")
        edit_button = QPushButton(tr("console.button.edit"))
        delete_button = QPushButton(tr("console.button.delete"))
        active_button = QPushButton(tr("console.button.make_current"))
        add_button.clicked.connect(self._add_preset)
        edit_button.clicked.connect(self._edit_preset)
        delete_button.clicked.connect(self._delete_preset)
        active_button.clicked.connect(self._set_active)
        actions = QHBoxLayout()
        actions.addWidget(add_button)
        actions.addWidget(edit_button)
        actions.addWidget(delete_button)
        actions.addStretch(1)
        actions.addWidget(active_button)
        password_row = QHBoxLayout()
        password_note = QLabel(tr("console.password.note"))
        password_note.setProperty("hint", True)
        change_password = QPushButton(tr("console.password.change"))
        change_password.setObjectName("changePasswordButton")
        change_password.clicked.connect(self._change_password)
        password_row.addWidget(password_note, 1)
        password_row.addWidget(change_password)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.addWidget(QLabel(tr("console.presets.note")))
        layout.addWidget(self.preset_table, 1)
        layout.addWidget(QLabel(tr("console.api.selected_key")))
        layout.addLayout(credential_row)
        layout.addWidget(self.api_key_status)
        layout.addLayout(actions)
        layout.addLayout(password_row)
        return tab

    def _build_statistics_tab(self) -> QWidget:
        tab = QWidget()
        self.today_count_label = QLabel(tr("console.statistics.zero"))
        self.today_count_label.setObjectName("todayGenerationCount")
        self.today_count_label.setStyleSheet("font-size: 42px; font-weight: 800; color: #f5d547")
        caption = QLabel(tr("console.statistics.caption"))
        caption.setStyleSheet("color: #94a4c0")
        refresh = QPushButton(tr("console.statistics.refresh"))
        refresh.setObjectName("refreshStatisticsButton")
        refresh.clicked.connect(self._refresh_statistics)
        note = QLabel(tr("console.statistics.note"))
        note.setWordWrap(True)
        note.setProperty("hint", True)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.addWidget(self.today_count_label, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(caption, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(refresh, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(20)
        layout.addWidget(note)
        layout.addStretch(1)
        return tab

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setObjectName("outputDirectoryEdit")
        browse = QPushButton(tr("console.output.browse"))
        browse.clicked.connect(self._browse_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_dir_edit, 1)
        output_row.addWidget(browse)
        self.prompt_length_spin = QSpinBox()
        self.prompt_length_spin.setObjectName("promptFilenameLengthSpin")
        self.prompt_length_spin.setRange(10, 100)
        self.prompt_length_spin.setSuffix(tr("console.output.character_suffix"))
        autosave_note = QLabel(tr("console.output.autosave_note"))
        autosave_note.setWordWrap(True)
        autosave_note.setProperty("hint", True)
        form = QFormLayout()
        form.addRow(tr("console.output.directory"), output_row)
        form.addRow(tr("console.output.filename_length"), self.prompt_length_spin)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addLayout(form)
        layout.addWidget(autosave_note)
        layout.addStretch(1)
        return tab

    def _build_system_tab(self) -> QWidget:
        tab = QWidget()
        self._system_path_labels: dict[str, QLabel] = {}
        form = QFormLayout()
        for key, title_key in (
            ("data", "console.system.user_data"),
            ("logs", "console.system.logs"),
            ("images", "console.system.images"),
        ):
            label = QLabel()
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setWordWrap(True)
            self._system_path_labels[key] = label
            open_button = QPushButton(tr("common.open"))
            open_button.setObjectName(f"open_{key}_directory")
            open_button.clicked.connect(lambda _checked=False, name=key: self._open_system_path(name))
            row = QHBoxLayout()
            row.addWidget(label, 1)
            row.addWidget(open_button)
            form.addRow(tr(title_key), row)
        note = QLabel(tr("console.system.note"))
        note.setWordWrap(True)
        note.setProperty("hint", True)
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addStretch(1)
        return tab

    def _service_value(self, key: str, default: Any = None) -> Any:
        service = self._settings
        if service is None:
            return default
        for name in ("get", "value", "get_setting"):
            method = getattr(service, name, None)
            if callable(method):
                try:
                    return method(key, default)
                except TypeError:
                    try:
                        value = method(key)
                        return default if value is None else value
                    except (TypeError, KeyError, AttributeError):
                        continue
        values = getattr(service, "settings", None)
        if isinstance(values, dict):
            return values.get(key, default)
        return getattr(service, key, default)

    def _service_set(self, key: str, value: Any) -> bool:
        service = self._settings
        if service is None:
            return False
        signature_error: TypeError | None = None
        for name in ("set", "set_value", "set_setting"):
            method = getattr(service, name, None)
            if callable(method):
                try:
                    method(key, value)
                    return True
                except TypeError as exc:
                    signature_error = exc
                    continue
                except (ValueError, RuntimeError, OSError):
                    raise
        values = getattr(service, "settings", None)
        if isinstance(values, dict):
            values[key] = value
            return True
        if signature_error is not None:
            raise signature_error
        return False

    def _call(self, names: tuple[str, ...], variants: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...]) -> Any:
        if self._settings is None:
            return None
        for name in names:
            method = getattr(self._settings, name, None)
            if not callable(method):
                continue
            for args, kwargs in variants:
                try:
                    return method(*args, **kwargs)
                except TypeError:
                    continue
        return None

    def _call_required(
        self,
        names: tuple[str, ...],
        variants: tuple[tuple[tuple[Any, ...], dict[str, Any]], ...],
    ) -> Any:
        """Invoke a required settings mutation or report an unavailable service.

        Unlike ``_call``, this helper distinguishes a successful method that
        returns ``None`` from the absence of a compatible mutation method.
        """

        if self._settings is None:
            raise RuntimeError(tr("console.settings.service_unavailable"))
        signature_error: TypeError | None = None
        found_callable = False
        for name in names:
            method = getattr(self._settings, name, None)
            if not callable(method):
                continue
            found_callable = True
            for args, kwargs in variants:
                try:
                    return method(*args, **kwargs)
                except TypeError as exc:
                    signature_error = exc
                    continue
        if signature_error is not None:
            raise signature_error
        if found_callable:
            raise RuntimeError(tr("console.settings.service_unavailable"))
        raise RuntimeError(tr("console.settings.service_unavailable"))

    def reload(self) -> None:
        raw: Iterable[Any] = ()
        if self._settings is not None:
            for name in ("list_presets", "get_presets", "list_api_presets"):
                method = getattr(self._settings, name, None)
                if callable(method):
                    try:
                        raw = method() or ()
                        break
                    except TypeError:
                        continue
            else:
                raw = getattr(self._settings, "presets", ()) or ()
        presets = [_normalise_preset(item) for item in raw]
        self._presets = [item for item in presets if item is not None]
        active = self._service_value("active_preset_id", "")
        for name in ("get_active_preset_id", "active_preset_id"):
            method = getattr(self._settings, name, None) if self._settings is not None else None
            if callable(method):
                try:
                    active = method()
                    break
                except TypeError:
                    pass
        self._active_id = str(active or "")
        self.output_dir_edit.setText(
            str(self._service_value("save_dir", self._service_value("output_directory", "")) or "")
        )
        self.prompt_length_spin.setValue(int(self._service_value("prompt_save_length", 30) or 30))
        self._refresh_statistics()
        self._refresh_system_paths()
        self._refresh_table()

    def presets(self) -> tuple[ApiPreset, ...]:
        return tuple(self._presets)

    def active_preset_id(self) -> str:
        return self._active_id

    def _refresh_table(self) -> None:
        selected_id = self._selected_preset().preset_id if self._selected_preset() else self._active_id
        self._clear_api_key_display(tr("console.api.select_preset"))
        self.preset_table.setRowCount(len(self._presets))
        selected_row = -1
        for row, preset in enumerate(self._presets):
            provider = API_PROVIDERS.get(preset.provider)
            values = (
                tr("console.active_marker") if preset.preset_id == self._active_id else "",
                preset.name,
                provider.label if provider else preset.provider,
                tr("common.saved_securely")
                if preset.credential_ref
                else tr("common.not_configured"),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, preset.preset_id)
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.preset_table.setItem(row, column, item)
            if preset.preset_id == selected_id:
                selected_row = row
        if selected_row < 0 and self._presets:
            selected_row = 0
        if selected_row >= 0:
            self.preset_table.selectRow(selected_row)

    def _selected_preset(self) -> ApiPreset | None:
        row = self.preset_table.currentRow()
        return self._presets[row] if 0 <= row < len(self._presets) else None

    def _credential_present(self, preset: ApiPreset) -> bool:
        result = self._call(
            ("has_credential", "has_api_key"),
            (((preset.credential_ref,), {}), ((preset.preset_id,), {})),
        )
        return bool(result) if result is not None else bool(preset.credential_ref)

    def _clear_api_key_display(self, status: str) -> None:
        self.reveal_api_key_button.setChecked(False)
        self.api_key_view.clear()
        self.api_key_view.setEchoMode(QLineEdit.EchoMode.Password)
        self.reveal_api_key_button.setText(tr("console.api.show"))
        self.reveal_api_key_button.setEnabled(False)
        self.copy_api_key_button.setEnabled(False)
        self.api_key_status.setText(status)

    def _selected_preset_changed(self, *_args: Any) -> None:
        preset = self._selected_preset()
        self._clear_api_key_display(tr("console.api.select_preset"))
        if preset is None:
            return
        method = getattr(self._settings, "get_api_key", None) if self._settings is not None else None
        if not callable(method):
            self.api_key_status.setText(tr("console.api.read_unavailable"))
            return
        try:
            value = method(preset.preset_id)
        except Exception:
            # Do not expose provider/DPAPI diagnostics here: they may contain
            # sensitive context and selection should remain non-disruptive.
            self.api_key_status.setText(tr("console.api.decrypt_failed"))
            return
        api_key = value if isinstance(value, str) else ""
        if not api_key:
            self.api_key_status.setText(tr("console.api.not_configured"))
            return
        self.api_key_view.setText(api_key)
        self.reveal_api_key_button.setEnabled(True)
        self.copy_api_key_button.setEnabled(True)
        self.api_key_status.setText(tr("console.api.loaded_masked"))

    def _set_api_key_visible(self, visible: bool) -> None:
        self.api_key_view.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        self.reveal_api_key_button.setText(
            tr("console.api.hide" if visible else "console.api.show")
        )

    def _copy_api_key(self) -> None:
        api_key = self.api_key_view.text()
        if not api_key:
            return
        QApplication.clipboard().setText(api_key)
        self.api_key_status.setText(tr("console.api.copied"))

    def done(self, result: int) -> None:
        # Limit the plaintext lifetime to this authenticated console session.
        self._clear_api_key_display("")
        super().done(result)

    def _add_preset(self) -> None:
        dialog = ApiPresetDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._save_preset(dialog.preset(), dialog.api_key())

    def _edit_preset(self, *_args: Any) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        dialog = ApiPresetDialog(self, preset=preset, credential_present=self._credential_present(preset))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._save_preset(dialog.preset(), dialog.api_key())

    def _save_preset(self, preset: ApiPreset, api_key: str) -> None:
        try:
            self._call(
                ("upsert_api_preset", "upsert_preset", "save_preset", "save_api_preset"),
                (
                    (
                        (),
                        {
                            "preset_id": preset.preset_id,
                            "name": preset.name,
                            "provider": preset.provider,
                            "api_key": api_key or None,
                        },
                    ),
                    ((preset,), {"api_key": api_key}),
                    ((preset, api_key), {}),
                    ((asdict(preset), api_key), {}),
                    ((preset,), {}),
                ),
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                tr("console.error.save.title"),
                user_error_text(exc),
            )
            return
        for index, existing in enumerate(self._presets):
            if existing.preset_id == preset.preset_id:
                self._presets[index] = preset
                break
        else:
            self._presets.append(preset)
        if not self._active_id:
            self._active_id = preset.preset_id
            self._call(("set_active_preset", "set_active_preset_id"), (((self._active_id,), {}),))
        self._refresh_table()
        self.presetsChanged.emit()

    def _delete_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        if QMessageBox.question(
            self,
            tr("console.delete.title"),
            tr("console.delete.body", name=preset.name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._call_required(
                ("remove_api_preset", "delete_preset", "delete_api_preset"),
                (((preset.preset_id,), {}), ((preset,), {})),
            )
        except Exception as exc:
            self.reload()
            QMessageBox.critical(
                self,
                tr("console.error.save.title"),
                tr("console.preset.delete_failed", error=user_error_text(exc)),
            )
            return
        # The production service updates active_preset_id atomically with the
        # deletion.  Reloading makes its committed state authoritative and also
        # keeps compatible service implementations in sync.
        self.reload()
        self._refresh_table()
        self.presetsChanged.emit()

    def _set_active(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            return
        try:
            methods = ("set_active_preset", "set_active_preset_id")
            if any(callable(getattr(self._settings, name, None)) for name in methods):
                self._call_required(methods, (((preset.preset_id,), {}),))
            elif not self._service_set("active_preset_id", preset.preset_id):
                raise RuntimeError(tr("console.settings.service_unavailable"))
        except Exception as exc:
            self.reload()
            QMessageBox.critical(
                self,
                tr("console.error.save.title"),
                tr("console.preset.activate_failed", error=user_error_text(exc)),
            )
            return
        self.reload()
        self._refresh_table()
        self.presetsChanged.emit()

    def _browse_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            tr("console.output.select_title"),
            self.output_dir_edit.text(),
        )
        if directory:
            self.output_dir_edit.setText(directory)

    def apply_settings(self) -> None:
        values = {
            "save_dir": self.output_dir_edit.text().strip(),
            "prompt_save_length": self.prompt_length_spin.value(),
        }
        try:
            grouped = None
            if self._settings is not None:
                grouped = getattr(self._settings, "set_many", None) or getattr(
                    self._settings, "update_settings", None
                )
            if callable(grouped):
                grouped(values)
            else:
                previous = {
                    key: self._service_value(key)
                    for key in values
                }
                changed: list[str] = []
                try:
                    for key, value in values.items():
                        if not self._service_set(key, value):
                            raise RuntimeError(tr("console.settings.service_unavailable"))
                        changed.append(key)
                    saver = (
                        getattr(self._settings, "save", None)
                        if self._settings is not None
                        else None
                    )
                    if callable(saver):
                        saver()
                except Exception:
                    for key in reversed(changed):
                        try:
                            self._service_set(key, previous[key])
                        except Exception:
                            pass
                    raise
        except Exception as exc:
            QMessageBox.critical(
                self,
                tr("console.error.save.title"),
                tr("console.settings.save_failed", error=user_error_text(exc)),
            )
            return
        self._refresh_system_paths()
        self.settingsChanged.emit()

    def _refresh_statistics(self) -> None:
        value: Any = 0
        service = self._settings
        if service is not None:
            for name in ("get_today_generation_count", "get_today_count"):
                method = getattr(service, name, None)
                if callable(method):
                    try:
                        value = method()
                        break
                    except TypeError:
                        continue
            else:
                value = getattr(service, "today_generation_count", 0)
                if callable(value):
                    value = value()
        try:
            count = max(0, int(value or 0))
        except (TypeError, ValueError):
            count = 0
        self.today_count_label.setText(str(count))

    def _system_paths(self) -> dict[str, Path]:
        data_value = getattr(self._settings, "data_dir", None) if self._settings is not None else None
        if data_value is None:
            settings_path = getattr(self._settings, "path", None) if self._settings is not None else None
            data = Path(settings_path).parent if settings_path else Path()
        else:
            data = Path(data_value)
        image_value = self.output_dir_edit.text().strip() or str(self._service_value("save_dir", "") or "")
        images = Path(image_value) if image_value else data / "images"
        return {"data": data, "logs": data / "logs", "images": images}

    def _refresh_system_paths(self) -> None:
        for key, path in self._system_paths().items():
            self._system_path_labels[key].setText(str(path))

    def _open_system_path(self, key: str) -> None:
        path = self._system_paths().get(key)
        if path is None or not path.is_dir():
            QMessageBox.information(
                self,
                tr("console.directory.missing.title"),
                tr("console.directory.not_created", path=path),
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _change_password(self) -> None:
        dialog = PasswordDialog(
            self,
            title=tr("console.password.change_title"),
            prompt=tr("console.password.change_prompt"),
            confirm=True,
            require_current=True,
            minimum_length=6,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        auth = self._auth
        if auth is None:
            QMessageBox.warning(
                self,
                tr("console.password.service_missing.title"),
                tr("console.password.service_missing.body"),
            )
            return
        try:
            if not bool(auth.verify_password(dialog.current_password())):
                QMessageBox.warning(
                    self,
                    tr("console.password.change_failed.title"),
                    tr("console.password.current_wrong"),
                )
                return
            auth.set_password(dialog.password())
        except Exception as exc:
            QMessageBox.critical(
                self,
                tr("console.password.change_failed.title"),
                user_error_text(exc),
            )
            return
        QMessageBox.information(
            self,
            tr("console.password.changed.title"),
            tr("console.password.changed.body"),
        )


__all__ = ["ConsoleDialog"]
