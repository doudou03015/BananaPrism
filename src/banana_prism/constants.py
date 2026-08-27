"""Application-wide constants and immutable product configuration."""

from __future__ import annotations

from dataclasses import dataclass


APP_ID = "BananaPrism"
APP_DISPLAY_NAME = "BananaPrism"
LEGACY_APP_ID = "NanaBananaStudio"
SETTINGS_SCHEMA_VERSION = 1
SECRET_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    provider_id: str
    label: str
    api_url: str
    key_url: str


@dataclass(frozen=True, slots=True)
class ModelSpec:
    label_key: str
    model_id: str
    short_name: str
    supported_sizes: tuple[str, ...] = ("1K", "2K", "4K")


@dataclass(frozen=True, slots=True)
class AnnotationColorSpec:
    """One consistently rendered and described edit-guide colour."""

    color_id: str
    label_key: str
    fill_rgba: tuple[int, int, int, int]
    border_rgba: tuple[int, int, int, int]


API_PROVIDERS: dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        provider_id="openrouter",
        label="OpenRouter",
        api_url="https://openrouter.ai/api/v1/chat/completions",
        key_url="https://openrouter.ai/settings/keys",
    ),
    "aihubmix": ProviderSpec(
        provider_id="aihubmix",
        label="AiHubMix",
        api_url="https://api.aihubmix.com/gemini/v1beta/models/{model}:streamGenerateContent",
        key_url="https://aihubmix.com/user/token",
    ),
}

MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        "model.gemini_31_flash",
        "google/gemini-3.1-flash-image",
        "NanoBanana2",
    ),
    ModelSpec(
        "model.gemini_25_flash",
        "google/gemini-2.5-flash-image",
        "NanoBanana",
        ("1K",),
    ),
    ModelSpec(
        "model.gemini_3_pro",
        "google/gemini-3-pro-image",
        "NanoBananaPro",
    ),
)

SIZES = ("1K", "2K", "4K")
RATIOS = ("16:9", "1:1", "4:3", "3:2", "9:16", "3:4")
DEFAULT_RATIO = "1:1"
SIZE_DIMENSIONS: dict[str, dict[str, tuple[int, int]]] = {
    "1K": {
        "16:9": (1376, 768), "9:16": (768, 1376), "1:1": (1024, 1024),
        "4:3": (1184, 888), "3:2": (1248, 832), "3:4": (888, 1184),
    },
    "2K": {
        "16:9": (2752, 1536), "9:16": (1536, 2752), "1:1": (2048, 2048),
        "4:3": (2368, 1776), "3:2": (2496, 1664), "3:4": (1776, 2368),
    },
    "4K": {
        "16:9": (5504, 3072), "9:16": (3072, 5504), "1:1": (4096, 4096),
        "4:3": (4736, 3552), "3:2": (4992, 3328), "3:4": (3552, 4736),
    },
}

REQUEST_TIMEOUT_MS = 300_000
REMOTE_IMAGE_TIMEOUT_MS = 120_000
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
PROMPT_SAVE_LENGTH_DEFAULT = 30
MAX_FILENAME_LENGTH = 220
DEFAULT_BRUSH_RADIUS = 30
MAX_UNDO_COMMANDS = 30
ZOOM_MIN = 0.05
ZOOM_MAX = 10.0
ZOOM_STEP = 0.15
DEFAULT_ANNOTATION_COLOR = "red"
ANNOTATION_COLORS: dict[str, AnnotationColorSpec] = {
    "red": AnnotationColorSpec(
        "red", "main.annotation.color.red", (255, 45, 45, 150), (255, 78, 78, 255)
    ),
    "green": AnnotationColorSpec(
        "green", "main.annotation.color.green", (0, 225, 105, 150), (55, 255, 145, 255)
    ),
    "magenta": AnnotationColorSpec(
        "magenta", "main.annotation.color.magenta", (255, 0, 205, 150), (255, 80, 225, 255)
    ),
    "cyan": AnnotationColorSpec(
        "cyan", "main.annotation.color.cyan", (0, 205, 255, 150), (75, 225, 255, 255)
    ),
    "yellow": AnnotationColorSpec(
        "yellow", "main.annotation.color.yellow", (245, 213, 71, 140), (255, 230, 90, 255)
    ),
}
# Compatibility aliases for code that has not selected a colour explicitly.
ANNOTATION_COLOR = ANNOTATION_COLORS[DEFAULT_ANNOTATION_COLOR].fill_rgba
ANNOTATION_BORDER_COLOR = ANNOTATION_COLORS[DEFAULT_ANNOTATION_COLOR].border_rgba
OUTPUT_DPI = 300.0
