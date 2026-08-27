"""Regression tests for accidental combo-box wheel changes."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QComboBox, QScrollArea, QWidget

from banana_prism.ui.dialogs.api_preset_dialog import ApiPresetDialog
from banana_prism.ui.main_window import MainWindow
from banana_prism.ui.widgets.wheel_safe_combo_box import WheelSafeComboBox


def _wheel_event(combo: QComboBox, delta: int = 120) -> QWheelEvent:
    local = QPointF(combo.rect().center())
    return QWheelEvent(
        local,
        QPointF(combo.mapToGlobal(QPoint(int(local.x()), int(local.y())))),
        QPoint(),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )


def test_closed_combo_ignores_wheel_without_changing_selection(
    _qt_application: QApplication,
) -> None:
    combo = WheelSafeComboBox()
    combo.addItems(["one", "two", "three"])
    combo.setCurrentIndex(1)
    combo.show()
    _qt_application.processEvents()

    event = _wheel_event(combo)
    combo.wheelEvent(event)

    assert combo.currentIndex() == 1
    assert not event.isAccepted()
    combo.close()


def test_all_main_window_dropdowns_use_wheel_safe_control() -> None:
    window = MainWindow()

    combos = window.findChildren(QComboBox)
    assert {combo.objectName() for combo in combos} == {
        "presetCombo",
        "modelCombo",
        "sizeCombo",
        "outputFormatCombo",
        "outputDpiCombo",
        "annotationColorCombo",
    }
    assert all(isinstance(combo, WheelSafeComboBox) for combo in combos)
    window.close()


def test_api_preset_provider_dropdown_uses_wheel_safe_control() -> None:
    dialog = ApiPresetDialog()

    assert isinstance(dialog.provider_combo, WheelSafeComboBox)
    dialog.close()


def test_closed_combo_passes_wheel_to_parent_scroll_area(
    _qt_application: QApplication,
) -> None:
    scroll = QScrollArea()
    body = QWidget()
    body.resize(300, 1200)
    combo = WheelSafeComboBox(body)
    combo.setGeometry(20, 400, 180, 30)
    combo.addItems(["one", "two", "three"])
    combo.setCurrentIndex(1)
    scroll.setWidget(body)
    scroll.resize(260, 220)
    scroll.show()
    _qt_application.processEvents()

    bar = scroll.verticalScrollBar()
    bar.setValue(330)
    initial_index = combo.currentIndex()
    initial_scroll = bar.value()
    position = combo.mapTo(scroll, combo.rect().center())
    QTest.wheelEvent(scroll.windowHandle(), position, QPoint(0, -120))
    _qt_application.processEvents()

    assert combo.currentIndex() == initial_index
    assert bar.value() > initial_scroll
    scroll.close()


def test_open_popup_list_still_accepts_wheel(
    _qt_application: QApplication,
) -> None:
    combo = WheelSafeComboBox()
    combo.addItems([f"item-{index}" for index in range(100)])
    combo.resize(180, 30)
    combo.show()
    _qt_application.processEvents()
    combo.showPopup()
    _qt_application.processEvents()

    view = combo.view()
    popup = view.window()
    bar = view.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(0)
    position = view.viewport().mapTo(popup, view.viewport().rect().center())
    QTest.wheelEvent(popup.windowHandle(), position, QPoint(0, -120))
    _qt_application.processEvents()

    assert bar.value() > 0
    combo.hidePopup()
    combo.close()
