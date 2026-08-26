"""Crash-resumable migration from NanaBananaStudio's unversioned settings.

The migration commits and verifies a DPAPI-encrypted backup and secret store,
then commits non-secret settings, and only then scrubs plaintext legacy fields.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, cast

from banana_prism.constants import (
    API_PROVIDERS,
    DEFAULT_RATIO,
    LEGACY_APP_ID,
    MODELS,
    PROMPT_SAVE_LENGTH_DEFAULT,
    RATIOS,
    SECRET_SCHEMA_VERSION,
    SIZES,
)
from banana_prism.services.secret_store import (
    MAX_LEGACY_BACKUP_BYTES,
    SecretStore,
    atomic_write_bytes,
    new_secret_document,
)
from banana_prism.services.settings_service import (
    MAX_PRESETS,
    SettingsValidationError,
    default_settings_document,
    read_settings_document,
    write_settings_document,
)
from banana_prism.utils.paths import (
    DATA_DIR_ENV,
    PathLike,
    get_app_data_dir,
    get_legacy_app_data_dir,
)


LEGACY_PBKDF2_ITERATIONS = 260_000
MAX_LEGACY_CREDENTIAL_CHARS = 4096
_TOMBSTONE_KEY = "_bananaprism_migration"
_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCAL_LOCKS: dict[str, threading.Lock] = {}


class MigrationError(RuntimeError):
    pass


class LegacySettingsValidationError(MigrationError, ValueError):
    pass


class MigrationStateError(MigrationError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationResult:
    status: str
    migrated: bool
    cleanup_complete: bool
    active_preset_repaired: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


@contextmanager
def _migration_lock(path: Path) -> Iterator[None]:
    """Cross-process advisory lock with an additional same-process guard."""

    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        local_lock = _PROCESS_LOCAL_LOCKS.setdefault(key, threading.Lock())
    with local_lock:
        stream = path.open("a+b")
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _legacy_int(
    document: dict[str, Any],
    field: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = document.get(field, default)
    if isinstance(value, bool):
        raise LegacySettingsValidationError(f"legacy {field} is invalid")
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise LegacySettingsValidationError(f"legacy {field} is invalid") from exc
    if not minimum <= converted <= maximum:
        raise LegacySettingsValidationError(f"legacy {field} is out of range")
    return converted


def _legacy_password_record(value: object) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise LegacySettingsValidationError("legacy password verifier is invalid")
    try:
        salt_hex, digest_hex = value.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except (ValueError, TypeError) as exc:
        raise LegacySettingsValidationError("legacy password verifier is malformed") from exc
    if len(salt) != 32 or len(digest) != 32:
        raise LegacySettingsValidationError("legacy password verifier length is invalid")
    return {
        "scheme": "pbkdf2-hmac-sha256",
        "iterations": LEGACY_PBKDF2_ITERATIONS,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "dk_b64": base64.b64encode(digest).decode("ascii"),
    }


def _legacy_presets(
    document: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    raw_presets: object
    if "api_presets" in document and document["api_presets"] is not None:
        raw_presets = document["api_presets"]
        if not isinstance(raw_presets, list):
            raise LegacySettingsValidationError("legacy api_presets must be a list")
    else:
        legacy_key = document.get("api_key", "")
        if not isinstance(legacy_key, str):
            raise LegacySettingsValidationError("legacy api_key must be text")
        raw_presets = (
            [
                {
                    "id": "preset_0",
                    "name": "默认 (OpenRouter)",
                    "provider": "openrouter",
                    "api_key": legacy_key,
                }
            ]
            if legacy_key
            else []
        )
    if len(raw_presets) > MAX_PRESETS:
        raise LegacySettingsValidationError("legacy preset count is excessive")

    presets: list[dict[str, str]] = []
    credentials: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    for raw in raw_presets:
        if not isinstance(raw, dict):
            raise LegacySettingsValidationError("legacy preset entry must be an object")
        if not {"id", "name", "provider", "api_key"}.issubset(raw):
            raise LegacySettingsValidationError("legacy preset entry is incomplete")
        preset_id = raw.get("id")
        name = raw.get("name")
        provider = raw.get("provider")
        api_key = raw.get("api_key")
        if (
            not isinstance(preset_id, str)
            or not preset_id
            or len(preset_id) > 128
            or any(ord(character) < 32 for character in preset_id)
            or preset_id in seen
        ):
            raise LegacySettingsValidationError("legacy preset id is invalid or duplicated")
        seen.add(preset_id)
        if not isinstance(name, str) or len(name) > 256:
            raise LegacySettingsValidationError("legacy preset name is invalid")
        name = name.strip() or "未命名配置"
        if provider not in API_PROVIDERS:
            raise LegacySettingsValidationError("legacy provider is unsupported")
        if (
            not isinstance(api_key, str)
            or len(api_key) > MAX_LEGACY_CREDENTIAL_CHARS
        ):
            raise LegacySettingsValidationError("legacy credential is invalid")
        reference = f"api:{preset_id}" if api_key else ""
        presets.append(
            {
                "id": preset_id,
                "name": name,
                "provider": cast(str, provider),
                "credential_ref": reference,
            }
        )
        if api_key:
            credentials[reference] = {
                "kind": "api_key",
                "provider": cast(str, provider),
                "value": api_key,
            }
    return presets, credentials


class MigrationService:
    """Migrate legacy settings without ever creating a plaintext backup."""

    def __init__(
        self,
        data_dir: PathLike | None = None,
        *,
        settings_path: PathLike | None = None,
        legacy_data_dir: PathLike | None = None,
        legacy_settings_path: PathLike | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        environment_injected = DATA_DIR_ENV in os.environ
        explicitly_injected = (
            data_dir is not None
            or settings_path is not None
            or secret_store is not None
            or environment_injected
        )
        if data_dir is None and settings_path is None and secret_store is not None:
            self.data_dir = secret_store.path.parent
            self.data_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.data_dir = get_app_data_dir(data_dir)
        self.settings_path = (
            Path(settings_path).expanduser()
            if settings_path is not None
            else self.data_dir / "settings.json"
        )
        if not self.settings_path.is_absolute():
            raise ValueError("settings_path must be absolute")
        if legacy_settings_path is not None:
            self.legacy_settings_path: Path | None = Path(legacy_settings_path).expanduser()
            if not self.legacy_settings_path.is_absolute():
                raise ValueError("legacy_settings_path must be absolute")
        elif legacy_data_dir is not None:
            self.legacy_settings_path = (
                get_legacy_app_data_dir(legacy_data_dir, create=False) / "settings.json"
            )
        elif explicitly_injected:
            # Critical test/portable-mode invariant: never probe the real roaming
            # profile when the new data directory was injected.
            self.legacy_settings_path = None
        else:
            self.legacy_settings_path = (
                get_legacy_app_data_dir(create=False) / "settings.json"
            )
        self.secret_store = secret_store or SecretStore(self.data_dir)
        self.lock_path = self.data_dir / ".migration.lock"

    def _read_legacy(self) -> tuple[bytes, dict[str, Any]]:
        assert self.legacy_settings_path is not None
        try:
            size = self.legacy_settings_path.stat().st_size
        except OSError as exc:
            raise MigrationError("legacy settings could not be inspected") from exc
        if not 1 <= size <= MAX_LEGACY_BACKUP_BYTES:
            raise LegacySettingsValidationError("legacy settings size is invalid")
        try:
            raw = self.legacy_settings_path.read_bytes()
            document = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LegacySettingsValidationError(
                "legacy settings are not valid UTF-8 JSON"
            ) from exc
        if not isinstance(document, dict):
            raise LegacySettingsValidationError("legacy settings root must be an object")
        return raw, document

    def _build_documents(
        self,
        raw: bytes,
        legacy: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        fingerprint = hashlib.sha256(raw).hexdigest()
        presets, credentials = _legacy_presets(legacy)
        password_record = _legacy_password_record(legacy.get("console_password_hash"))
        settings = default_settings_document()
        settings["last_model_index"] = _legacy_int(
            legacy, "last_model_index", 0, minimum=0, maximum=len(MODELS) - 1
        )
        settings["last_size_index"] = _legacy_int(
            legacy, "last_size_index", 0, minimum=0, maximum=len(SIZES) - 1
        )
        ratio = legacy.get("last_ratio", DEFAULT_RATIO)
        if ratio not in RATIOS:
            raise LegacySettingsValidationError("legacy ratio is unsupported")
        settings["last_ratio"] = ratio
        save_dir = legacy.get("save_dir", settings["save_dir"])
        if (
            not isinstance(save_dir, str)
            or not save_dir.strip()
            or len(save_dir) > 4096
            or "\x00" in save_dir
            or not Path(save_dir).expanduser().is_absolute()
        ):
            raise LegacySettingsValidationError("legacy save_dir is invalid")
        settings["save_dir"] = save_dir
        prompt_length = _legacy_int(
            legacy,
            "prompt_save_length",
            PROMPT_SAVE_LENGTH_DEFAULT,
            minimum=-(2**31),
            maximum=2**31 - 1,
        )
        settings["prompt_save_length"] = max(10, min(100, prompt_length))
        legacy_today = legacy.get("today_gen")
        if legacy_today is not None:
            if not isinstance(legacy_today, dict):
                raise LegacySettingsValidationError("legacy today_gen is invalid")
            today_date = legacy_today.get("date")
            count = legacy_today.get("count", 0)
            try:
                date.fromisoformat(today_date)
            except (TypeError, ValueError) as exc:
                raise LegacySettingsValidationError("legacy today_gen date is invalid") from exc
            if isinstance(count, bool):
                raise LegacySettingsValidationError("legacy today_gen count is invalid")
            try:
                count = int(count)
            except (TypeError, ValueError) as exc:
                raise LegacySettingsValidationError("legacy today_gen count is invalid") from exc
            if count < 0:
                raise LegacySettingsValidationError("legacy today_gen count is invalid")
            settings["today_gen"] = {"date": today_date, "count": count}
        settings["api_presets"] = presets
        requested_active = legacy.get("active_preset_id", "")
        if not isinstance(requested_active, str):
            raise LegacySettingsValidationError("legacy active preset id is invalid")
        ids = {preset["id"] for preset in presets}
        repaired = bool(requested_active and requested_active not in ids)
        active = requested_active if requested_active in ids else (presets[0]["id"] if presets else "")
        settings["active_preset_id"] = active
        settings["console_password_configured"] = password_record is not None

        secrets = new_secret_document()
        secrets["credentials"] = credentials
        secrets["console_auth"] = password_record
        secrets["migration_backup"] = {
            "source": LEGACY_APP_ID,
            "source_sha256": fingerprint,
            "captured_at": _utc_now(),
            "payload_b64": base64.b64encode(raw).decode("ascii"),
        }
        generation = secrets["generation"]
        started = _utc_now()
        settings["secret_store"] = {
            "schema_version": SECRET_SCHEMA_VERSION,
            "generation": generation,
        }
        settings["migration"] = {
            "source": LEGACY_APP_ID,
            "version": 1,
            "status": "cleanup_pending",
            "source_sha256": fingerprint,
            "secret_generation": generation,
            "started_at": started,
            "active_preset_repaired": repaired,
        }
        return settings, secrets, repaired

    def _verified_existing_secret(
        self,
        fingerprint: str,
        expected_secrets: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.secret_store.exists:
            return None
        existing = self.secret_store.load()
        backup = existing.get("migration_backup")
        if not isinstance(backup, dict) or backup.get("source_sha256") != fingerprint:
            raise MigrationStateError(
                "an unrelated secret store exists without committed settings"
            )
        if (
            existing.get("credentials") != expected_secrets.get("credentials")
            or existing.get("console_auth") != expected_secrets.get("console_auth")
            or backup.get("payload_b64")
            != expected_secrets["migration_backup"]["payload_b64"]
        ):
            raise MigrationStateError("existing migration secret store does not match source")
        return existing

    @staticmethod
    def _legacy_is_scrubbed(document: dict[str, Any], fingerprint: str) -> bool:
        tombstone = document.get(_TOMBSTONE_KEY)
        if not isinstance(tombstone, dict) or tombstone.get("source_sha256") != fingerprint:
            return False
        if "api_key" in document or "console_password_hash" in document:
            return False
        presets = document.get("api_presets")
        return not isinstance(presets, list) or all(
            not isinstance(item, dict) or "api_key" not in item for item in presets
        )

    def _scrub_legacy(self, fingerprint: str) -> str:
        if self.legacy_settings_path is None or not self.legacy_settings_path.exists():
            return "scrubbed"
        raw, document = self._read_legacy()
        current_fingerprint = hashlib.sha256(raw).hexdigest()
        if self._legacy_is_scrubbed(document, fingerprint):
            return "scrubbed"
        if current_fingerprint != fingerprint:
            return "conflict"
        scrubbed = copy.deepcopy(document)
        scrubbed.pop("api_key", None)
        scrubbed.pop("console_password_hash", None)
        presets = scrubbed.get("api_presets")
        if isinstance(presets, list):
            for preset in presets:
                if isinstance(preset, dict):
                    preset.pop("api_key", None)
        scrubbed[_TOMBSTONE_KEY] = {
            "source": LEGACY_APP_ID,
            "target": "BananaPrism",
            "version": 1,
            "status": "complete",
            "source_sha256": fingerprint,
            "completed_at": _utc_now(),
        }
        try:
            atomic_write_bytes(self.legacy_settings_path, _safe_json_bytes(scrubbed))
            reread = json.loads(self.legacy_settings_path.read_text(encoding="utf-8"))
            if not isinstance(reread, dict) or not self._legacy_is_scrubbed(
                reread, fingerprint
            ):
                raise MigrationError("legacy secret fields were not fully scrubbed")
        except Exception as exc:
            try:
                atomic_write_bytes(self.legacy_settings_path, raw)
            except Exception as rollback_exc:
                raise MigrationError(
                    "legacy cleanup failed and the original file could not be restored"
                ) from rollback_exc
            if isinstance(exc, MigrationError):
                raise
            raise MigrationError("scrubbed legacy settings failed verification") from exc
        return "scrubbed"

    def _finish_cleanup(self, settings: dict[str, Any]) -> MigrationResult:
        migration = settings["migration"]
        secret_document = self.secret_store.load()
        expected_generation = settings["secret_store"]["generation"]
        if secret_document["generation"] != expected_generation:
            raise MigrationStateError("settings reference a different secret generation")
        backup = secret_document.get("migration_backup")
        if (
            not isinstance(backup, dict)
            or backup.get("source_sha256") != migration["source_sha256"]
        ):
            raise MigrationStateError("encrypted migration backup is missing or mismatched")
        legacy_before: bytes | None = None
        if self.legacy_settings_path is not None and self.legacy_settings_path.exists():
            legacy_before = self.legacy_settings_path.read_bytes()
        outcome = self._scrub_legacy(migration["source_sha256"])
        if outcome == "conflict":
            return MigrationResult(
                status="legacy_conflict",
                migrated=False,
                cleanup_complete=False,
                active_preset_repaired=migration.get("active_preset_repaired", False),
            )
        if migration["status"] != "complete":
            completed = copy.deepcopy(settings)
            completed["migration"]["status"] = "complete"
            completed["migration"]["completed_at"] = _utc_now()
            try:
                write_settings_document(self.settings_path, completed)
            except Exception:
                if legacy_before is not None and self.legacy_settings_path is not None:
                    try:
                        atomic_write_bytes(self.legacy_settings_path, legacy_before)
                    except Exception as rollback_exc:
                        raise MigrationError(
                            "migration finalization failed and legacy rollback failed"
                        ) from rollback_exc
                raise
        return MigrationResult(
            status="complete",
            migrated=False,
            cleanup_complete=True,
            active_preset_repaired=migration.get("active_preset_repaired", False),
        )

    def migrate_if_needed(self) -> MigrationResult:
        with _migration_lock(self.lock_path):
            if self.settings_path.exists():
                settings = read_settings_document(self.settings_path)
                migration = settings.get("migration")
                if migration is None:
                    return MigrationResult("not_needed", False, True)
                return self._finish_cleanup(settings)

            if self.legacy_settings_path is None:
                return MigrationResult("legacy_probe_disabled", False, True)
            if not self.legacy_settings_path.exists():
                return MigrationResult("no_legacy", False, True)

            raw, legacy = self._read_legacy()
            settings, secrets, repaired = self._build_documents(raw, legacy)
            fingerprint = cast(str, settings["migration"]["source_sha256"])
            existing = self._verified_existing_secret(fingerprint, secrets)
            if existing is None:
                persisted_secret = self.secret_store.save(secrets)
            else:
                persisted_secret = existing
                generation = existing["generation"]
                settings["secret_store"]["generation"] = generation
                settings["migration"]["secret_generation"] = generation
            # Full secret verification precedes the non-secret commit.
            backup = persisted_secret.get("migration_backup")
            if (
                persisted_secret.get("credentials") != secrets["credentials"]
                or persisted_secret.get("console_auth") != secrets["console_auth"]
                or not isinstance(backup, dict)
                or backup.get("payload_b64") != secrets["migration_backup"]["payload_b64"]
            ):
                raise MigrationStateError("secret migration verification failed")
            write_settings_document(self.settings_path, settings)
            reread_settings = read_settings_document(self.settings_path)
            reread_secret = self.secret_store.load()
            if (
                reread_settings["secret_store"]["generation"]
                != reread_secret["generation"]
            ):
                raise MigrationStateError("committed migration generations differ")

            result = self._finish_cleanup(reread_settings)
            if result.cleanup_complete:
                return MigrationResult("migrated", True, True, repaired)
            return MigrationResult(result.status, True, False, repaired)


__all__ = [
    "LEGACY_PBKDF2_ITERATIONS",
    "LegacySettingsValidationError",
    "MigrationError",
    "MigrationResult",
    "MigrationService",
    "MigrationStateError",
]
