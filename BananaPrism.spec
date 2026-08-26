# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata


root = Path(SPEC).resolve().parent
datas = [
    # The frozen package reads the project version from the same immutable
    # source used by development and the PE resource generator.
    (str(root / "pyproject.toml"), "."),
    (str(root / "resources" / "banana_prism.svg"), "resources"),
    (str(root / "resources" / "banana_prism.ico"), "resources"),
    (str(root / "resources" / "banana_prism.png"), "resources"),
]
try:
    datas += copy_metadata("banana-prism")
except Exception:
    pass

a = Analysis(
    [str(root / "src" / "banana_prism" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=["PySide6.QtSvg"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DRender",
        "PySide6.QtBluetooth",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtGraphs",
        "PySide6.QtMultimedia",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtPositioning",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtSpatialAudio",
        "PySide6.QtTextToSpeech",
        "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebSockets",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BananaPrism",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "resources" / "banana_prism.ico"),
    version=str(root / "build" / "version_info.txt"),
    contents_directory="_internal",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="BananaPrism",
)
