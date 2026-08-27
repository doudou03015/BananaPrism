from __future__ import annotations

import base64
import random
from dataclasses import dataclass

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRect
from PySide6.QtGui import QColor, QImage, QPainter, QPen

from banana_prism.services.api_client import ApiClient, ApiRequest


_MAX_EDIT_BODY_BYTES = 16 * 1024 * 1024
_LARGE_WIDTH = 4_800
_LARGE_HEIGHT = 3_584
_MARKED_RECT = QRect(3_360, 2_150, 720, 560)


@dataclass(frozen=True)
class EditInputs:
    original: bytes
    guide: bytes
    mask: bytes
    width: int
    height: int


def _encode(image: QImage, fmt: str) -> bytes:
    target = QByteArray()
    buffer = QBuffer(target)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, fmt)
    return bytes(target)


def _large_high_entropy_inputs() -> EditInputs:
    # A deterministic noise image is deliberately difficult to compress.  It
    # exercises the same failure mode as a large, detailed imported PNG without
    # relying on a user's source image or filesystem.
    pixels = random.Random(0xBAA_AA5).randbytes(
        _LARGE_WIDTH * _LARGE_HEIGHT * 3
    )
    wrapped = QImage(
        pixels,
        _LARGE_WIDTH,
        _LARGE_HEIGHT,
        _LARGE_WIDTH * 3,
        QImage.Format.Format_RGB888,
    )
    original = wrapped.copy()
    assert not original.isNull()
    del wrapped, pixels

    guide = original.copy()
    painter = QPainter(guide)
    painter.setPen(QPen(QColor(255, 0, 0, 230), 36))
    painter.setBrush(QColor(255, 0, 0, 112))
    painter.drawRect(_MARKED_RECT)
    painter.end()

    mask = QImage(_LARGE_WIDTH, _LARGE_HEIGHT, QImage.Format.Format_Grayscale8)
    mask.fill(0)
    painter = QPainter(mask)
    painter.fillRect(_MARKED_RECT, QColor(255, 255, 255))
    painter.end()

    original_bytes = _encode(original, "PNG")
    guide_bytes = _encode(guide, "PNG")
    mask_bytes = _encode(mask, "PNG")
    return EditInputs(
        original=original_bytes,
        guide=guide_bytes,
        mask=mask_bytes,
        width=_LARGE_WIDTH,
        height=_LARGE_HEIGHT,
    )


def _small_inputs() -> EditInputs:
    width, height = 640, 480
    original = QImage(width, height, QImage.Format.Format_RGB32)
    original.fill(QColor("#234567"))
    guide = original.copy()
    painter = QPainter(guide)
    painter.fillRect(QRect(300, 180, 120, 90), QColor(255, 0, 0, 112))
    painter.end()
    mask = QImage(width, height, QImage.Format.Format_Grayscale8)
    mask.fill(0)
    painter = QPainter(mask)
    painter.fillRect(QRect(300, 180, 120, 90), QColor(255, 255, 255))
    painter.end()
    return EditInputs(
        original=_encode(original, "PNG"),
        guide=_encode(guide, "PNG"),
        mask=_encode(mask, "PNG"),
        width=width,
        height=height,
    )


@pytest.fixture(scope="module")
def high_entropy_edit_inputs() -> EditInputs:
    return _large_high_entropy_inputs()


def _wire_images(request: ApiRequest) -> list[tuple[str, bytes]]:
    payload = request.payload
    encoded: list[tuple[str, str]] = []
    if request.provider.value == "openrouter":
        if "input_references" in payload:
            for reference in payload["input_references"]:
                uri = reference["image_url"]["url"]
                header, value = uri.split(",", 1)
                mime = header.removeprefix("data:").removesuffix(";base64")
                encoded.append((mime, value))
        else:
            for part in payload["messages"][0]["content"]:
                if part.get("type") != "image_url":
                    continue
                uri = part["image_url"]["url"]
                header, value = uri.split(",", 1)
                mime = header.removeprefix("data:").removesuffix(";base64")
                encoded.append((mime, value))
    else:
        for part in payload["contents"][0]["parts"]:
            inline = part.get("inlineData")
            if inline is not None:
                encoded.append((inline["mimeType"], inline["data"]))
    return [(mime, base64.b64decode(value, validate=True)) for mime, value in encoded]


def _wire_prompt(request: ApiRequest) -> str:
    payload = request.payload
    if request.provider.value == "openrouter":
        if "input_references" in payload:
            return str(payload["prompt"])
        return "\n".join(
            str(part["text"])
            for part in payload["messages"][0]["content"]
            if part.get("type") == "text"
        )
    return "\n".join(
        str(part["text"])
        for part in payload["contents"][0]["parts"]
        if "text" in part
    )


def _build_edit(provider: str, image_size: str, inputs: EditInputs) -> ApiRequest:
    return ApiClient.build_edit_request(
        provider=provider,
        api_key="synthetic-test-key",
        model_id="google/gemini-3.1-flash-image",
        edit_prompt="replace only the marked rectangle",
        original_image=inputs.original,
        annotated_image=inputs.guide,
        selection_mask=inputs.mask,
        annotation_color="red",
        image_size=image_size,
        aspect_ratio="4:3",
    )


@pytest.mark.parametrize("provider", ["openrouter", "aihubmix"])
@pytest.mark.parametrize("image_size", ["1K", "2K", "4K"])
def test_high_entropy_4800_edit_wire_contract_stays_below_16_mib(
    provider: str,
    image_size: str,
    high_entropy_edit_inputs: EditInputs,
) -> None:
    request = _build_edit(provider, image_size, high_entropy_edit_inputs)

    assert len(request.body) <= _MAX_EDIT_BODY_BYTES
    images = _wire_images(request)
    assert len(images) == 2
    assert {mime for mime, _data in images} == {"image/jpeg"}

    decoded = [QImage.fromData(data) for _mime, data in images]
    assert all(not image.isNull() for image in decoded)
    assert decoded[0].size() == decoded[1].size()
    assert decoded[0].width() <= high_entropy_edit_inputs.width
    assert decoded[0].height() <= high_entropy_edit_inputs.height
    # This worst-case pair cannot meet the body ceiling at its source size.
    assert decoded[0].width() < high_entropy_edit_inputs.width

    prompt = _wire_prompt(request)
    assert prompt.count("You receive exactly TWO images:") == 1
    assert "exactly THREE" not in prompt
    assert "IMAGE 3" not in prompt
    assert "BINARY SELECTION MASK" not in prompt
    assert base64.b64encode(high_entropy_edit_inputs.mask) not in request.body


@pytest.mark.parametrize("provider", ["openrouter", "aihubmix"])
def test_small_edit_images_are_not_needlessly_rescaled(provider: str) -> None:
    inputs = _small_inputs()
    request = _build_edit(provider, "2K", inputs)
    decoded = [QImage.fromData(data) for _mime, data in _wire_images(request)]

    assert len(decoded) == 2
    assert {(image.width(), image.height()) for image in decoded} == {
        (inputs.width, inputs.height)
    }
    assert len(request.body) <= _MAX_EDIT_BODY_BYTES


def _pixel_delta(left: QImage, right: QImage, x: int, y: int) -> int:
    a = left.pixelColor(x, y)
    b = right.pixelColor(x, y)
    return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(
        a.blue() - b.blue()
    )


def test_large_guide_and_original_use_one_scale_transform(
    high_entropy_edit_inputs: EditInputs,
) -> None:
    request = _build_edit("openrouter", "1K", high_entropy_edit_inputs)
    images = [QImage.fromData(data) for _mime, data in _wire_images(request)]
    original, guide = images
    scale_x = guide.width() / high_entropy_edit_inputs.width
    scale_y = guide.height() / high_entropy_edit_inputs.height
    expected_left = round(_MARKED_RECT.left() * scale_x)
    expected_right = round(_MARKED_RECT.right() * scale_x)
    expected_top = round(_MARKED_RECT.top() * scale_y)
    expected_bottom = round(_MARKED_RECT.bottom() * scale_y)
    center_x = (expected_left + expected_right) // 2
    center_y = (expected_top + expected_bottom) // 2

    # Scan through the centre of the known mark.  Independent resizes or an
    # aspect-ratio drift would move these transitions away from their expected
    # source-space coordinates.
    changed_x = [
        x
        for x in range(guide.width())
        if _pixel_delta(original, guide, x, center_y) >= 45
    ]
    changed_y = [
        y
        for y in range(guide.height())
        if _pixel_delta(original, guide, center_x, y) >= 45
    ]
    assert changed_x and changed_y
    tolerance = 24
    assert abs(min(changed_x) - expected_left) <= tolerance
    assert abs(max(changed_x) - expected_right) <= tolerance
    assert abs(min(changed_y) - expected_top) <= tolerance
    assert abs(max(changed_y) - expected_bottom) <= tolerance
    assert _pixel_delta(original, guide, center_x, center_y) >= 80
