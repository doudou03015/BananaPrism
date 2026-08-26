"""Render the SVG brand asset to a multi-resolution Windows ICO."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(ROOT / "resources" / "banana_prism.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid BananaPrism SVG")

    # Qt's ICO writer keeps the largest frame; a 256px source contains enough detail
    # for Windows to generate shell thumbnails while the SVG remains the source asset.
    image = QImage(QSize(256, 256), QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    png = ROOT / "resources" / "banana_prism.png"
    if not image.save(str(png), "PNG"):
        raise RuntimeError("Unable to write BananaPrism PNG preview")
    target = ROOT / "resources" / "banana_prism.ico"
    with Image.open(png) as source:
        source.save(
            target,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
