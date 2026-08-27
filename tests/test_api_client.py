from __future__ import annotations

import base64
import json
import socket

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage

from banana_prism.services.api_client import (
    ApiClient,
    ApiProtocolError,
    ImageValidationError,
    UnsafeImageUrlError,
    validate_remote_image_url,
)


def png_bytes(width: int = 2, height: int = 1) -> bytes:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#7c3aed"))
    target = QByteArray()
    buffer = QBuffer(target)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(target)


def bmp_bytes() -> bytes:
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(QColor("#f5d547"))
    target = QByteArray()
    buffer = QBuffer(target)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "BMP")
    return bytes(target)


def data_uri(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def test_openrouter_generation_payload_is_exact() -> None:
    request = ApiClient.build_generation_request(
        provider="openrouter",
        api_key="secret-for-test",
        model_id="google/current-ga-image-model",
        prompt="a prism",
        image_size="2K",
        aspect_ratio="16:9",
    )
    assert request.url == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers == {
        "Authorization": "Bearer secret-for-test",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://banana-prism.local",
        "X-Title": "BananaPrism",
    }
    assert request.payload == {
        "model": "google/current-ga-image-model",
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": "a prism"}],
        "image_config": {"aspect_ratio": "16:9", "image_size": "2K"},
    }


def test_aihubmix_generation_uses_gemini_native_endpoint_and_payload() -> None:
    request = ApiClient.build_generation_request(
        provider="aihubmix",
        api_key="test-key",
        model_id="google/gemini-current-ga-image",
        prompt="banana",
        image_size="4K",
        aspect_ratio="1:1",
    )
    assert request.url == (
        "https://api.aihubmix.com/gemini/v1beta/models/"
        "gemini-current-ga-image:streamGenerateContent"
    )
    assert request.headers == {
        "Content-Type": "application/json",
        "x-goog-api-key": "test-key",
    }
    assert request.payload == {
        "contents": [{"role": "user", "parts": [{"text": "banana"}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE", "TEXT"],
            "imageConfig": {"aspectRatio": "1:1", "imageSize": "4K"},
        },
    }


def test_image_size_validation_rejects_lowercase_and_unsupported_legacy_resolution() -> None:
    common = dict(
        provider="aihubmix",
        api_key="test-key",
        prompt="banana",
        aspect_ratio="1:1",
    )
    with pytest.raises(ValueError, match="Unsupported image size"):
        ApiClient.build_generation_request(
            model_id="google/gemini-3.1-flash-image",
            image_size="4k",
            **common,
        )
    with pytest.raises(ValueError, match="only 1K"):
        ApiClient.build_generation_request(
            model_id="google/gemini-2.5-flash-image",
            image_size="2K",
            **common,
        )


def test_edit_payloads_preserve_recovered_provider_differences() -> None:
    original = png_bytes(2, 2)
    annotated = png_bytes(3, 2)
    common = dict(
        api_key="test-key",
        model_id="google/a-current-model",
        edit_prompt="replace the marked area",
        original_image=original,
        annotated_image=annotated,
        image_size="2K",
        aspect_ratio="4:3",
    )
    openrouter = ApiClient.build_edit_request(provider="openrouter", **common)
    content = openrouter.payload["messages"][0]["content"]
    assert [part["type"] for part in content] == [
        "text",
        "image_url",
        "text",
        "image_url",
    ]
    assert content[2]["text"] == "Annotated image (banana-yellow = region to edit):"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    aihubmix = ApiClient.build_edit_request(provider="aihubmix", **common)
    parts = aihubmix.payload["contents"][0]["parts"]
    assert parts[2] == {"text": "Annotated image (banana-yellow = region to edit):"}
    assert parts[2]["text"] == content[2]["text"]
    assert parts[1]["inlineData"]["mimeType"] == "image/png"
    assert parts[3]["inlineData"]["data"] == base64.b64encode(annotated).decode(
        "ascii"
    )


def test_bmp_edit_inputs_are_normalized_to_provider_compatible_png() -> None:
    common = dict(
        api_key="test-key",
        model_id="google/a-current-model",
        edit_prompt="edit",
        original_image=bmp_bytes(),
        annotated_image=bmp_bytes(),
        image_size="1K",
        aspect_ratio="1:1",
    )
    openrouter = ApiClient.build_edit_request(provider="openrouter", **common)
    url = openrouter.payload["messages"][0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1], validate=True).startswith(
        b"\x89PNG\r\n\x1a\n"
    )

    aihubmix = ApiClient.build_edit_request(provider="aihubmix", **common)
    inline = aihubmix.payload["contents"][0]["parts"][1]["inlineData"]
    assert inline["mimeType"] == "image/png"
    assert base64.b64decode(inline["data"], validate=True).startswith(
        b"\x89PNG\r\n\x1a\n"
    )


@pytest.mark.parametrize(
    "document",
    [
        lambda uri, raw: {"choices": [{"message": {"images": [{"image_url": {"url": uri}}]}}]},
        lambda uri, raw: {"choices": [{"message": {"content": [{"type": "image_url", "image_url": uri}]}}]},
        lambda uri, raw: {"choices": [{"message": {"content": "done " + uri}}]},
        lambda uri, raw: {"choices": [{"image": {"url": uri}, "message": {"content": "ok"}}]},
        lambda uri, raw: {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "native"},
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": base64.b64encode(raw).decode("ascii"),
                                }
                            },
                        ]
                    }
                }
            ]
        },
    ],
    ids=["message-images", "content-array", "content-data-uri", "choice-image", "inline-data"],
)
def test_all_five_response_image_strategies(document) -> None:
    raw = png_bytes()
    result = ApiClient.parse_response(
        provider="openrouter",
        body=json.dumps(document(data_uri(raw), raw)),
        status_code=200,
    )
    assert result.image is not None
    assert result.image.data == raw
    assert (result.image.width, result.image.height) == (2, 1)


def test_json_array_ndjson_and_sse_are_normalized() -> None:
    raw = png_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    array = [
        {"candidates": [{"content": {"parts": [{"text": "hello "}]}}]},
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": "world"},
                            {"inlineData": {"mimeType": "image/png", "data": encoded}},
                        ]
                    }
                }
            ],
            "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
        },
    ]
    for body in (
        json.dumps(array),
        "\n".join(json.dumps(item) for item in array),
        "\n\n".join(f"event: message\ndata: {json.dumps(item)}" for item in array)
        + "\n\ndata: [DONE]\n",
    ):
        result = ApiClient.parse_response(
            provider="aihubmix", body=body, status_code=200
        )
        assert result.text == "hello world"
        assert result.image and result.image.data == raw
        assert (result.input_tokens, result.output_tokens) == (7, 3)


def test_untrusted_usage_metadata_cannot_break_an_otherwise_valid_image() -> None:
    raw = png_bytes()
    document = {
        "choices": [{"message": {"content": data_uri(raw)}}],
        "usage": {
            "prompt_tokens": "not-a-number",
            "completion_tokens": -10,
        },
        "usageMetadata": {
            "promptTokenCount": True,
            "candidatesTokenCount": 10**30,
        },
    }

    result = ApiClient.parse_response(
        provider="openrouter",
        body=json.dumps(document),
        status_code=200,
    )

    assert result.image and result.image.data == raw
    assert (result.input_tokens, result.output_tokens) == (0, 0)


def test_strict_status_base64_and_image_validation() -> None:
    with pytest.raises(ApiProtocolError, match="HTTP 401"):
        ApiClient.parse_response(
            provider="openrouter",
            body='{"error":{"message":"denied"}}',
            status_code=401,
        )
    with pytest.raises(ApiProtocolError, match="HTTP 204"):
        ApiClient.parse_response(provider="openrouter", body="{}", status_code=204)
    bad = {"choices": [{"message": {"images": [{"data": "not base64!", "mime_type": "image/png"}]}}]}
    with pytest.raises(ImageValidationError, match="strict base64"):
        ApiClient.parse_response(
            provider="openrouter", body=json.dumps(bad), status_code=200
        )
    fake_png = base64.b64encode(b"\x89PNG\r\n\x1a\nnot-an-image").decode("ascii")
    malformed = {"choices": [{"message": {"content": f"data:image/png;base64,{fake_png}"}}]}
    with pytest.raises(ImageValidationError):
        ApiClient.parse_response(
            provider="openrouter", body=json.dumps(malformed), status_code=200
        )


@pytest.mark.parametrize(
    "body",
    [
        '{"choices":[{"error":{"message":"provider unavailable"}}]}',
        'data: {"error":{"message":"provider unavailable"}}\n\ndata: [DONE]\n',
    ],
)
def test_openrouter_embedded_choice_and_sse_errors_are_not_text_only(body: str) -> None:
    with pytest.raises(ApiProtocolError, match="provider unavailable"):
        ApiClient.parse_response(
            provider="openrouter",
            body=body,
            status_code=200,
        )


def test_remote_url_requires_https_and_public_dns() -> None:
    def public_resolver(host, port, *, type):
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    assert validate_remote_image_url(
        "https://images.example.test/output.png", resolver=public_resolver
    ) == ("93.184.216.34",)

    for url in (
        "http://images.example.test/output.png",
        "https://localhost/output.png",
        "https://127.0.0.1/output.png",
        "https://169.254.169.254/latest/meta-data",
    ):
        with pytest.raises(UnsafeImageUrlError):
            validate_remote_image_url(url, resolver=public_resolver)

    def private_resolver(host, port, *, type):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port))]

    with pytest.raises(UnsafeImageUrlError, match="non-public"):
        validate_remote_image_url(
            "https://images.example.test/output.png", resolver=private_resolver
        )

    def tunnelled_loopback_resolver(host, port, *, type):
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("2002:7f00:0001::", port, 0, 0),
            )
        ]

    with pytest.raises(UnsafeImageUrlError, match="non-public"):
        validate_remote_image_url(
            "https://images.example.test/output.png",
            resolver=tunnelled_loopback_resolver,
        )

    for nat64_address in (
        "64:ff9b::7f00:1",
        "64:ff9b::a00:8",
        "64:ff9b:1:7f00:0:100::",
        "64:ff9b:1::7f00:1",
    ):
        def nat64_private_resolver(host, port, *, type, address=nat64_address):
            return [
                (
                    socket.AF_INET6,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (address, port, 0, 0),
                )
            ]

        with pytest.raises(UnsafeImageUrlError, match="non-public"):
            validate_remote_image_url(
                "https://images.example.test/output.png",
                resolver=nat64_private_resolver,
            )

    def nat64_public_resolver(host, port, *, type):
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("64:ff9b::5db8:d822", port, 0, 0),
            )
        ]

    assert validate_remote_image_url(
        "https://images.example.test/output.png", resolver=nat64_public_resolver
    ) == ("64:ff9b::5db8:d822",)


def test_http_image_candidate_is_rejected_not_treated_as_text_only() -> None:
    document = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "try this"},
                        {"type": "image_url", "image_url": {"url": "http://example.test/a.png"}},
                    ]
                }
            }
        ]
    }
    with pytest.raises(UnsafeImageUrlError, match="HTTPS"):
        ApiClient.parse_response(
            provider="openrouter", body=json.dumps(document), status_code=200
        )
