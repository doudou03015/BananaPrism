"""Small, dependency-free helpers."""

from __future__ import annotations


def detect_image_format(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"RIFF",)) and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"BM"):
        return "bmp"
    return "unknown"


def mime_for_format(fmt: str) -> str:
    return {
        "png": "image/png",
        "jpeg": "image/jpeg",
        "jpg": "image/jpeg",
        "webp": "image/webp",
        "bmp": "image/bmp",
    }.get(fmt.lower(), "application/octet-stream")

