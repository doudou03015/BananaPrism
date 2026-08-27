"""Transactional image and metadata sidecar persistence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QImageReader

from banana_prism import __version__
from banana_prism.constants import (
    ANNOTATION_COLORS,
    API_PROVIDERS,
    MAX_FILENAME_LENGTH,
)
from banana_prism.models import EditResult, GenerationResult
from banana_prism.services.log_service import redact_for_display
from banana_prism.utils.paths import PathLike, get_default_save_dir

if TYPE_CHECKING:
    from banana_prism.services.settings_service import SettingsService


_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_UNDERSCORES = re.compile(r"_+")
_WINDOWS_RESERVED = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE
)
_ALLOWED_FORMATS = frozenset({"png", "jpg", "jpeg", "webp", "bmp"})
# Keep every path comfortably below legacy Win32 MAX_PATH.  PyInstaller and
# Python normally opt into long paths, but user policy and filesystem APIs can
# still differ; a conservative limit also leaves room for a terminating NUL.
_WINDOWS_SAFE_PATH_UNITS = 240
_TRANSACTION_DIR_PLACEHOLDER = ".bp-" + ("x" * 16) + ".txn"


class FileSaveError(RuntimeError):
    pass


def _utf16_units(value: object) -> int:
    """Return the number of Windows UTF-16 code units used by *value*."""

    return len(str(value).encode("utf-16-le", errors="surrogatepass")) // 2


def _paths_fit_windows_budget(paths: tuple[Path, ...]) -> bool:
    return all(_utf16_units(path) <= _WINDOWS_SAFE_PATH_UNITS for path in paths)


def _fit_result_stem(
    directory: Path,
    requested_stem: str,
    suffix_text: str,
    extension: str,
) -> str:
    """Fit the repeated container/file stem to the complete destination path."""

    stem = requested_stem
    while stem:
        final_stem = f"{stem}{suffix_text}"
        image_name = f"{final_stem}{extension}"
        sidecar_name = f"{final_stem}.json"
        container = directory / final_stem
        candidates = (
            container,
            container / image_name,
            container / sidecar_name,
            directory / f".{final_stem}.reserve",
            directory / _TRANSACTION_DIR_PLACEHOLDER / image_name,
            directory / _TRANSACTION_DIR_PLACEHOLDER / sidecar_name,
            directory / image_name,
            directory / sidecar_name,
        )
        if (
            _utf16_units(image_name) <= MAX_FILENAME_LENGTH
            and _utf16_units(sidecar_name) <= MAX_FILENAME_LENGTH
            and _paths_fit_windows_budget(candidates)
        ):
            return final_stem
        stem = stem[:-1].rstrip("_. ")
    raise FileSaveError(
        "configured save directory path is too long for a safe result transaction"
    )


def sanitize_filename_component(text: object) -> str:
    value = _ILLEGAL.sub("_", str(text))
    value = _WHITESPACE.sub("_", value).strip("_. ")
    value = _UNDERSCORES.sub("_", value)
    if not value:
        return "unnamed"
    if _WINDOWS_RESERVED.match(value):
        value = f"_{value}"
    return value


def _normalise_format(fmt: str) -> str:
    value = fmt.strip().lower().lstrip(".")
    if value == "jpeg":
        return "jpg"
    if value not in _ALLOWED_FORMATS:
        raise FileSaveError("unsupported image format")
    return value


def _inspect_encoded_image(
    image_bytes: bytes,
) -> tuple[str, int, int, tuple[float, float] | None]:
    """Decode persisted bytes and return their real container metadata."""

    if not image_bytes:
        raise FileSaveError("refusing to inspect empty image data")
    data = QByteArray(image_bytes)
    buffer = QBuffer(data)
    if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
        raise FileSaveError("could not open image verification buffer")
    reader = QImageReader(buffer)
    detected = bytes(reader.format()).decode("ascii", "ignore").lower()
    image = reader.read()
    error = reader.errorString()
    buffer.close()
    if image.isNull() or not detected:
        raise FileSaveError(f"encoded image verification failed: {error}")
    try:
        normalised = _normalise_format(detected)
    except FileSaveError as exc:
        raise FileSaveError("encoded image format is unsupported") from exc
    if image.width() <= 0 or image.height() <= 0:
        raise FileSaveError("encoded image dimensions are invalid")
    dpm = (image.dotsPerMeterX(), image.dotsPerMeterY())
    dpi = (
        (dpm[0] * 0.0254, dpm[1] * 0.0254)
        if dpm[0] > 0 and dpm[1] > 0
        else None
    )
    return normalised, image.width(), image.height(), dpi


def _write_exact(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise


def _publish_transaction(temporary: Path, destination: Path) -> None:
    """Atomically publish a complete result directory without replacement."""

    try:
        os.rename(temporary, destination)
    except FileExistsError:
        raise
    except OSError as exc:
        raise FileSaveError("filesystem cannot atomically publish the result directory") from exc


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FileService:
    """Save encoded image bytes and a verified JSON commit sidecar."""

    def __init__(
        self,
        settings: SettingsService | None = None,
        *,
        save_dir: PathLike | None = None,
    ) -> None:
        self._settings = settings
        self._save_dir_override = Path(save_dir).expanduser() if save_dir is not None else None
        if self._save_dir_override is not None and not self._save_dir_override.is_absolute():
            raise ValueError("save_dir must be absolute")
        self._lock = threading.RLock()

    def _directory(self) -> Path:
        if self._save_dir_override is not None:
            directory = self._save_dir_override
        elif self._settings is not None:
            directory = Path(self._settings.save_dir).expanduser()
        else:
            directory = get_default_save_dir(create=False)
        if not directory.is_absolute():
            raise FileSaveError("configured save directory must be absolute")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @property
    def _prompt_length(self) -> int:
        return self._settings.prompt_save_length if self._settings is not None else 30

    def _build_filename(
        self,
        result: GenerationResult | EditResult,
        mode: Literal["t2i", "edit"],
    ) -> str:
        request = result.request
        prompt = redact_for_display(
            request.prompt if mode == "t2i" else request.edit_prompt
        )
        prompt_component = sanitize_filename_component(prompt[: self._prompt_length])
        if prompt_component == "unnamed":
            prompt_component = "no_prompt"
        ratio = sanitize_filename_component(request.ratio.replace(":", "x"))
        extension = _normalise_format(result.fmt)
        stem = "_".join(
            (
                result.timestamp.strftime("%Y%m%d_%H%M%S"),
                mode,
                sanitize_filename_component(request.model_short_name),
                sanitize_filename_component(request.size),
                ratio,
                prompt_component,
            )
        )
        overflow = len(stem) + len(extension) + 1 - MAX_FILENAME_LENGTH
        if overflow > 0:
            stem = stem[:-overflow].rstrip("_. ")
        if not stem:
            raise FileSaveError("generated filename is empty")
        return f"{stem}.{extension}"

    @staticmethod
    def _actual_dpi(
        result: GenerationResult | EditResult,
        decoded_dpi: tuple[float, float] | None,
    ) -> tuple[float, float] | None:
        if result.output_dpi is None:
            return None
        declared = (float(result.output_dpi[0]), float(result.output_dpi[1]))
        if not all(math.isfinite(value) and 0 < value <= 10_000 for value in declared):
            raise FileSaveError("output DPI is invalid")
        if decoded_dpi is None:
            return None
        if not all(math.isfinite(value) and 0 < value <= 10_000 for value in decoded_dpi):
            raise FileSaveError("decoded output DPI is invalid")
        # Preserve nominal values across integer dots-per-metre rounding, but
        # never claim a DPI that the encoded bytes do not actually read back.
        if all(abs(actual - expected) <= 0.1 for actual, expected in zip(decoded_dpi, declared)):
            return declared
        return tuple(round(value, 4) for value in decoded_dpi)

    @staticmethod
    def _usage(result: GenerationResult | EditResult) -> dict[str, int] | None:
        return asdict(result.usage) if result.usage is not None else None

    def _sidecar(
        self,
        result: GenerationResult | EditResult,
        mode: Literal["t2i", "edit"],
    ) -> dict[str, Any]:
        request = result.request
        if (
            isinstance(result.width, bool)
            or isinstance(result.height, bool)
            or not isinstance(result.width, int)
            or not isinstance(result.height, int)
            or result.width <= 0
            or result.height <= 0
        ):
            raise FileSaveError("image dimensions are invalid")
        if request.provider not in API_PROVIDERS:
            raise FileSaveError("result provider is unsupported")
        if not request.preset_id:
            raise FileSaveError("result preset id is missing")
        detected_fmt, decoded_width, decoded_height, decoded_dpi = _inspect_encoded_image(
            result.image_bytes
        )
        expected_fmt = _normalise_format(result.fmt)
        if detected_fmt != expected_fmt:
            raise FileSaveError("result format does not match encoded image bytes")
        if (decoded_width, decoded_height) != (result.width, result.height):
            raise FileSaveError("result dimensions do not match encoded image bytes")
        digest = hashlib.sha256(result.image_bytes).hexdigest()
        dpi = self._actual_dpi(result, decoded_dpi)
        requested_output_format = str(request.requested_output_format).strip().lower()
        if requested_output_format not in {"png", "jpeg"}:
            raise FileSaveError("requested output format is unsupported")
        requested_dpi = request.requested_dpi
        if (
            isinstance(requested_dpi, bool)
            or not isinstance(requested_dpi, (int, float))
            or not math.isfinite(float(requested_dpi))
            or float(requested_dpi) not in {72.0, 96.0, 150.0, 300.0}
        ):
            raise FileSaveError("requested output DPI is unsupported")
        common: dict[str, Any] = {
            "schema_version": 1,
            "app_version": __version__,
            "mode": "txt2img" if mode == "t2i" else "edit",
            "model": request.model_id,
            "model_short": request.model_short_name,
            "size": request.size,
            "ratio": request.ratio,
            "provider": request.provider,
            "preset_id": request.preset_id,
            "text_content": redact_for_display(result.text_content),
            "saved_fmt": _normalise_format(result.fmt),
            "requested_output_format": requested_output_format,
            "requested_dpi": float(requested_dpi),
            "actual_output_format": detected_fmt,
            "actual_dpi": list(dpi) if dpi is not None else None,
            "width": result.width,
            "height": result.height,
            "timestamp": result.timestamp.isoformat(),
            "image_sha256": digest,
            "output_dpi": list(dpi) if dpi is not None else None,
            "token_usage": self._usage(result),
        }
        if isinstance(result, GenerationResult):
            common["prompt"] = redact_for_display(request.prompt)
            common["fmt"] = _normalise_format(result.fmt)
        else:
            annotation_color = str(request.annotation_color).strip().lower()
            if annotation_color not in ANNOTATION_COLORS:
                raise FileSaveError("annotation color is unsupported")
            common["edit_prompt"] = redact_for_display(request.edit_prompt)
            common["annotation_color"] = annotation_color
            common["selection_mask_format"] = (
                "png" if request.selection_mask_bytes else None
            )
            common["source_fmt"] = _normalise_format(request.source_fmt)
            common["wire_source_fmt"] = (
                _normalise_format(request.wire_source_fmt)
                if request.wire_source_fmt
                else _normalise_format(request.source_fmt)
            )
            common["source_dpi"] = (
                list(request.source_dpi) if request.source_dpi is not None else None
            )
            # This is deliberately the actual encoded output DPI, not the old
            # implementation's incorrect copy of source_dpi.
            common["saved_dpi"] = list(dpi) if dpi is not None else None
        return common

    @staticmethod
    def _reserve(directory: Path, filename: str) -> tuple[Path, Path, Path]:
        base = Path(filename)
        for suffix in range(10_000):
            suffix_text = f"_{suffix}" if suffix else ""
            stem = _fit_result_stem(
                directory,
                base.stem,
                suffix_text,
                base.suffix,
            )
            candidate_name = f"{stem}{base.suffix}"
            container = directory / stem
            image = container / candidate_name
            sidecar = container / f"{stem}.json"
            reservation = directory / f".{container.name}.reserve"
            # Also avoid colliding with files produced by pre-v1 flat layouts.
            if (
                container.exists()
                or (directory / candidate_name).exists()
                or (directory / f"{stem}.json").exists()
            ):
                continue
            try:
                descriptor = os.open(
                    reservation,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                continue
            else:
                os.close(descriptor)
                if container.exists():
                    reservation.unlink(missing_ok=True)
                    continue
                return image, sidecar, reservation
        raise FileSaveError("could not reserve a unique filename")

    @staticmethod
    def _commit_pair(
        image_path: Path,
        sidecar_path: Path,
        reservation: Path,
        image_bytes: bytes,
        sidecar: dict[str, Any],
    ) -> None:
        if not image_bytes:
            reservation.unlink(missing_ok=True)
            raise FileSaveError("refusing to save empty image data")
        transaction: Path | None = None
        published = False
        try:
            encoded_sidecar = (
                json.dumps(
                    sidecar,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            transaction = Path(
                tempfile.mkdtemp(
                    dir=image_path.parent.parent,
                    prefix=".bp-",
                    suffix=".txn",
                )
            )
            image_temp = transaction / image_path.name
            sidecar_temp = transaction / sidecar_path.name
            _write_exact(image_temp, image_bytes)
            _write_exact(sidecar_temp, encoded_sidecar)
            if image_temp.read_bytes() != image_bytes:
                raise FileSaveError("temporary image verification failed")
            try:
                decoded_sidecar = json.loads(sidecar_temp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise FileSaveError("temporary sidecar verification failed") from exc
            if decoded_sidecar != sidecar:
                raise FileSaveError("temporary sidecar verification failed")
            if decoded_sidecar.get("image_sha256") != hashlib.sha256(image_bytes).hexdigest():
                raise FileSaveError("image and sidecar digest differ")
            _fsync_directory(transaction)
            _publish_transaction(transaction, image_path.parent)
            transaction = None
            published = True
            _fsync_directory(image_path.parent.parent)
            if image_path.read_bytes() != image_bytes:
                raise FileSaveError("published image verification failed")
            persisted_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if persisted_sidecar != sidecar:
                raise FileSaveError("published sidecar verification failed")
        except Exception as exc:
            if published:
                sidecar_path.unlink(missing_ok=True)
                image_path.unlink(missing_ok=True)
                try:
                    image_path.parent.rmdir()
                except OSError:
                    pass
            if isinstance(exc, FileSaveError):
                raise
            raise FileSaveError("image transaction failed") from exc
        finally:
            if transaction is not None:
                (transaction / image_path.name).unlink(missing_ok=True)
                (transaction / sidecar_path.name).unlink(missing_ok=True)
                try:
                    transaction.rmdir()
                except OSError:
                    pass
            reservation.unlink(missing_ok=True)

    def _save(
        self,
        result: GenerationResult | EditResult,
        mode: Literal["t2i", "edit"],
    ) -> Path:
        with self._lock:
            directory = self._directory()
            filename = self._build_filename(result, mode)
            metadata = self._sidecar(result, mode)
            image_path, sidecar_path, reservation = self._reserve(directory, filename)
            self._commit_pair(
                image_path,
                sidecar_path,
                reservation,
                result.image_bytes,
                metadata,
            )
            result.saved_path = str(image_path)
            return image_path

    def auto_save(self, result: GenerationResult) -> Path:
        return self._save(result, "t2i")

    def auto_save_edit(self, result: EditResult) -> Path:
        return self._save(result, "edit")

    def save_generation(self, result: GenerationResult) -> Path:
        return self.auto_save(result)

    def save_edit(self, result: EditResult) -> Path:
        return self.auto_save_edit(result)

    def save_result(
        self,
        result: GenerationResult | EditResult,
        *,
        automatic: bool = True,
    ) -> Path | None:
        if (
            automatic
            and self._settings is not None
            and not bool(self._settings.get("auto_save", True))
        ):
            return None
        if isinstance(result, GenerationResult):
            return self.auto_save(result)
        if isinstance(result, EditResult):
            return self.auto_save_edit(result)
        raise TypeError("result must be a GenerationResult or EditResult")

    def save(self, result: GenerationResult | EditResult) -> Path | None:
        return self.save_result(result)

    def save_image_result(self, result: GenerationResult | EditResult) -> Path | None:
        return self.save_result(result)

    def save_imported(self, image_bytes: bytes, fmt: str) -> Path:
        with self._lock:
            directory = self._directory()
            extension = _normalise_format(fmt)
            detected_fmt, width, height, source_dpi = _inspect_encoded_image(image_bytes)
            if detected_fmt != extension:
                raise FileSaveError("import format does not match encoded image bytes")
            timestamp = datetime.now()
            filename = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_imported.{extension}"
            image_path, sidecar_path, reservation = self._reserve(directory, filename)
            sidecar = {
                "schema_version": 1,
                "app_version": __version__,
                "mode": "imported",
                "source_fmt": extension,
                "saved_fmt": extension,
                "width": width,
                "height": height,
                "source_dpi": list(source_dpi) if source_dpi is not None else None,
                "output_dpi": list(source_dpi) if source_dpi is not None else None,
                "timestamp": timestamp.isoformat(),
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
            }
            self._commit_pair(
                image_path,
                sidecar_path,
                reservation,
                image_bytes,
                sidecar,
            )
            return image_path

    @staticmethod
    def is_complete(image_path: PathLike) -> bool:
        path = Path(image_path)
        sidecar_path = path.with_suffix(".json")
        try:
            metadata = json.loads(sidecar_path.read_text(encoding="utf-8"))
            expected = metadata["image_sha256"]
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, KeyError, json.JSONDecodeError, TypeError):
            return False
        return isinstance(expected, str) and expected == actual


__all__ = [
    "FileSaveError",
    "FileService",
    "sanitize_filename_component",
]
