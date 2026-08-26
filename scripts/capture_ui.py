"""Capture the real main window offscreen for release visual QA."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault(
    "QT_QPA_PLATFORM",
    os.environ.get("BANANAPRISM_CAPTURE_PLATFORM", "offscreen"),
)
os.environ.setdefault(
    "BANANAPRISM_DATA_DIR",
    str(ROOT / "build" / "ui-qa-profile"),
)

from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from banana_prism.application import build_services, create_main_window  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=ROOT / "build" / "ui-main-window.png",
    )
    options = parser.parse_args()
    output = options.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([sys.argv[0]])
    # Qt's offscreen platform does not enumerate Windows fonts. Register the
    # same system families used by the production stylesheet so a screenshot
    # with tofu boxes can never be mistaken for a passed visual review.
    if app.platformName() == "offscreen" and not QFontDatabase.hasFamily(
        "Microsoft YaHei UI"
    ):
        fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        for filename in ("msyh.ttc", "seguiemj.ttf"):
            path = fonts_dir / filename
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
    if not QFontDatabase.hasFamily("Microsoft YaHei UI"):
        raise RuntimeError("Microsoft YaHei UI is unavailable for visual QA")
    services = build_services()
    try:
        window = create_main_window(services)
        window.show()
        app.processEvents()
        pixmap = window.grab()
        if pixmap.isNull() or not pixmap.save(str(output), "PNG"):
            raise RuntimeError("could not capture the main window")
        window.close()
    finally:
        services.close()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
