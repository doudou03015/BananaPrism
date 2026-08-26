"""Best-effort daily logging with mandatory secret redaction."""

from __future__ import annotations

import re
import threading
from datetime import date, datetime
from pathlib import Path

from banana_prism.utils.paths import PathLike, get_logs_dir


REDACTION = "[REDACTED]"
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|console[_-]?password[_-]?hash|password|authorization|"
    r"access[_-]?token|refresh[_-]?token|credential|ciphertext|secret)"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_SHAPES = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-(?:or-v1-)?[A-Za-z0-9_-]{8,}|AIza[0-9A-Za-z_-]{20,})"
)
_LEVEL = re.compile(r"^[A-Za-z0-9_-]{1,10}$")
_PROCESS_SECRETS: set[str] = set()
_PROCESS_SECRETS_LOCK = threading.Lock()


def register_process_secret(value: str) -> None:
    """Register a resolved credential for redaction by every logger instance."""

    if value:
        with _PROCESS_SECRETS_LOCK:
            _PROCESS_SECRETS.add(value)


def unregister_process_secret(value: str) -> None:
    with _PROCESS_SECRETS_LOCK:
        _PROCESS_SECRETS.discard(value)


def redact_sensitive_text(message: object, sensitive_values: tuple[str, ...] = ()) -> str:
    """Remove credential material while preserving ordinary text layout."""

    text = str(message)
    for value in sorted((item for item in sensitive_values if item), key=len, reverse=True):
        text = text.replace(value, REDACTION)
    text = _BEARER.sub(f"Bearer {REDACTION}", text)
    text = _KEY_SHAPES.sub(REDACTION, text)

    def replace_key_value(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}{REDACTION}"

    text = _KEY_VALUE_SECRET.sub(replace_key_value, text)
    return text


def redact_for_display(message: object) -> str:
    """Redact registered process credentials before text reaches any UI."""

    with _PROCESS_SECRETS_LOCK:
        process_secrets = tuple(_PROCESS_SECRETS)
    return redact_sensitive_text(message, process_secrets)


def redact_log_message(message: object, sensitive_values: tuple[str, ...] = ()) -> str:
    """Return a single-line message with common and registered secrets removed."""

    text = redact_sensitive_text(message, sensitive_values)
    # Log forging is prevented after redaction so a secret split across lines
    # cannot create a plausible second log record.
    return text.replace("\r", r"\r").replace("\n", r"\n")


class LogService:
    """Write redacted log records; logging failures never crash the application."""

    def __init__(
        self,
        data_dir: PathLike | None = None,
        *,
        logs_dir: PathLike | None = None,
    ) -> None:
        if logs_dir is None:
            self._dir = get_logs_dir(data_dir)
        else:
            self._dir = Path(logs_dir).expanduser()
            if not self._dir.is_absolute():
                raise ValueError("logs_dir must be absolute")
            self._dir.mkdir(parents=True, exist_ok=True)
        self._current_date: date | None = None
        self._current_path: Path | None = None
        self._sensitive_values: set[str] = set()
        self._lock = threading.RLock()

    def register_secret(self, value: str) -> None:
        """Register an opaque credential whose format cannot be pattern-matched."""

        if value:
            with self._lock:
                self._sensitive_values.add(value)

    def unregister_secret(self, value: str) -> None:
        with self._lock:
            self._sensitive_values.discard(value)

    def _get_log_path(self, *, on_date: date | None = None) -> Path:
        today = on_date or date.today()
        if self._current_date != today or self._current_path is None:
            self._current_date = today
            self._current_path = self._dir / f"bananaprism_{today.isoformat()}.log"
        return self._current_path

    def write(self, level: str, message: object) -> bool:
        """Append a timestamped, redacted entry and return whether it succeeded."""

        try:
            with self._lock:
                safe_level = level.upper() if _LEVEL.fullmatch(level) else "INFO"
                with _PROCESS_SECRETS_LOCK:
                    process_secrets = tuple(_PROCESS_SECRETS)
                safe_message = redact_log_message(
                    message,
                    tuple(self._sensitive_values) + process_secrets,
                )
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                line = f"[{timestamp}] [{safe_level:<5}] {safe_message}\n"
                path = self._get_log_path()
                with path.open("a", encoding="utf-8", newline="") as stream:
                    stream.write(line)
                    stream.flush()
                return True
        except Exception:
            return False

    def debug(self, message: object) -> bool:
        return self.write("DEBUG", message)

    def info(self, message: object) -> bool:
        return self.write("INFO", message)

    def warning(self, message: object) -> bool:
        return self.write("WARN", message)

    def error(self, message: object) -> bool:
        return self.write("ERROR", message)


__all__ = [
    "LogService",
    "REDACTION",
    "redact_for_display",
    "redact_log_message",
    "redact_sensitive_text",
    "register_process_secret",
    "unregister_process_secret",
]
