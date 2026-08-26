"""Pure data contracts shared by services and UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class JobState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"


class QueueStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class GenerationRequest:
    prompt: str
    model_id: str
    model_short_name: str
    size: str
    ratio: str
    preset_id: str
    provider: str


@dataclass(slots=True)
class GenerationResult:
    image_bytes: bytes
    fmt: str
    width: int
    height: int
    request: GenerationRequest
    text_content: str = ""
    usage: TokenUsage | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    saved_path: str | None = None
    output_dpi: tuple[float, float] | None = None


@dataclass(slots=True)
class EditRequest:
    source_image_bytes: bytes
    annotated_image_bytes: bytes
    edit_prompt: str
    model_id: str
    model_short_name: str
    size: str
    ratio: str
    preset_id: str
    provider: str
    source_fmt: str = "png"
    source_dpi: tuple[float, float] | None = None
    wire_source_fmt: str | None = None


@dataclass(slots=True)
class EditResult:
    image_bytes: bytes
    fmt: str
    width: int
    height: int
    request: EditRequest
    text_content: str = ""
    usage: TokenUsage | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    saved_path: str | None = None
    output_dpi: tuple[float, float] | None = None


@dataclass(slots=True)
class QueuedTask:
    prompt: str
    preset_id: str
    model_index: int
    size_index: int
    ratio: str
    status: QueueStatus = QueueStatus.PENDING
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class ApiPreset:
    preset_id: str
    name: str
    provider: str
    credential_ref: str
