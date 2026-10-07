"""Offline provider contracts for Nano Banana 2.1 and legacy model routing."""

from __future__ import annotations

import base64
import json
import random
from urllib.parse import urlsplit

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRect
from PySide6.QtGui import QColor, QImage, QPainter

from banana_prism.constants import API_PROVIDERS, NANO_BANANA_21_MODEL_ID
from banana_prism.services.api_client import (
    ApiClient,
    ApiClientError,
    ApiProtocolError,
    ImageValidationError,
)


MODEL_ID = "google/gemini-nano-banana-2.1"
NATIVE_URL = (
    "https://aihubmix.com/gemini/v1beta/models/"
    "gemini-nano-banana-2.1:generateContent"
)


def _png(image: QImage) -> bytes:
    target = QByteArray()
    buffer = QBuffer(target)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(target)


def _solid_png(width: int, height: int, color: str = "#234567") -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return _png(image)


def _inline(raw: bytes, *, thought: bool = False) -> dict:
    part = {
        "inlineData": {
            "mimeType": "image/png",
            "data": base64.b64encode(raw).decode("ascii"),
        }
    }
    if thought:
        part["thought"] = True
    return part


def _native_document(parts: list[dict]) -> dict:
    return {"candidates": [{"content": {"role": "model", "parts": parts}}]}


@pytest.mark.parametrize("provider", ["openrouter", "aihubmix"])
@pytest.mark.parametrize("image_size", ["1K", "2K", "4K"])
@pytest.mark.parametrize("use_ui_endpoint", [False, True], ids=["default", "ui-preset"])
def test_nano_banana_21_generation_protocol(
    provider: str, image_size: str, use_ui_endpoint: bool
) -> None:
    assert NANO_BANANA_21_MODEL_ID == MODEL_ID
    endpoint = {"api_url": API_PROVIDERS[provider].api_url} if use_ui_endpoint else {}
    request = ApiClient.build_generation_request(
        provider=provider,
        api_key="offline-test-key",
        model_id=MODEL_ID,
        prompt="a banana prism",
        image_size=image_size,
        aspect_ratio="3:4",
        **endpoint,
    )
    if provider == "openrouter":
        assert request.url == "https://openrouter.ai/api/v1/images"
        assert request.headers["Authorization"] == "Bearer offline-test-key"
        assert request.payload == {
            "model": MODEL_ID,
            "prompt": "a banana prism",
            "resolution": image_size,
            "aspect_ratio": "3:4",
            "n": 1,
        }
    else:
        assert request.url == NATIVE_URL
        assert request.headers == {
            "Content-Type": "application/json",
            "x-goog-api-key": "offline-test-key",
        }
        assert request.payload == {
            "contents": [{"role": "user", "parts": [{"text": "a banana prism"}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE", "TEXT"],
                "imageConfig": {"aspectRatio": "3:4", "imageSize": image_size},
            },
        }


@pytest.mark.parametrize("provider", ["openrouter", "aihubmix"])
@pytest.mark.parametrize("image_size", ["1K", "2K", "4K"])
def test_nano_banana_21_edit_keeps_original_then_colored_guide(
    provider: str, image_size: str
) -> None:
    original = _solid_png(64, 48)
    guide = QImage.fromData(original)
    painter = QPainter(guide)
    painter.fillRect(QRect(32, 16, 16, 16), QColor(255, 0, 205, 150))
    painter.end()
    annotation = _png(guide)
    mask = _solid_png(64, 48, "#ffffff")
    request = ApiClient.build_edit_request(
        provider=provider,
        api_key="offline-test-key",
        model_id=MODEL_ID,
        edit_prompt="replace only the marked area",
        original_image=original,
        annotated_image=annotation,
        selection_mask=mask,
        annotation_color="magenta",
        image_size=image_size,
        aspect_ratio="4:3",
        api_url=API_PROVIDERS[provider].api_url,
    )
    if provider == "openrouter":
        assert request.url == "https://openrouter.ai/api/v1/images"
        assert request.payload["model"] == MODEL_ID
        assert request.payload["resolution"] == image_size
        assert request.payload["aspect_ratio"] == "4:3"
        assert request.payload["n"] == 1
        references = request.payload["input_references"]
        assert len(references) == 2
        assert all(reference["type"] == "image_url" for reference in references)
        uris = [reference["image_url"]["url"] for reference in references]
        assert all(uri.startswith("data:image/jpeg;base64,") for uri in uris)
        wire = [base64.b64decode(uri.split(",", 1)[1], validate=True) for uri in uris]
        prompt = request.payload["prompt"]
        assert "messages" not in request.payload
    else:
        assert request.url == NATIVE_URL
        assert request.headers["x-goog-api-key"] == "offline-test-key"
        assert request.payload["generationConfig"]["imageConfig"] == {
            "aspectRatio": "4:3", "imageSize": image_size
        }
        parts = request.payload["contents"][0]["parts"]
        assert [next(iter(part)) for part in parts] == [
            "text", "inlineData", "text", "inlineData"
        ]
        inlines = [parts[1]["inlineData"], parts[3]["inlineData"]]
        assert all(part["mimeType"] == "image/jpeg" for part in inlines)
        wire = [base64.b64decode(part["data"], validate=True) for part in inlines]
        prompt = parts[0]["text"] + parts[2]["text"]

    assert "IMAGE 1 — ORIGINAL" in prompt
    assert "IMAGE 2 — ANNOTATION GUIDE" in prompt
    assert "bright magenta" in prompt
    assert "replace only the marked area" in prompt
    assert base64.b64encode(mask) not in request.body
    assert len(request.body) <= 16 * 1024 * 1024
    assert all(raw.startswith(b"\xff\xd8") for raw in wire)
    first, second = (QImage.fromData(raw) for raw in wire)
    assert first.size() == second.size() == guide.size()
    # Distinct reference content catches reversed ordering, which same-image
    # fixtures cannot. Test broad color differences because JPEG is lossy.
    original_center = first.pixelColor(40, 24)
    guide_center = second.pixelColor(40, 24)
    assert abs(original_center.red() - 35) <= 3
    assert guide_center.red() - original_center.red() > 100
    assert guide_center.blue() - original_center.blue() > 40
    assert abs(first.pixelColor(4, 4).red() - second.pixelColor(4, 4).red()) <= 3


@pytest.mark.parametrize("provider", ["openrouter", "aihubmix"])
@pytest.mark.parametrize("image_size", ["1K", "2K", "4K"])
def test_existing_model_routes_remain_compatible(provider: str, image_size: str) -> None:
    model_id = "google/gemini-3.1-flash-image"
    request = ApiClient.build_generation_request(
        provider=provider,
        api_key="offline-test-key",
        model_id=model_id,
        prompt="a legacy model image",
        image_size=image_size,
        aspect_ratio="1:1",
        api_url=API_PROVIDERS[provider].api_url,
    )
    if provider == "aihubmix":
        assert request.url == (
            "https://aihubmix.com/gemini/v1beta/models/"
            "gemini-3.1-flash-image:streamGenerateContent"
        )
        assert request.payload["generationConfig"]["imageConfig"]["imageSize"] == image_size
    else:
        assert request.payload["model"] == model_id
        if image_size == "4K":
            assert request.url == "https://openrouter.ai/api/v1/images"
            assert request.payload["resolution"] == "4K"
        else:
            assert request.url == "https://openrouter.ai/api/v1/chat/completions"
            assert request.payload["image_config"]["image_size"] == image_size
            assert request.payload["messages"] == [
                {"role": "user", "content": "a legacy model image"}
            ]


@pytest.mark.parametrize("provider", ["openrouter", "aihubmix"])
@pytest.mark.parametrize("image_size", ["1K", "2K", "4K"])
def test_nano_banana_21_custom_endpoints_preserve_credential_origin(
    provider: str, image_size: str
) -> None:
    endpoint = "https://gateway.example.test:8443/vendor/v1/chat/completions"
    request = ApiClient.build_generation_request(
        provider=provider,
        api_key="proxy-test-key",
        model_id=MODEL_ID,
        prompt="a proxy image",
        image_size=image_size,
        aspect_ratio="1:1",
        api_url=endpoint,
    )
    assert urlsplit(request.url).netloc == urlsplit(endpoint).netloc
    if provider == "openrouter":
        assert request.url == "https://gateway.example.test:8443/vendor/v1/images"
    else:
        assert request.url == (
            "https://gateway.example.test:8443/gemini/v1beta/models/"
            "gemini-nano-banana-2.1:generateContent"
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://gateway.example.test/v1/chat/completions",
        "https://user:password@gateway.example.test/v1/chat/completions",
        "https://gateway.example.test/custom-generate",
        "https://gateway.example.test/custom-generate?route=/chat/completions",
        "https://gateway.example.test/v1/chat/completions#fragment",
    ],
)
def test_nano_banana_21_rejects_unsafe_or_ambiguous_openrouter_endpoints(
    endpoint: str,
) -> None:
    with pytest.raises(ApiClientError):
        ApiClient.build_generation_request(
            provider="openrouter",
            api_key="proxy-test-key",
            model_id=MODEL_ID,
            prompt="a prism",
            image_size="1K",
            aspect_ratio="1:1",
            api_url=endpoint,
        )


@pytest.mark.parametrize("encoding", ["json", "array", "ndjson", "sse"])
def test_native_thinking_draft_and_text_never_replace_final_output(encoding: str) -> None:
    pixels = random.Random(21).randbytes(64 * 48 * 3)
    draft_image = QImage(pixels, 64, 48, 64 * 3, QImage.Format.Format_RGB888)
    draft = _png(draft_image)
    final = _solid_png(8, 6, "#f5d547")
    assert len(draft) > len(final)
    thinking = [{"text": "private working notes", "thought": True}, _inline(draft, thought=True)]
    completed = [{"text": "Here is the final image."}, _inline(final)]
    documents = [_native_document(thinking), _native_document(completed)]
    documents[-1]["usageMetadata"] = {"promptTokenCount": 12, "candidatesTokenCount": 34}
    if encoding == "json":
        document = _native_document(thinking + completed)
        document["usageMetadata"] = documents[-1]["usageMetadata"]
        body = json.dumps(document)
    elif encoding == "array":
        body = json.dumps(documents)
    elif encoding == "ndjson":
        body = "\n".join(json.dumps(document) for document in documents)
    else:
        body = "\n\n".join(f"data: {json.dumps(document)}" for document in documents)
        body += "\n\ndata: [DONE]\n"
    result = ApiClient.parse_response(provider="aihubmix", body=body, status_code=200)
    assert result.image is not None
    assert result.image.data == final
    assert (result.image.width, result.image.height) == (8, 6)
    assert result.text == "Here is the final image."
    assert (result.input_tokens, result.output_tokens) == (12, 34)


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("https://gateway.example.test/v1/images", "https://gateway.example.test/v1/images"),
        ("https://gateway.example.test/v1/images/", "https://gateway.example.test/v1/images"),
        (
            "https://gateway.example.test:8443/v1/chat/completions/?tenant=banana",
            "https://gateway.example.test:8443/v1/images?tenant=banana",
        ),
    ],
)
def test_images_endpoint_translation_uses_path_and_preserves_query(
    endpoint: str, expected: str
) -> None:
    request = ApiClient.build_generation_request(
        provider="openrouter",
        api_key="offline-test-key",
        model_id=MODEL_ID,
        prompt="a prism",
        image_size="1K",
        aspect_ratio="1:1",
        api_url=endpoint,
    )
    assert request.url == expected


def test_native_thinking_only_response_does_not_claim_image_success() -> None:
    body = _native_document([
        {"text": "private draft explanation", "thought": True},
        _inline(_solid_png(32, 24), thought=True),
    ])
    result = ApiClient.parse_response(
        provider="aihubmix", body=json.dumps(body), status_code=200
    )
    assert result.image is None
    assert result.remote_image_url is None
    assert result.text == ""


def test_native_thinking_parts_cannot_hide_an_invalid_final_image() -> None:
    invalid_final = {"inlineData": {"mimeType": "image/png", "data": "not base64!"}}
    body = _native_document([_inline(_solid_png(32, 24), thought=True), invalid_final])
    with pytest.raises(ImageValidationError, match="strict base64"):
        ApiClient.parse_response(provider="aihubmix", body=json.dumps(body), status_code=200)


def test_native_final_image_still_validates_declared_mime() -> None:
    part = _inline(_solid_png(8, 6))
    part["inlineData"]["mimeType"] = "image/jpeg"
    with pytest.raises(ImageValidationError, match="does not match"):
        ApiClient.parse_response(
            provider="aihubmix", body=json.dumps(_native_document([part])), status_code=200
        )


def test_native_prompt_block_is_not_misreported_as_an_image() -> None:
    body = _native_document([_inline(_solid_png(8, 6))])
    body["promptFeedback"] = {"blockReason": "SAFETY"}
    with pytest.raises(ApiProtocolError, match="Provider blocked the prompt: SAFETY"):
        ApiClient.parse_response(provider="aihubmix", body=json.dumps(body), status_code=200)
