"""Collapsible panel for text returned alongside (or instead of) an image."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from banana_prism.i18n import tr


class TextResultPanel(QFrame):
    expandedChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("textResultPanel")
        self._expanded = True
        self._toggle = QPushButton(tr("text_panel.toggle.expanded"))
        self._toggle.setObjectName("toggleTextResultButton")
        self._toggle.setFixedWidth(28)
        self._toggle.clicked.connect(lambda: self.set_expanded(not self._expanded))
        title = QLabel(tr("text_panel.title"))
        title.setObjectName("panelTitle")
        copy_button = QPushButton(tr("common.copy"))
        copy_button.setObjectName("copyTextResultButton")
        copy_button.clicked.connect(self.copy)
        clear_button = QPushButton(tr("common.clear"))
        clear_button.clicked.connect(self.clear)

        header = QHBoxLayout()
        header.addWidget(self._toggle)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(copy_button)
        header.addWidget(clear_button)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("textResultView")
        self.editor.setReadOnly(True)
        self.editor.setPlaceholderText(tr("text_panel.placeholder"))
        self.editor.setMaximumBlockCount(2_000)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self.editor)
        self.setStyleSheet(
            """
            QFrame#textResultPanel { background: #101827; border: 1px solid #293752; border-radius: 10px; }
            QLabel#panelTitle { color: #edf2ff; font-weight: 650; }
            QPlainTextEdit#textResultView { background: #090f1c; color: #c8d2e7; border: 1px solid #24314a;
                                           border-radius: 7px; padding: 7px; }
            """
        )

    def set_text(self, text: str, *, expand: bool = True) -> None:
        self.editor.setPlainText(text or "")
        if expand:
            self.set_expanded(True)

    def append_text(self, text: str) -> None:
        self.editor.appendPlainText(text)

    def text(self) -> str:
        return self.editor.toPlainText()

    def clear(self) -> None:
        self.editor.clear()

    def copy(self) -> None:
        QApplication.clipboard().setText(self.text())

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.editor.setVisible(expanded)
        self._toggle.setText(
            tr("text_panel.toggle.expanded" if expanded else "text_panel.toggle.collapsed")
        )
        self.expandedChanged.emit(expanded)


__all__ = ["TextResultPanel"]
