from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from banana_prism.ui.widgets.image_preview import ImagePreview


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _image(width: int = 512, height: int = 384) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0))
    return image


def _stroke(preview: ImagePreview, point: QPointF, *, tool: str, radius: int) -> None:
    canvas = preview.canvas
    canvas.set_tool(tool)
    canvas.set_brush_radius(radius)
    canvas._stroke_tiles.clear()
    canvas._stroke_last = None
    canvas._stroke_to(point)
    canvas._finish_stroke()


def test_annotation_is_yellow_lossless_png_and_tile_undo(app: QApplication) -> None:
    preview = ImagePreview()
    assert preview.set_image(_image())
    _stroke(preview, QPointF(50, 60), tool="brush", radius=12)

    assert preview.has_selection()
    annotated = preview.get_annotated_qimage()
    pixel = annotated.pixelColor(50, 60)
    assert pixel.red() > pixel.green() > pixel.blue()
    encoded = preview.get_annotated_bytes()
    assert encoded is not None
    assert encoded.startswith(b"\x89PNG\r\n\x1a\n")

    command = preview.canvas._undo[-1]
    changed_pixels = sum(rect.width() * rect.height() for rect, _ in command.patches)
    assert changed_pixels < preview.image().width() * preview.image().height()
    assert preview.undo()
    assert not preview.has_selection()


def test_full_erase_clears_selection_even_when_mask_object_remains(app: QApplication) -> None:
    preview = ImagePreview()
    preview.set_image(_image(128, 128))
    _stroke(preview, QPointF(64, 64), tool="brush", radius=8)
    assert preview.has_selection()

    _stroke(preview, QPointF(64, 64), tool="eraser", radius=18)
    assert not preview.canvas._mask.isNull()
    assert not preview.has_selection()

    assert preview.undo()
    assert preview.has_selection()


def test_new_image_and_clear_release_annotation_history(app: QApplication) -> None:
    preview = ImagePreview()
    preview.set_image(_image(256, 256))
    _stroke(preview, QPointF(30, 30), tool="brush", radius=7)
    assert preview.canvas._undo
    preview.clear_selection()
    assert not preview.has_selection()
    assert not preview.canvas._undo


def test_rectangle_uses_the_single_mask_and_can_be_fully_erased(app: QApplication) -> None:
    preview = ImagePreview()
    preview.set_image(_image(64, 64))
    assert preview.canvas._commit_rectangle(QRect(12, 12, 16, 16))
    assert preview.has_selection()
    annotated = preview.get_annotated_qimage()
    pixel = annotated.pixelColor(18, 18)
    assert pixel.red() > pixel.green() > pixel.blue()

    _stroke(preview, QPointF(20, 20), tool="eraser", radius=24)
    assert not preview.has_selection()
    assert preview.undo()
    assert preview.has_selection()
    assert preview.undo()
    assert not preview.has_selection()


def test_rectangle_drag_includes_bottom_right_image_pixel(app: QApplication) -> None:
    preview = ImagePreview()
    preview.set_image(_image(64, 64))
    canvas = preview.canvas
    canvas.set_tool("rect")
    canvas.show()

    QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(0, 0))
    QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(63, 63))

    assert preview.has_selection()
    assert canvas._mask.pixelColor(63, 63).alpha() > 0


def test_4k_undo_history_stores_bounded_tiles_not_full_images(app: QApplication) -> None:
    preview = ImagePreview()
    preview.set_image(_image(4096, 4096))
    for index in range(30):
        point = QPointF(40 + (index % 10) * 360, 40 + (index // 10) * 800)
        _stroke(preview, point, tool="brush", radius=10)

    stored_bytes = sum(
        before.sizeInBytes()
        for command in preview.canvas._undo
        for _rect, before in command.patches
    )
    assert len(preview.canvas._undo) == 30
    assert stored_bytes < 16 * 1024 * 1024

    _stroke(preview, QPointF(40, 40), tool="brush", radius=7)
    preview.set_image(_image(64, 64))
    assert not preview.has_selection()
    assert not preview.canvas._undo
