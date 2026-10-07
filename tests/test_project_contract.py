from __future__ import annotations

import tomllib
from pathlib import Path

from PIL import Image

from banana_prism import __version__
from banana_prism.constants import DEFAULT_MODEL_INDEX, MODELS, RATIOS, SIZES, SIZE_DIMENSIONS


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_version_comes_from_project_metadata():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        expected = tomllib.load(stream)["project"]["version"]
    assert __version__ == expected


def test_frozen_spec_bundles_the_version_source():
    spec = (ROOT / "BananaPrism.spec").read_text(encoding="utf-8")
    assert 'root / "pyproject.toml"' in spec
    verifier = (ROOT / "scripts" / "verify_package.py").read_text(encoding="utf-8")
    assert 'BANANAPRISM_DATA_DIR' in verifier
    assert '"--smoke-test"' in verifier


def test_builtin_models_preserve_persisted_indices_and_append_nano_banana_21():
    assert [model.model_id for model in MODELS] == [
        "google/gemini-3.1-flash-image",
        "google/gemini-2.5-flash-image",
        "google/gemini-3-pro-image",
        "google/gemini-nano-banana-2.1",
    ]
    assert MODELS[DEFAULT_MODEL_INDEX].model_id == "google/gemini-nano-banana-2.1"
    assert MODELS[DEFAULT_MODEL_INDEX].supported_sizes == SIZES


def test_all_size_ratio_combinations_have_dimensions():
    assert tuple(SIZE_DIMENSIONS) == SIZES
    for size in SIZES:
        assert set(SIZE_DIMENSIONS[size]) == set(RATIOS)
        assert all(width > 0 and height > 0 for width, height in SIZE_DIMENSIONS[size].values())


def test_windows_icon_contains_expected_resolutions():
    with Image.open(ROOT / "resources" / "banana_prism.ico") as icon:
        assert icon.ico.sizes() == {
            (16, 16), (24, 24), (32, 32), (48, 48),
            (64, 64), (128, 128), (256, 256),
        }
