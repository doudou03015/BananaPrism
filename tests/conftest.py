"""Order-independent Qt test bootstrap."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session", autouse=True)
def _qt_application() -> QApplication:
    """Create QApplication before a service test can create QCoreApplication.

    QWidget construction aborts the process when a preceding test left only a
    QCoreApplication singleton.  A session-wide GUI application makes the test
    suite independent of file order while remaining fully offscreen.
    """

    application = QApplication.instance()
    if application is None:
        application = QApplication([])
    if not isinstance(application, QApplication):
        pytest.fail("Qt was initialised with QCoreApplication before QApplication")
    yield application


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Keep every test away from real BananaPrism and legacy user data."""

    data_dir = tmp_path / "BananaPrismData"
    monkeypatch.setenv("BANANAPRISM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    return data_dir
