"""Combo box that cannot be changed accidentally by a hover wheel gesture."""

from __future__ import annotations

from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QComboBox


class WheelSafeComboBox(QComboBox):
    """Ignore wheel selection changes while the popup list is closed.

    Ignoring the event lets Qt propagate it to a containing scroll area.  The
    popup view still receives wheel events normally after the user explicitly
    opens the list.
    """

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        # The popup's item view receives its own wheel events.  A wheel event
        # delivered to the combo itself is therefore always an accidental
        # hover/focus gesture and should continue to the parent scroll area.
        event.ignore()
