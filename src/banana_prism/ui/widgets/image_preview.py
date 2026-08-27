"""Zoomable image preview and lightweight local-edit annotation canvas.

The canvas keeps annotations in original-image coordinates.  Brush history is
stored as changed 128 px tiles rather than full-size ARGB image snapshots, so a
4K source does not multiply its memory footprint for every undo step.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, hypot
from typing import Literal

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QImage,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from banana_prism.constants import (
    ANNOTATION_COLORS,
    DEFAULT_ANNOTATION_COLOR,
    DEFAULT_BRUSH_RADIUS,
    MAX_UNDO_COMMANDS,
    ZOOM_MAX,
    ZOOM_MIN,
    ZOOM_STEP,
)
from banana_prism.i18n import tr

EditTool = Literal["view", "rect", "brush", "eraser"]
_TILE_SIZE = 128
_ALPHA_TO_BINARY = bytes(0 if value == 0 else 255 for value in range(256))


@dataclass(slots=True)
class _MaskUndoCommand:
    patches: tuple[tuple[QRect, QImage], ...]

    def undo(self, canvas: "_ImageCanvas") -> None:
        painter = QPainter(canvas._mask)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        for rect, before in self.patches:
            painter.drawImage(rect.topLeft(), before)
        painter.end()


class _ImageCanvas(QWidget):
    selectionChanged = Signal(bool)
    undoAvailableChanged = Signal(bool)
    zoomChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._image = QImage()
        self._mask = QImage()
        self._mask_has_pixels = False
        self._undo: list[_MaskUndoCommand] = []
        self._stroke_tiles: dict[tuple[int, int], tuple[QRect, QImage]] = {}
        self._stroke_last: QPointF | None = None
        self._rect_start: QPointF | None = None
        self._rect_current: QPointF | None = None
        self._drawing = False
        self._panning = False
        self._pan_start_global = QPointF()
        self._pan_start_scroll = (0, 0)
        self._scroll_area: QScrollArea | None = None
        self._tool: EditTool = "view"
        self._brush_radius = DEFAULT_BRUSH_RADIUS
        self._annotation_color_id = DEFAULT_ANNOTATION_COLOR
        color = ANNOTATION_COLORS[self._annotation_color_id]
        self._annotation_color = color.fill_rgba
        self._annotation_border_color = color.border_rgba
        self._zoom = 1.0
        self.setMinimumSize(1, 1)

    @property
    def image(self) -> QImage:
        return self._image

    @property
    def zoom(self) -> float:
        return self._zoom

    def set_image(self, image: QImage) -> None:
        self._image = image.convertToFormat(QImage.Format.Format_ARGB32) if not image.isNull() else QImage()
        self._mask = QImage(self._image.size(), QImage.Format.Format_Alpha8)
        if not self._mask.isNull():
            self._mask.fill(0)
        self._mask_has_pixels = False
        self._undo.clear()
        self._cancel_gesture()
        self._update_canvas_size()
        self.selectionChanged.emit(False)
        self.undoAvailableChanged.emit(False)
        self.update()

    def set_tool(self, tool: EditTool) -> None:
        if tool not in {"view", "rect", "brush", "eraser"}:
            raise ValueError(f"unknown edit tool: {tool}")
        self._tool = tool
        self._cancel_gesture()
        self._update_cursor()
        self.update()

    def set_scroll_area(self, scroll_area: QScrollArea) -> None:
        """Bind the viewport whose scroll bars are controlled by view-mode drag."""

        self._scroll_area = scroll_area
        scroll_area.horizontalScrollBar().rangeChanged.connect(self._update_cursor)
        scroll_area.verticalScrollBar().rangeChanged.connect(self._update_cursor)
        self._update_cursor()

    def _can_pan(self) -> bool:
        if self._image.isNull() or self._scroll_area is None:
            return False
        horizontal = self._scroll_area.horizontalScrollBar()
        vertical = self._scroll_area.verticalScrollBar()
        return horizontal.maximum() > horizontal.minimum() or vertical.maximum() > vertical.minimum()

    def _update_cursor(self, *_range: int) -> None:
        if self._tool == "view":
            if self._panning:
                cursor = Qt.CursorShape.ClosedHandCursor
            elif self._can_pan():
                cursor = Qt.CursorShape.OpenHandCursor
            else:
                cursor = Qt.CursorShape.ArrowCursor
        else:
            cursor = {
                "rect": Qt.CursorShape.CrossCursor,
                "brush": Qt.CursorShape.BlankCursor,
                "eraser": Qt.CursorShape.BlankCursor,
            }[self._tool]
        self.setCursor(cursor)

    def set_brush_radius(self, radius: int) -> None:
        self._brush_radius = max(1, int(radius))
        self.update()

    def set_annotation_color(self, color_id: str) -> None:
        """Select the guide colour used on screen and in the exported guide."""

        normalized = str(color_id).strip().lower()
        try:
            color = ANNOTATION_COLORS[normalized]
        except KeyError as exc:
            raise ValueError(f"unknown annotation colour: {color_id}") from exc
        self._annotation_color_id = normalized
        self._annotation_color = color.fill_rgba
        self._annotation_border_color = color.border_rgba
        self.update()

    @property
    def annotation_color(self) -> str:
        return self._annotation_color_id

    def set_zoom(self, zoom: float) -> None:
        zoom = max(float(ZOOM_MIN), min(float(ZOOM_MAX), float(zoom)))
        if abs(zoom - self._zoom) < 0.0001:
            return
        self._zoom = zoom
        self._update_canvas_size()
        self.zoomChanged.emit(zoom)

    def _update_canvas_size(self) -> None:
        if self._image.isNull():
            self.resize(1, 1)
            return
        self.resize(
            max(1, int(round(self._image.width() * self._zoom))),
            max(1, int(round(self._image.height() * self._zoom))),
        )

    def has_selection(self) -> bool:
        return self._mask_has_pixels

    def clear_selection(self) -> None:
        before = self.has_selection()
        if not self._mask.isNull():
            self._mask.fill(0)
        self._mask_has_pixels = False
        self._undo.clear()
        self._cancel_gesture()
        if before:
            self.selectionChanged.emit(False)
        self.undoAvailableChanged.emit(False)
        self.update()

    def undo(self) -> bool:
        if not self._undo:
            return False
        before = self.has_selection()
        command = self._undo.pop()
        command.undo(self)
        self._mask_has_pixels = self._scan_mask()
        after = self.has_selection()
        if before != after:
            self.selectionChanged.emit(after)
        self.undoAvailableChanged.emit(bool(self._undo))
        self.update()
        return True

    def _push_undo(self, command: _MaskUndoCommand) -> None:
        self._undo.append(command)
        if len(self._undo) > int(MAX_UNDO_COMMANDS):
            del self._undo[: len(self._undo) - int(MAX_UNDO_COMMANDS)]
        self.undoAvailableChanged.emit(True)

    def annotated_image(self) -> QImage:
        if self._image.isNull() or not self.has_selection():
            return QImage()
        result = self._image.convertToFormat(QImage.Format.Format_ARGB32)
        painter = QPainter(result)
        if self._mask_has_pixels:
            red, green, blue, alpha = self._annotation_color
            overlay = QImage(self._image.size(), QImage.Format.Format_ARGB32_Premultiplied)
            overlay.fill(QColor(red, green, blue, 255))
            overlay.setAlphaChannel(self._mask)
            painter.setOpacity(alpha / 255.0)
            painter.drawImage(0, 0, overlay)
            painter.setOpacity(1.0)
        painter.end()
        return result

    def selection_mask_image(self) -> QImage:
        """Return the authoritative full-resolution black/white selection mask."""

        if self._mask.isNull() or not self.has_selection():
            return QImage()
        # Alpha8 and Grayscale8 are both one byte per pixel.  Reinterpret a
        # thresholded copy so antialiased edge pixels are unambiguously selected.
        raw = bytes(self._mask.constBits()).translate(_ALPHA_TO_BINARY)
        return QImage(
            raw,
            self._mask.width(),
            self._mask.height(),
            self._mask.bytesPerLine(),
            QImage.Format.Format_Grayscale8,
        ).copy()

    def _to_image_point(self, widget_point: QPointF) -> QPointF:
        if self._image.isNull():
            return QPointF()
        x = max(0.0, min(self._image.width() - 1.0, widget_point.x() / self._zoom))
        y = max(0.0, min(self._image.height() - 1.0, widget_point.y() / self._zoom))
        return QPointF(x, y)

    def _capture_tiles(self, affected: QRect) -> None:
        affected = affected.intersected(self._mask.rect())
        if affected.isEmpty():
            return
        left = affected.left() // _TILE_SIZE
        right = affected.right() // _TILE_SIZE
        top = affected.top() // _TILE_SIZE
        bottom = affected.bottom() // _TILE_SIZE
        for tile_y in range(top, bottom + 1):
            for tile_x in range(left, right + 1):
                key = (tile_x, tile_y)
                if key in self._stroke_tiles:
                    continue
                rect = QRect(
                    tile_x * _TILE_SIZE,
                    tile_y * _TILE_SIZE,
                    _TILE_SIZE,
                    _TILE_SIZE,
                ).intersected(self._mask.rect())
                self._stroke_tiles[key] = (rect, self._mask.copy(rect))

    def _capture_tiles_for_stamp(self, point: QPointF) -> None:
        radius = self._brush_radius + 2
        self._capture_tiles(
            QRect(
                int(point.x()) - radius,
                int(point.y()) - radius,
                radius * 2 + 1,
                radius * 2 + 1,
            )
        )

    def _commit_rectangle(self, rect: QRect) -> bool:
        """Add a rectangular selection to the same erasable 8-bit mask."""

        rect = rect.intersected(self._mask.rect())
        if rect.width() < 4 or rect.height() < 4:
            return False
        before = self.has_selection()
        self._stroke_tiles.clear()
        self._capture_tiles(rect)
        painter = QPainter(self._mask)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        painter.fillRect(rect, QColor(255, 255, 255, 255))
        painter.end()
        patches = tuple(self._stroke_tiles.values())
        if patches and any(self._mask.copy(area) != previous for area, previous in patches):
            self._push_undo(_MaskUndoCommand(patches))
        self._stroke_tiles.clear()
        self._mask_has_pixels = self._scan_mask()
        if before != self._mask_has_pixels:
            self.selectionChanged.emit(self._mask_has_pixels)
        return True

    def _stamp(self, point: QPointF) -> None:
        self._capture_tiles_for_stamp(point)
        painter = QPainter(self._mask)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._tool == "eraser":
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 0))
        else:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 255))
        painter.drawEllipse(point, self._brush_radius, self._brush_radius)
        painter.end()

    def _stroke_to(self, point: QPointF) -> None:
        if self._stroke_last is None:
            self._stamp(point)
            self._stroke_last = point
            return
        distance = hypot(point.x() - self._stroke_last.x(), point.y() - self._stroke_last.y())
        step = max(1.0, self._brush_radius / 2.0)
        count = max(1, int(ceil(distance / step)))
        start = self._stroke_last
        for index in range(1, count + 1):
            factor = index / count
            self._stamp(
                QPointF(
                    start.x() + (point.x() - start.x()) * factor,
                    start.y() + (point.y() - start.y()) * factor,
                )
            )
        self._stroke_last = point

    def _scan_mask(self) -> bool:
        if self._mask.isNull():
            return False
        # Alpha8 contains no colour channels; zero padding is harmless here.
        return any(bytes(self._mask.constBits()))

    def _finish_stroke(self) -> None:
        if self._stroke_tiles:
            patches = tuple(self._stroke_tiles.values())
            changed = any(self._mask.copy(rect) != before for rect, before in patches)
            if changed:
                self._push_undo(_MaskUndoCommand(patches))
        before = self.has_selection()
        self._mask_has_pixels = self._scan_mask()
        after = self.has_selection()
        if before != after:
            self.selectionChanged.emit(after)
        self._stroke_tiles.clear()
        self._stroke_last = None

    def _cancel_gesture(self) -> None:
        self._drawing = False
        self._panning = False
        self._stroke_tiles.clear()
        self._stroke_last = None
        self._rect_start = None
        self._rect_current = None
        self._update_cursor()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._image.isNull() or event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if self._tool == "view":
            if not self._can_pan() or self._scroll_area is None:
                super().mousePressEvent(event)
                return
            horizontal = self._scroll_area.horizontalScrollBar()
            vertical = self._scroll_area.verticalScrollBar()
            self._panning = True
            self._pan_start_global = event.globalPosition()
            self._pan_start_scroll = (horizontal.value(), vertical.value())
            self._update_cursor()
            event.accept()
            return
        self._drawing = True
        point = self._to_image_point(event.position())
        if self._tool == "rect":
            self._rect_start = point
            self._rect_current = point
        else:
            self._stroke_tiles.clear()
            self._stroke_last = None
            self._stroke_to(point)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._panning and self._scroll_area is not None:
            delta = event.globalPosition() - self._pan_start_global
            horizontal = self._scroll_area.horizontalScrollBar()
            vertical = self._scroll_area.verticalScrollBar()
            horizontal.setValue(self._pan_start_scroll[0] - int(round(delta.x())))
            vertical.setValue(self._pan_start_scroll[1] - int(round(delta.y())))
            event.accept()
            return
        if not self._image.isNull() and self._tool in {"brush", "eraser"}:
            self.update()
        if not self._drawing:
            super().mouseMoveEvent(event)
            return
        point = self._to_image_point(event.position())
        if self._tool == "rect":
            self._rect_current = point
        else:
            self._stroke_to(point)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._panning and event.button() == Qt.MouseButton.LeftButton:
            self._panning = False
            self._update_cursor()
            event.accept()
            return
        if not self._drawing or event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._tool == "rect" and self._rect_start is not None:
            end = self._to_image_point(event.position())
            x1, x2 = sorted((int(round(self._rect_start.x())), int(round(end.x()))))
            y1, y2 = sorted((int(round(self._rect_start.y())), int(round(end.y()))))
            # Both sampled endpoints are pixels, so the rectangle is inclusive.
            # Without the +1, the bottom/right image edges can never be selected.
            rect = QRect(x1, y1, x2 - x1 + 1, y2 - y1 + 1).intersected(
                self._image.rect()
            )
            self._commit_rectangle(rect)
        elif self._tool in {"brush", "eraser"}:
            self._stroke_to(self._to_image_point(event.position()))
            self._finish_stroke()
        self._drawing = False
        self._rect_start = None
        self._rect_current = None
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#090d18"))
        if self._image.isNull():
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.scale(self._zoom, self._zoom)
        painter.drawImage(0, 0, self._image)
        if self._mask_has_pixels or self._drawing:
            red, green, blue, alpha = self._annotation_color
            overlay = QImage(self._image.size(), QImage.Format.Format_ARGB32_Premultiplied)
            overlay.fill(QColor(red, green, blue, 255))
            overlay.setAlphaChannel(self._mask)
            painter.setOpacity(alpha / 255.0)
            painter.drawImage(0, 0, overlay)
            painter.setOpacity(1.0)
        red, green, blue, alpha = self._annotation_border_color
        painter.setPen(QPen(QColor(red, green, blue, alpha), max(1.0, 3.0 / self._zoom)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self._drawing and self._rect_start is not None and self._rect_current is not None:
            painter.drawRect(QRectF(self._rect_start, self._rect_current).normalized())
        painter.resetTransform()
        if self._tool in {"brush", "eraser"} and self.underMouse():
            cursor = self.mapFromGlobal(self.cursor().pos())
            radius = self._brush_radius * self._zoom
            color = (
                QColor(*self._annotation_border_color)
                if self._tool == "brush"
                else QColor("#f7fafc")
            )
            painter.setPen(QPen(color, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(cursor), radius, radius)
        painter.end()


class ImagePreview(QFrame):
    """Composite image viewport used by both generation and edit modes."""

    selectionChanged = Signal(bool)
    imageChanged = Signal(bool)
    zoomChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("imagePreview")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._canvas = _ImageCanvas()
        self._tool: EditTool = "view"

        self._status = QLabel(tr("preview.empty"))
        self._status.setObjectName("previewStatus")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("previewScroll")
        self._scroll.setWidget(self._canvas)
        self._scroll.setWidgetResizable(False)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._canvas.set_scroll_area(self._scroll)

        self._zoom_label = QLabel(tr("preview.zoom.initial"))
        self._zoom_label.setMinimumWidth(48)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        zoom_out = QPushButton(tr("preview.zoom.out"))
        zoom_out.setObjectName("zoomOutButton")
        zoom_fit = QPushButton(tr("preview.zoom.fit"))
        zoom_fit.setObjectName("zoomFitButton")
        zoom_in = QPushButton(tr("preview.zoom.in"))
        zoom_in.setObjectName("zoomInButton")
        for button in (zoom_out, zoom_fit, zoom_in):
            button.setProperty("compact", True)
        zoom_out.clicked.connect(self.zoom_out)
        zoom_fit.clicked.connect(self.fit_to_view)
        zoom_in.clicked.connect(self.zoom_in)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(10, 7, 10, 7)
        toolbar.addWidget(self._status, 1)
        toolbar.addWidget(zoom_out)
        toolbar.addWidget(self._zoom_label)
        toolbar.addWidget(zoom_fit)
        toolbar.addWidget(zoom_in)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(toolbar)
        layout.addWidget(self._scroll, 1)

        self._canvas.selectionChanged.connect(self.selectionChanged)
        self._canvas.zoomChanged.connect(self._on_zoom_changed)
        self.setStyleSheet(
            """
            QFrame#imagePreview { background: #090d18; border: 1px solid #26334e; border-radius: 12px; }
            QLabel#previewStatus { color: #9eabc3; padding: 2px 8px; }
            QScrollArea#previewScroll { background: #090d18; }
            QPushButton[compact="true"] { min-width: 30px; padding: 4px 8px; }
            """
        )

    @property
    def canvas(self) -> _ImageCanvas:
        """Expose the canvas for focused tests and advanced integrations."""
        return self._canvas

    def has_image(self) -> bool:
        return not self._canvas.image.isNull()

    def image(self) -> QImage:
        return self._canvas.image.copy()

    def set_image(self, image: bytes | QImage | QPixmap) -> bool:
        if isinstance(image, bytes):
            qimage = QImage.fromData(image)
        elif isinstance(image, QPixmap):
            qimage = image.toImage()
        else:
            qimage = image
        if qimage.isNull():
            return False
        self._canvas.set_image(qimage)
        self._status.setText(
            tr("preview.dimensions", width=qimage.width(), height=qimage.height())
        )
        self.imageChanged.emit(True)
        self.fit_to_view()
        return True

    # Compatibility aliases for the recovered application vocabulary.
    set_pixmap = set_image

    def clear_image(self) -> None:
        self._canvas.set_image(QImage())
        self._status.setText(tr("preview.empty"))
        self.imageChanged.emit(False)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_mode(self, mode: str) -> None:
        self.set_tool("view" if mode == "view" else self._tool)

    def set_tool(self, tool: EditTool) -> None:
        self._tool = tool
        self._canvas.set_tool(tool)

    def tool(self) -> EditTool:
        return self._tool

    def set_brush_radius(self, radius: int) -> None:
        self._canvas.set_brush_radius(radius)

    def set_annotation_color(self, color_id: str) -> None:
        self._canvas.set_annotation_color(color_id)

    @property
    def annotation_color(self) -> str:
        return self._canvas.annotation_color

    def has_selection(self) -> bool:
        return self._canvas.has_selection()

    def clear_selection(self) -> None:
        self._canvas.clear_selection()

    def undo(self) -> bool:
        return self._canvas.undo()

    def get_annotated_qimage(self) -> QImage:
        return self._canvas.annotated_image()

    def get_annotated_bytes(self) -> bytes | None:
        image = self.get_annotated_qimage()
        if image.isNull():
            return None
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return None
        ok = image.save(buffer, "PNG")
        buffer.close()
        return bytes(data) if ok else None

    def get_selection_mask_qimage(self) -> QImage:
        return self._canvas.selection_mask_image()

    def get_selection_mask_bytes(self) -> bytes | None:
        image = self.get_selection_mask_qimage()
        if image.isNull():
            return None
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return None
        ok = image.save(buffer, "PNG")
        buffer.close()
        return bytes(data) if ok else None

    def get_original_bytes(self) -> bytes | None:
        image = self._canvas.image
        if image.isNull():
            return None
        data = QByteArray()
        buffer = QBuffer(data)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return None
        ok = image.save(buffer, "PNG")
        buffer.close()
        return bytes(data) if ok else None

    def zoom_in(self) -> None:
        self._canvas.set_zoom(self._canvas.zoom * (1.0 + float(ZOOM_STEP)))

    def zoom_out(self) -> None:
        self._canvas.set_zoom(self._canvas.zoom / (1.0 + float(ZOOM_STEP)))

    def set_zoom(self, value: float) -> None:
        self._canvas.set_zoom(value)

    def fit_to_view(self) -> None:
        image = self._canvas.image
        viewport = self._scroll.viewport().size()
        if image.isNull() or viewport.width() <= 1 or viewport.height() <= 1:
            return
        margin = 16
        zoom = min(
            (viewport.width() - margin) / image.width(),
            (viewport.height() - margin) / image.height(),
            1.0,
        )
        self._canvas.set_zoom(max(float(ZOOM_MIN), zoom))

    def _on_zoom_changed(self, value: float) -> None:
        self._zoom_label.setText(tr("preview.zoom.value", percent=value * 100))
        self.zoomChanged.emit(value)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_in() if event.angleDelta().y() > 0 else self.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)


__all__ = ["ImagePreview", "EditTool"]
