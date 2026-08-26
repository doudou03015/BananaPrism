from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "BananaPrismData"
    monkeypatch.setenv("BANANAPRISM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    return data_dir

