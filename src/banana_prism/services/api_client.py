"""Provider protocol adapters and untrusted-response validation.

This module deliberately contains no synchronous HTTP client.  It prepares requests
for :class:`QNetworkAccessManager` and turns the returned bytes into a small, common
result which the UI-facing service can consume.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import math
import re
import socket
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, urlsplit

from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QImage, QImageReader

from banana_prism.constants import (
    EDIT_ANNOTATION_LABEL,
    EDIT_SYSTEM_PROMPT,
    MAX_IMAGE_PIXELS,
    MAX_RESPONSE_BYTES,
    SIZES,
)


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_IMAGES_URL = "https://openrouter.ai/api/v1/images"
AIHUBMIX_URL = "https://api.aihubmix.com/gemini/v1beta"
_DATA_URI_RE = re.compile(
    r"data:(image/[a-zA-Z0-9.+-]+);base64,([^\s\"'<>]+)",
    re.IGNORECASE,
)
_SUPPORTED_FORMATS = {"png", "jpeg", "jpg", "webp", "bmp"}
_NAT64_WELL_KNOWN_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL_USE_PREFIX = ipaddress.ip_network("64:ff9b:1::/48")


class Provider(str, Enum):
    OPENROUTER = "openrouter"
    AIHUBMIX = "aihubmix"


class ApiClientError(ValueError):
    """Base error safe to surface to the application."""


class ApiProtocolError(ApiClientError):
    """The provider returned a failed or malformed protocol response."""


class ImageValidationError(ApiClientError):
    """Returned image data failed strict validation."""


class UnsafeImageUrlError(ApiClientError):
    """A remote image URL could reach an unsafe network destination."""


@dataclass(frozen=True, slots=True)
class ApiRequest:
    provider: Provider
    url: str
    headers: Mapping[str, str] = field(repr=False)
    payload: Mapping[str, Any]

    @property
    def body(self) -> bytes:
        return json.dumps(
            self.payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    data: bytes = field(repr=False)
    mime_type: str
    fmt: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class ApiResult:
    text: str = ""
    image: ValidatedImage | None = None
    remote_image_url: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class _ImageCandidate:
    value: str
    mime_type: str | None = None


def _provider(value: Provider | str) -> Provider:
    try:
        return value if isinstance(value, Provider) else Provider(value.strip().lower())
    except (AttributeError, ValueError) as exc:
        raise ApiClientError(f"Unsupported API provider: {value!r}") from exc


def _require_https_api_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ApiClientError("API endpoint must be an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ApiClientError("API endpoint contains unsupported URL components")
    return url


def _validate_api_key(api_key: str) -> str:
    key = api_key.strip()
    if not key:
        raise ApiClientError("API key is required")
    if "\r" in key or "\n" in key:
        raise ApiClientError("API key contains invalid header characters")
    return key


def _mime_alias(value: str) -> str:
    value = value.lower().split(";", 1)[0].strip()
    return "image/jpeg" if value in {"image/jpg", "image/jpeg"} else value


def validate_image_bytes(
    data: bytes | bytearray | memoryview,
    declared_mime: str | None = None,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> ValidatedImage:
    """Decode enough of an image to verify format, dimensions and full validity."""

    raw = bytes(data)
    if not raw:
        raise ImageValidationError("Image payload is empty")
    if len(raw) > max_bytes:
        raise ImageValidationError(f"Image exceeds the {max_bytes}-byte limit")

    device = QBuffer()
    device.setData(QByteArray(raw))
    if not device.open(QIODevice.OpenModeFlag.ReadOnly):
        raise ImageValidationError("Could not open image data")
    reader = QImageReader(device)
    reader.setDecideFormatFromContent(True)
    fmt = bytes(reader.format()).decode("ascii", "ignore").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in _SUPPORTED_FORMATS or not reader.canRead():
        raise ImageValidationError("Unsupported or malformed image data")
    size = reader.size()
    width, height = size.width(), size.height()
    if width <= 0 or height <= 0:
        raise ImageValidationError("Image dimensions are invalid")
    if width * height > max_pixels:
        raise ImageValidationError(f"Image exceeds the {max_pixels}-pixel limit")
    decoded = reader.read()
    if decoded.isNull():
        raise ImageValidationError("Image decoder rejected the payload")

    mime = {
        "png": "image/png",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "bmp": "image/bmp",
    }[fmt]
    if declared_mime and _mime_alias(declared_mime) != mime:
        raise ImageValidationError(
            f"Declared image type {_mime_alias(declared_mime)!r} does not match {mime!r}"
        )
    return ValidatedImage(raw, mime, fmt, width, height)


def decode_base64_image(
    encoded: str,
    declared_mime: str | None = None,
    *,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_pixels: int = MAX_IMAGE_PIXELS,
) -> ValidatedImage:
    if not isinstance(encoded, str) or not encoded:
        raise ImageValidationError("Image base64 payload is empty")
    # Reject whitespace and non-alphabet bytes instead of silently normalizing them.
    max_encoded = 4 * math.ceil(max_bytes / 3) + 4
    if len(encoded) > max_encoded:
        raise ImageValidationError("Encoded image exceeds the configured size limit")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ImageValidationError("Image payload is not strict base64") from exc
    return validate_image_bytes(
        raw, declared_mime, max_bytes=max_bytes, max_pixels=max_pixels
    )


def _provider_input_image(data: bytes) -> ValidatedImage:
    """Return an API-supported image, losslessly transcoding BMP to PNG."""

    image = validate_image_bytes(data)
    if image.fmt in {"png", "jpeg", "webp"}:
        return image
    decoded = QImage.fromData(image.data)
    target = QByteArray()
    buffer = QBuffer(target)
    if decoded.isNull() or not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise ImageValidationError("Could not normalize input image")
    if not decoded.save(buffer, "PNG"):
        raise ImageValidationError("Could not normalize input image as PNG")
    return validate_image_bytes(bytes(target), "image/png")


def validate_remote_image_url_syntax(url: str) -> tuple[str, int]:
    """Validate URL syntax and literal hosts without performing DNS."""

    if not isinstance(url, str) or len(url) > 4096:
        raise UnsafeImageUrlError("Remote image URL is invalid")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise UnsafeImageUrlError("Remote images must use an absolute HTTPS URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise UnsafeImageUrlError("Remote image URL contains unsafe components")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise UnsafeImageUrlError("Remote image URL has an invalid port") from exc
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeImageUrlError("Localhost image URLs are not allowed")
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        pass
    else:
        ensure_public_addresses([str(literal)])
    return host, port


def ensure_public_addresses(addresses: Iterable[str]) -> tuple[str, ...]:
    checked: list[str] = []
    for value in addresses:
        try:
            address = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeImageUrlError("DNS returned an invalid address") from exc
        embedded = []
        is_nat64 = False
        if isinstance(address, ipaddress.IPv6Address):
            embedded.extend(
                candidate
                for candidate in (address.ipv4_mapped, address.sixtofour)
                if candidate is not None
            )
            if address.teredo:
                embedded.extend(address.teredo)
            packed = address.packed
            if address in _NAT64_WELL_KNOWN_PREFIX:
                is_nat64 = True
                embedded.append(ipaddress.IPv4Address(packed[-4:]))
            elif address in _NAT64_LOCAL_USE_PREFIX:
                is_nat64 = True
                # RFC 6052's /48 layout puts the first two IPv4 octets at
                # bytes 6..7, the reserved ``u`` octet at byte 8, and the
                # remaining two octets at bytes 9..10.  Some DNS64 stacks use
                # the simpler last-32-bits form with this local-use prefix;
                # inspect that form as well when its intervening bytes are 0.
                if packed[8] == 0 and not any(packed[11:]):
                    embedded.append(
                        ipaddress.IPv4Address(packed[6:8] + packed[9:11])
                    )
                if not any(packed[6:12]):
                    embedded.append(ipaddress.IPv4Address(packed[-4:]))
        # ipaddress classifies the standardized NAT64 prefixes as reserved.
        # Their safety is determined by the embedded destination instead; an
        # unrecognized local-use layout is rejected rather than guessed.
        if is_nat64 and not embedded:
            raise UnsafeImageUrlError(
                "Remote image host uses an unsupported NAT64 address layout"
            )
        inspected = embedded if is_nat64 else [address, *embedded]
        if any(
            not candidate.is_global
            or candidate.is_private
            or candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_reserved
            or candidate.is_unspecified
            for candidate in inspected
        ):
            raise UnsafeImageUrlError(
                "Remote image host resolves to a non-public address"
            )
        checked.append(str(address))
    if not checked:
        raise UnsafeImageUrlError("Remote image host did not resolve")
    return tuple(checked)


def validate_remote_image_url(
    url: str,
    *,
    resolver: Callable[..., Sequence[tuple[Any, ...]]] = socket.getaddrinfo,
) -> tuple[str, ...]:
    """Synchronous validator intended for tests and non-event-loop callers.

    ``ImageService`` uses Qt's asynchronous resolver instead.
    """

    host, port = validate_remote_image_url_syntax(url)
    try:
        records = resolver(host, port, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise UnsafeImageUrlError("Remote image host could not be resolved") from exc
    addresses = [record[4][0] for record in records if len(record) >= 5 and record[4]]
    return ensure_public_addresses(addresses)


class ApiClient:
    """Build exact provider payloads and normalize their response variants."""

    @staticmethod
    def build_generation_request(
        *,
        provider: Provider | str,
        api_key: str,
        model_id: str,
        prompt: str,
        image_size: str,
        aspect_ratio: str,
        api_url: str | None = None,
    ) -> ApiRequest:
        selected = _provider(provider)
        key = _validate_api_key(api_key)
        if not model_id.strip():
            raise ApiClientError("Model ID is required")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiClientError("Prompt is required")
        if image_size not in SIZES:
            raise ApiClientError(f"Unsupported image size: {image_size}")
        if model_id.removeprefix("google/") == "gemini-2.5-flash-image" and image_size != "1K":
            raise ApiClientError("Gemini 2.5 Flash Image supports only 1K output")
        if not aspect_ratio:
            raise ApiClientError("Image size and aspect ratio are required")

        if selected is Provider.OPENROUTER:
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/doudou03015/BananaPrism",
                "X-OpenRouter-Title": "BananaPrism",
            }
            # OpenRouter's Chat Completions compatibility path currently rejects
            # 4K for the GA Gemini image slug even though the dedicated Images
            # API advertises that exact slug and resolution as supported.  Keep
            # Chat for 1K/2K (it can return explanatory text), while routing 4K
            # explicitly through the capability-aware Images API.  The requested
            # model and resolution are preserved; there is no preview-model swap
            # or silent downgrade.
            if image_size == "4K":
                url = ApiClient._openrouter_images_url(api_url or OPENROUTER_URL)
                payload: Mapping[str, Any] = {
                    "model": model_id,
                    "prompt": prompt,
                    "resolution": image_size,
                    "aspect_ratio": aspect_ratio,
                    "n": 1,
                }
                return ApiRequest(selected, url, headers, payload)

            url = _require_https_api_url(api_url or OPENROUTER_URL)
            payload: Mapping[str, Any] = {
                "model": model_id,
                "modalities": ["image", "text"],
                "messages": [{"role": "user", "content": prompt}],
                "image_config": {
                    "aspect_ratio": aspect_ratio,
                    "image_size": image_size,
                },
            }
            return ApiRequest(selected, url, headers, payload)

        url = ApiClient._aihubmix_native_url(api_url or AIHUBMIX_URL, model_id)
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE", "TEXT"],
                "imageConfig": {
                    "aspectRatio": aspect_ratio,
                    # Gemini Native rejects lowercase values such as ``4k``.
                    "imageSize": image_size,
                },
            },
        }
        return ApiRequest(selected, url, headers, payload)

    @staticmethod
    def build_edit_request(
        *,
        provider: Provider | str,
        api_key: str,
        model_id: str,
        edit_prompt: str,
        original_image: bytes,
        annotated_image: bytes,
        image_size: str,
        aspect_ratio: str,
        api_url: str | None = None,
    ) -> ApiRequest:
        selected = _provider(provider)
        original = _provider_input_image(original_image)
        annotation = _provider_input_image(annotated_image)
        combined = (
            "[System instruction]\n"
            f"{EDIT_SYSTEM_PROMPT}\n\n"
            "[User edit request]\n"
            f"{edit_prompt}"
        )
        if not edit_prompt.strip():
            raise ApiClientError("Edit prompt is required")

        request = ApiClient.build_generation_request(
            provider=selected,
            api_key=api_key,
            model_id=model_id,
            prompt=combined,
            image_size=image_size,
            aspect_ratio=aspect_ratio,
            api_url=api_url,
        )
        original_uri = (
            f"data:{original.mime_type};base64,"
            f"{base64.b64encode(original.data).decode('ascii')}"
        )
        annotation_uri = (
            f"data:{annotation.mime_type};base64,"
            f"{base64.b64encode(annotation.data).decode('ascii')}"
        )
        if selected is Provider.OPENROUTER:
            payload = dict(request.payload)
            if "messages" in payload:
                payload["messages"] = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": combined},
                            {"type": "image_url", "image_url": {"url": original_uri}},
                            {
                                "type": "text",
                                "text": EDIT_ANNOTATION_LABEL,
                            },
                            {"type": "image_url", "image_url": {"url": annotation_uri}},
                        ],
                    }
                ]
            else:
                # The dedicated Images API accepts ordered reference images
                # separately from the prompt.  Spell out the order so the same
                # original/annotation semantics survive the protocol switch.
                payload["prompt"] = (
                    f"{combined}\n\n"
                    "[Reference mapping]\n"
                    "IMAGE 1 — ORIGINAL.\n"
                    f"IMAGE 2 — {EDIT_ANNOTATION_LABEL}"
                )
                payload["input_references"] = [
                    {"type": "image_url", "image_url": {"url": original_uri}},
                    {"type": "image_url", "image_url": {"url": annotation_uri}},
                ]
        else:
            payload = dict(request.payload)
            payload["contents"] = [
                {
                    "role": "user",
                    "parts": [
                        {"text": combined},
                        {
                            "inlineData": {
                                "mimeType": original.mime_type,
                                "data": base64.b64encode(original.data).decode("ascii"),
                            }
                        },
                        {"text": EDIT_ANNOTATION_LABEL},
                        {
                            "inlineData": {
                                "mimeType": annotation.mime_type,
                                "data": base64.b64encode(annotation.data).decode("ascii"),
                            }
                        },
                    ],
                }
            ]
        return ApiRequest(request.provider, request.url, request.headers, payload)

    @staticmethod
    def _openrouter_images_url(api_url: str) -> str:
        """Return the same-origin Images API URL for an OpenRouter chat URL.

        Presets may point at an OpenRouter-compatible proxy.  Never move a
        credential to a different origin: translate only the conventional
        ``/chat/completions`` suffix on the configured HTTPS endpoint.
        """

        validated = _require_https_api_url(api_url)
        if validated == OPENROUTER_IMAGES_URL:
            return validated
        if validated.endswith("/chat/completions"):
            return validated[: -len("/chat/completions")] + "/images"
        raise ApiClientError(
            "OpenRouter 4K requires an Images API URL or a Chat Completions URL"
        )

    @staticmethod
    def _aihubmix_native_url(api_url: str, model_id: str) -> str:
        base = urlsplit(_require_https_api_url(api_url))
        native_model = model_id.removeprefix("google/").strip()
        if not native_model:
            raise ApiClientError("AiHubMix requires a Gemini model ID")
        encoded_model = quote(native_model, safe="._-")
        return f"{base.scheme}://{base.netloc}/gemini/v1beta/models/{encoded_model}:streamGenerateContent"

    @staticmethod
    def parse_response(
        *,
        provider: Provider | str,
        body: bytes | bytearray | str,
        status_code: int,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        max_image_pixels: int = MAX_IMAGE_PIXELS,
    ) -> ApiResult:
        selected = _provider(provider)
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        if len(raw) > max_response_bytes:
            raise ApiProtocolError("API response exceeds the configured size limit")
        if int(status_code) != 200:
            detail = ApiClient._error_detail(raw)
            suffix = f": {detail}" if detail else ""
            raise ApiProtocolError(f"API returned HTTP {status_code}{suffix}")
        documents = ApiClient._decode_documents(raw)
        ApiClient._raise_embedded_error(documents)
        text = ApiClient._extract_text(documents)
        input_tokens, output_tokens = ApiClient._extract_usage(documents, selected)

        first_error: ImageValidationError | UnsafeImageUrlError | None = None
        for candidates in ApiClient._candidate_strategies(documents):
            if not candidates:
                continue
            valid: list[ValidatedImage] = []
            remote_urls: list[str] = []
            for candidate in candidates:
                try:
                    if candidate.value.lower().startswith(("https://", "http://")):
                        validate_remote_image_url_syntax(candidate.value)
                        remote_urls.append(candidate.value)
                    else:
                        match = _DATA_URI_RE.fullmatch(candidate.value)
                        if match:
                            mime, encoded = match.groups()
                        else:
                            mime, encoded = candidate.mime_type, candidate.value
                        valid.append(
                            decode_base64_image(
                                encoded,
                                mime,
                                max_bytes=max_response_bytes,
                                max_pixels=max_image_pixels,
                            )
                        )
                except (ImageValidationError, UnsafeImageUrlError) as exc:
                    first_error = first_error or exc
            if valid:
                image = max(valid, key=lambda item: len(item.data))
                return ApiResult(text, image, None, input_tokens, output_tokens)
            if remote_urls:
                return ApiResult(text, None, remote_urls[0], input_tokens, output_tokens)

        if first_error:
            raise first_error
        return ApiResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)

    @staticmethod
    def _decode_documents(raw: bytes) -> list[dict[str, Any]]:
        if not raw.strip():
            raise ApiProtocolError("API response is empty")
        try:
            text = raw.decode("utf-8-sig", "strict")
        except UnicodeDecodeError as exc:
            raise ApiProtocolError("API response is not valid UTF-8") from exc

        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if decoded is not None:
            documents = ApiClient._flatten_document(decoded)
            if not documents:
                raise ApiProtocolError("API response contains no JSON objects")
            return documents

        documents: list[dict[str, Any]] = []
        pending_data: list[str] = []

        def add_json(value: str) -> None:
            value = value.strip()
            if not value or value == "[DONE]":
                return
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ApiProtocolError("Malformed NDJSON/SSE response") from exc
            documents.extend(ApiClient._flatten_document(parsed))

        def flush_data() -> None:
            nonlocal pending_data
            if not pending_data:
                return
            # Most APIs emit one JSON value per data line.  Standard SSE may split
            # one JSON value over several data lines, so support both forms.
            try:
                for value in pending_data:
                    add_json(value)
            except ApiProtocolError:
                add_json("\n".join(pending_data))
            pending_data = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                flush_data()
            elif line.startswith("data:"):
                pending_data.append(line[5:].lstrip())
            elif line.startswith(":") or line.startswith(("event:", "id:", "retry:")):
                continue
            else:
                flush_data()
                add_json(line)
        flush_data()
        if not documents:
            raise ApiProtocolError("API response contains no JSON objects")
        return documents

    @staticmethod
    def _flatten_document(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list):
            if not all(isinstance(item, dict) for item in value):
                raise ApiProtocolError("JSON arrays must contain only objects")
            return list(value)
        raise ApiProtocolError("Top-level JSON response must be an object or array")

    @staticmethod
    def _error_detail(raw: bytes) -> str:
        """Extract a bounded, single-line provider error without dumping payloads.

        Error bodies are untrusted and sometimes contain an HTML gateway page.
        Prefer the provider's structured message, then fall back to visible text;
        credential redaction is applied by ``ImageService`` because it owns the
        exact request credential.
        """

        value: Any = None
        try:
            value = json.loads(raw[:16_384].decode("utf-8", "replace"))
            error = value.get("error") if isinstance(value, dict) else None
            if isinstance(error, dict):
                value = (
                    error.get("message")
                    or error.get("detail")
                    or error.get("status")
                    or ""
                )
            elif error:
                value = error
            elif isinstance(value, dict):
                value = value.get("message") or value.get("detail") or ""
            else:
                value = ""
        except (ValueError, TypeError):
            value = raw[:4_096].decode("utf-8", "replace")

        text = re.sub(r"<[^>]{0,256}>", " ", str(value or ""))
        text = " ".join(text.replace("\x00", " ").split())
        return text[:300]

    @staticmethod
    def _raise_embedded_error(documents: Sequence[dict[str, Any]]) -> None:
        for document in documents:
            if document.get("error"):
                raise ApiProtocolError(ApiClient._embedded_error_message(document["error"]))
            # OpenRouter may return a provider failure on an individual choice
            # even when the outer HTTP response is 200 (including an SSE chunk).
            for collection_name in ("choices", "candidates"):
                collection = document.get(collection_name)
                if not isinstance(collection, list):
                    continue
                for item in collection:
                    if isinstance(item, dict) and item.get("error"):
                        raise ApiProtocolError(
                            ApiClient._embedded_error_message(item["error"])
                        )
            feedback = document.get("promptFeedback")
            if isinstance(feedback, dict) and feedback.get("blockReason"):
                raise ApiProtocolError(
                    f"Provider blocked the prompt: {feedback['blockReason']}"
                )

    @staticmethod
    def _embedded_error_message(error: Any) -> str:
        if isinstance(error, dict):
            error = (
                error.get("message")
                or error.get("detail")
                or error.get("status")
                or "Provider error"
            )
        return " ".join(str(error).replace("\x00", " ").split())[:500]

    @staticmethod
    def _choice_nodes(document: Mapping[str, Any]) -> Iterable[tuple[Mapping[str, Any], Mapping[str, Any]]]:
        choices = document.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                node = choice.get("message") or choice.get("delta") or {}
                if isinstance(node, dict):
                    yield choice, node
        candidates = document.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                content = candidate.get("content") or {}
                if isinstance(content, dict):
                    yield candidate, content

    @staticmethod
    def _extract_text(documents: Sequence[dict[str, Any]]) -> str:
        pieces: list[str] = []
        for document in documents:
            for _choice, node in ApiClient._choice_nodes(document):
                content = node.get("content")
                if isinstance(content, str):
                    cleaned = _DATA_URI_RE.sub("", content)
                    if cleaned:
                        pieces.append(cleaned)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            pieces.append(part["text"])
                for key in ("parts", "multi_mod_content"):
                    parts = node.get(key)
                    if isinstance(parts, list):
                        for part in parts:
                            if isinstance(part, dict) and isinstance(part.get("text"), str):
                                pieces.append(part["text"])
        return "".join(pieces).strip()

    @staticmethod
    def _extract_usage(
        documents: Sequence[dict[str, Any]], provider: Provider
    ) -> tuple[int, int]:
        del provider

        def token_count(value: Any) -> int:
            if isinstance(value, bool) or value is None:
                return 0
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                return 0
            return parsed if 0 <= parsed <= 1_000_000_000_000 else 0

        input_tokens = output_tokens = 0
        for document in documents:
            usage = document.get("usage")
            if isinstance(usage, dict):
                input_tokens = max(
                    input_tokens, token_count(usage.get("prompt_tokens"))
                )
                output_tokens = max(
                    output_tokens, token_count(usage.get("completion_tokens"))
                )
            metadata = document.get("usageMetadata")
            if isinstance(metadata, dict):
                input_tokens = max(
                    input_tokens, token_count(metadata.get("promptTokenCount"))
                )
                output_tokens = max(
                    output_tokens, token_count(metadata.get("candidatesTokenCount"))
                )
        return input_tokens, output_tokens

    @staticmethod
    def _coerce_candidate(value: Any, mime: str | None = None) -> list[_ImageCandidate]:
        if isinstance(value, str):
            if value.startswith("data:image/") or value.lower().startswith(
                ("https://", "http://")
            ):
                return [_ImageCandidate(value, mime)]
            if mime and mime.lower().startswith("image/"):
                return [_ImageCandidate(value, mime)]
            return []
        if not isinstance(value, dict):
            return []
        declared = (
            value.get("mime_type")
            or value.get("mimeType")
            or value.get("media_type")
            or mime
        )
        result: list[_ImageCandidate] = []
        for key in ("url", "data"):
            if key in value:
                result.extend(ApiClient._coerce_candidate(value[key], declared))
        for key in ("image_url", "image", "source", "inline_data", "inlineData"):
            if key in value:
                result.extend(ApiClient._coerce_candidate(value[key], declared))
        return result

    @staticmethod
    def _candidate_strategies(
        documents: Sequence[dict[str, Any]],
    ) -> Iterable[list[_ImageCandidate]]:
        # 1. OpenRouter's dedicated Images API: data[].b64_json/media_type.
        strategy: list[_ImageCandidate] = []
        for document in documents:
            data = document.get("data")
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    encoded = item.get("b64_json")
                    if isinstance(encoded, str):
                        media_type = item.get("media_type")
                        strategy.append(
                            _ImageCandidate(
                                encoded,
                                media_type if isinstance(media_type, str) else None,
                            )
                        )
        yield strategy

        # 2. OpenRouter's message.images extension.
        strategy: list[_ImageCandidate] = []
        for document in documents:
            for _choice, node in ApiClient._choice_nodes(document):
                images = node.get("images")
                if isinstance(images, list):
                    for image in images:
                        strategy.extend(ApiClient._coerce_candidate(image))
        yield strategy

        # 3. Multimodal entries in message.content[].
        strategy = []
        for document in documents:
            for _choice, node in ApiClient._choice_nodes(document):
                content = node.get("content")
                if isinstance(content, list):
                    for part in content:
                        strategy.extend(ApiClient._coerce_candidate(part))
        yield strategy

        # 4. Data URI embedded in a string content response.
        strategy = []
        for document in documents:
            for _choice, node in ApiClient._choice_nodes(document):
                content = node.get("content")
                if isinstance(content, str):
                    strategy.extend(
                        _ImageCandidate(match.group(0), match.group(1))
                        for match in _DATA_URI_RE.finditer(content)
                    )
        yield strategy

        # 5. Image directly attached to a choice/candidate.
        strategy = []
        for document in documents:
            for choice, _node in ApiClient._choice_nodes(document):
                for key in ("image_url", "image"):
                    if key in choice:
                        strategy.extend(ApiClient._coerce_candidate(choice[key]))
        yield strategy

        # 6. OpenRouter multi_mod_content or Gemini native content.parts inlineData.
        strategy = []
        for document in documents:
            for _choice, node in ApiClient._choice_nodes(document):
                for key in ("multi_mod_content", "parts"):
                    parts = node.get(key)
                    if isinstance(parts, list):
                        for part in parts:
                            if isinstance(part, dict) and (
                                "inline_data" in part or "inlineData" in part
                            ):
                                strategy.extend(ApiClient._coerce_candidate(part))
        yield strategy


__all__ = [
    "AIHUBMIX_URL",
    "OPENROUTER_IMAGES_URL",
    "OPENROUTER_URL",
    "ApiClient",
    "ApiClientError",
    "ApiProtocolError",
    "ApiRequest",
    "ApiResult",
    "ImageValidationError",
    "Provider",
    "UnsafeImageUrlError",
    "ValidatedImage",
    "decode_base64_image",
    "ensure_public_addresses",
    "validate_image_bytes",
    "validate_remote_image_url",
    "validate_remote_image_url_syntax",
]
