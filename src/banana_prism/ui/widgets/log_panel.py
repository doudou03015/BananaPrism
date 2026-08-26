"""Structured, bounded log viewer for the desktop UI."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget

from banana_prism.i18n import tr


class LogPanel(QFrame):
    """A small HTML log console with copy/clear controls.

    ``append_log(level, message)`` is intentionally compatible with the worker
    ``log(str, str)`` signal.  The widget never writes to disk.
    """

    cleared = Signal()
    MAX_BLOCKS = 1_000

    _COLORS = {
        "debug": "#74819a",
        "info": "#a8b4cb",
        "ok": "#63d9a5",
        "success": "#63d9a5",
        "warn": "#f5d547",
        "warning": "#f5d547",
        "err": "#ff6b7a",
        "error": "#ff6b7a",
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("logPanel")
        title = QLabel(tr("log_panel.title"))
        title.setObjectName("panelTitle")
        copy_button = QPushButton(tr("common.copy"))
        copy_button.setObjectName("copyLogButton")
        clear_button = QPushButton(tr("common.clear"))
        clear_button.setObjectName("clearLogButton")
        copy_button.clicked.connect(self.copy_all)
        clear_button.clicked.connect(self.clear)

        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(copy_button)
        header.addWidget(clear_button)

        self.view = QTextEdit()
        self.view.setObjectName("logView")
        self.view.setReadOnly(True)
        self.view.setAcceptRichText(True)
        self.view.document().setMaximumBlockCount(self.MAX_BLOCKS)
        self.view.setPlaceholderText(tr("log_panel.placeholder"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self.view, 1)
        self.setStyleSheet(
            """
            QFrame#logPanel { background: #101827; border: 1px solid #293752; border-radius: 10px; }
            QLabel#panelTitle { color: #edf2ff; font-weight: 650; }
            QTextEdit#logView { background: #090f1c; color: #a8b4cb; border: 1px solid #24314a;
                                border-radius: 7px; padding: 7px; font-family: Consolas, 'Microsoft YaHei UI'; }
            """
        )

    def append_log(self, level: str, message: str) -> None:
        level_key = (level or "info").lower()
        color = self._COLORS.get(level_key, self._COLORS["info"])
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_message = str(message).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.view.moveCursor(QTextCursor.MoveOperation.End)
        self.view.insertHtml(
            f'<span style="color:#61708d">[{timestamp}]</span> '
            f'<span style="color:{color}">{safe_message}</span><br>'
        )
        self.view.moveCursor(QTextCursor.MoveOperation.End)

    # Short alias used by a few service adapters.
    append = append_log

    def clear(self) -> None:
        self.view.clear()
        self.cleared.emit()

    def copy_all(self) -> None:
        QApplication.clipboard().setText(self.view.toPlainText())

    def text(self) -> str:
        return self.view.toPlainText()


__all__ = ["LogPanel"]
