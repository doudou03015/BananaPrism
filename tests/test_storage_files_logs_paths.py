from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

import banana_prism.services.file_service as file_module
from banana_prism import __version__
from banana_prism.models import (
    EditRequest,
    EditResult,
    GenerationRequest,
    GenerationResult,
    TokenUsage,
)
from banana_prism.services.file_service import (
    FileSaveError,
    FileService,
    sanitize_filename_component,
)
from banana_prism.services.log_service import (
    LogService,
    REDACTION,
    register_process_secret,
)
from banana_prism.utils.paths import (
    DATA_DIR_ENV,
    get_app_data_dir,
    get_resource_path,
)


def encoded_image(
    fmt: str,
    width: int,
    height: int,
    *,
    dpi: float | None = None,
) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#f5d547"))
    if dpi is not None:
        dpm = int(round(dpi / 0.0254))
        image.setDotsPerMeterX(dpm)
        image.setDotsPerMeterY(dpm)
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, fmt.upper(), 95 if fmt.lower() in {"jpg", "jpeg"} else -1)
    buffer.close()
    return bytes(data)


def decoded_dpi(data: bytes) -> tuple[float, float] | None:
    image = QImage.fromData(data)
    assert not image.isNull()
    if image.dotsPerMeterX() <= 0 or image.dotsPerMeterY() <= 0:
        return None
    return image.dotsPerMeterX() * 0.0254, image.dotsPerMeterY() * 0.0254


def generation_result() -> GenerationResult:
    request = GenerationRequest(
        prompt="a luminous banana prism",
        model_id="google/gemini-test-image",
        model_short_name="Nano/Banana",
        size="2K",
        ratio="4:3",
        preset_id="preset_abc",
        provider="openrouter",
    )
    return GenerationResult(
        image_bytes=encoded_image("png", 64, 48, dpi=300.0),
        fmt="png",
        width=64,
        height=48,
        request=request,
        text_content="done",
        usage=TokenUsage(input_tokens=12, output_tokens=3),
        timestamp=datetime(2026, 8, 26, 12, 34, 56),
        output_dpi=(300.0, 300.0),
    )


def edit_result() -> EditResult:
    request = EditRequest(
        source_image_bytes=b"source",
        annotated_image_bytes=b"annotated",
        edit_prompt="add a hat",
        model_id="google/gemini-test-image",
        model_short_name="NanoBanana",
        size="4K",
        ratio="1:1",
        preset_id="preset_xyz",
        provider="aihubmix",
        source_fmt="jpeg",
        source_dpi=(72.0, 72.0),
        wire_source_fmt="png",
        selection_mask_bytes=encoded_image("png", 100, 100),
        annotation_color="green",
        requested_output_format="jpeg",
        requested_dpi=300.0,
    )
    return EditResult(
        image_bytes=encoded_image("jpeg", 100, 100, dpi=300.0),
        fmt="jpeg",
        width=100,
        height=100,
        request=request,
        timestamp=datetime(2026, 8, 26, 12, 35, 0),
        output_dpi=(300.0, 300.0),
    )


def test_image_and_sidecar_commit_include_integrity_and_provenance(tmp_path: Path) -> None:
    service = FileService(save_dir=tmp_path)
    result = generation_result()

    image_path = service.auto_save(result)
    sidecar_path = image_path.with_suffix(".json")
    metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))

    assert image_path.read_bytes() == result.image_bytes
    assert FileService.is_complete(image_path)
    assert metadata["schema_version"] == 1
    assert metadata["app_version"] == __version__
    assert metadata["provider"] == "openrouter"
    assert metadata["preset_id"] == "preset_abc"
    assert metadata["image_sha256"] == hashlib.sha256(result.image_bytes).hexdigest()
    assert metadata["output_dpi"] == [300.0, 300.0]
    assert metadata["requested_output_format"] == "png"
    assert metadata["requested_dpi"] == 300.0
    assert metadata["actual_output_format"] == "png"
    assert metadata["actual_dpi"] == [300.0, 300.0]
    assert metadata["token_usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert result.saved_path == str(image_path)

    # Keep the original NanaBananaStudio generation-sidecar contract while
    # retaining BananaPrism's additional provenance and integrity fields.
    legacy_fields = {
        "mode",
        "model",
        "model_short",
        "size",
        "ratio",
        "prompt",
        "text_content",
        "fmt",
        "width",
        "height",
        "timestamp",
    }
    assert legacy_fields <= metadata.keys()
    assert metadata["mode"] == "txt2img"
    assert metadata["fmt"] == "png"

    second_path = service.auto_save(generation_result())
    assert second_path != image_path
    assert second_path.stem.endswith("_1")


def test_long_unicode_save_root_prompt_and_collision_stay_win32_safe(
    tmp_path: Path,
) -> None:
    def path_units(path: Path) -> int:
        return len(str(path).encode("utf-16-le")) // 2

    save_root = tmp_path
    while path_units(save_root) < 165:
        save_root /= "深层中文保存目录"
    settings = SimpleNamespace(prompt_save_length=100)
    service = FileService(settings=settings, save_dir=save_root)

    first_result = generation_result()
    first_result.request.prompt = "香蕉棱镜与彩色光线" * 20
    first_path = service.auto_save(first_result)
    second_result = generation_result()
    second_result.request.prompt = first_result.request.prompt
    second_path = service.auto_save(second_result)

    for path in (first_path, first_path.with_suffix(".json"), second_path, second_path.with_suffix(".json")):
        assert path.exists()
        assert path_units(path) <= 240
    assert first_path.parent.name == first_path.stem
    assert second_path.parent.name == second_path.stem
    assert second_path.stem.endswith("_1")
    assert FileService.is_complete(first_path)
    assert FileService.is_complete(second_path)


def test_edit_sidecar_records_actual_output_dpi_not_source_dpi(tmp_path: Path) -> None:
    service = FileService(save_dir=tmp_path)
    result = edit_result()
    image_path = service.auto_save_edit(result)
    metadata = json.loads(image_path.with_suffix(".json").read_text(encoding="utf-8"))

    assert image_path.suffix == ".jpg"
    assert metadata["source_dpi"] == [72.0, 72.0]
    assert metadata["wire_source_fmt"] == "png"
    assert metadata["saved_dpi"] == [300.0, 300.0]
    assert metadata["output_dpi"] == [300.0, 300.0]
    assert metadata["requested_output_format"] == "jpeg"
    assert metadata["requested_dpi"] == 300.0
    assert metadata["actual_output_format"] == "jpg"
    assert metadata["actual_dpi"] == [300.0, 300.0]
    assert metadata["provider"] == "aihubmix"
    assert metadata["preset_id"] == "preset_xyz"
    assert metadata["annotation_color"] == "green"
    assert metadata["selection_mask_format"] == "png"


def test_failed_or_unsupported_reencode_does_not_invent_output_dpi(tmp_path: Path) -> None:
    service = FileService(save_dir=tmp_path)
    result = generation_result()
    result.output_dpi = None

    image_path = service.auto_save(result)
    metadata = json.loads(image_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["output_dpi"] is None


def test_sidecar_uses_encoded_webp_dpi_readback_not_requested_claim(tmp_path: Path) -> None:
    result = generation_result()
    result.image_bytes = encoded_image("webp", 64, 48, dpi=300.0)
    result.fmt = "webp"
    result.output_dpi = (300.0, 300.0)
    actual = decoded_dpi(result.image_bytes)
    assert actual is not None

    image_path = FileService(save_dir=tmp_path).auto_save(result)
    metadata = json.loads(image_path.with_suffix(".json").read_text(encoding="utf-8"))

    expected = [round(value, 4) for value in actual]
    assert metadata["output_dpi"] == expected
    if any(abs(value - 300.0) > 0.1 for value in actual):
        assert metadata["output_dpi"] != [300.0, 300.0]


@pytest.mark.parametrize("fmt", ["png", "jpeg", "webp", "bmp"])
def test_import_sidecar_records_decoded_dimensions_format_and_dpi(
    tmp_path: Path,
    fmt: str,
) -> None:
    data = encoded_image(fmt, 37, 23, dpi=144.0)
    actual_dpi = decoded_dpi(data)
    image_path = FileService(save_dir=tmp_path / fmt).save_imported(data, fmt)
    metadata = json.loads(image_path.with_suffix(".json").read_text(encoding="utf-8"))

    normalised = "jpg" if fmt == "jpeg" else fmt
    assert image_path.suffix == f".{normalised}"
    assert metadata["source_fmt"] == normalised
    assert metadata["saved_fmt"] == normalised
    assert (metadata["width"], metadata["height"]) == (37, 23)
    assert metadata["source_dpi"] == (
        list(actual_dpi) if actual_dpi is not None else None
    )
    assert metadata["output_dpi"] == metadata["source_dpi"]


def test_pair_publish_failure_rolls_back_image_and_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FileService(save_dir=tmp_path)
    def fail_publish(temporary: Path, destination: Path) -> None:
        raise FileSaveError("injected result-directory publish failure")

    monkeypatch.setattr(file_module, "_publish_transaction", fail_publish)
    with pytest.raises(FileSaveError):
        service.auto_save(generation_result())

    assert list(tmp_path.glob("*.png")) == []
    assert list(tmp_path.glob("*.json")) == []
    assert list(tmp_path.glob("*.reserve")) == []
    assert list(tmp_path.glob(".bananaprism-*")) == []


def test_filename_sanitization_handles_illegal_and_reserved_windows_names() -> None:
    assert sanitize_filename_component("a/b:c * d") == "a_b_c_d"
    assert sanitize_filename_component("CON") == "_CON"
    assert sanitize_filename_component("  ") == "unnamed"


def test_logs_redact_registered_and_structured_secrets_and_escape_newlines(
    tmp_path: Path,
) -> None:
    logs = LogService(logs_dir=tmp_path)
    opaque = "totally-opaque-credential"
    logs.register_secret(opaque)
    assert logs.write(
        "info",
        f'api_key="{opaque}" password="two words" '
        "Authorization: Bearer abcdefghijklmnop\nforged",
    )

    text = next(tmp_path.glob("bananaprism_*.log")).read_text(encoding="utf-8")
    assert opaque not in text
    assert "abcdefghijklmnop" not in text
    assert "two words" not in text
    assert REDACTION in text
    assert r"\nforged" in text
    assert text.count("\n") == 1


def test_data_and_resource_paths_are_injectable_and_traversal_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "portable-data"
    bundle_dir = tmp_path / "bundle"
    monkeypatch.setenv(DATA_DIR_ENV, str(data_dir))
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle_dir), raising=False)

    assert get_app_data_dir() == data_dir
    assert get_resource_path("icons/app.ico") == (
        bundle_dir / "resources" / "icons" / "app.ico"
    ).resolve()
    with pytest.raises(ValueError):
        get_resource_path("../outside.txt")


def test_sidecar_and_filename_redact_registered_credentials(tmp_path: Path) -> None:
    secret = "opaque-sidecar-key-12345"
    register_process_secret(secret)
    result = generation_result()
    result.request.prompt = f"draw {secret} inside a prism"
    result.text_content = f"provider echoed {secret}"

    image_path = FileService(save_dir=tmp_path).auto_save(result)
    metadata = json.loads(image_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert secret not in str(image_path)
    assert secret not in json.dumps(metadata, ensure_ascii=False)
    assert REDACTION in metadata["prompt"]
    assert REDACTION in metadata["text_content"]
