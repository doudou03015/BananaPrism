"""Validated, non-secret application settings.

Credentials and password verifiers never belong in this file.  They are stored
by :mod:`banana_prism.services.secret_store` and referenced by opaque IDs.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from banana_prism.constants import (
    API_PROVIDERS,
    DEFAULT_MODEL_INDEX,
    DEFAULT_RATIO,
    LEGACY_APP_ID,
    MODELS,
    PROMPT_SAVE_LENGTH_DEFAULT,
    RATIOS,
    SECRET_SCHEMA_VERSION,
    SETTINGS_SCHEMA_VERSION,
    SIZES,
)
from banana_prism.models import ApiPreset
from banana_prism.services.secret_store import SecretStore, atomic_write_bytes
from banana_prism.utils.paths import (
    DATA_DIR_ENV,
    PathLike,
    get_app_data_dir,
    get_default_save_dir,
    get_legacy_app_data_dir,
)


MAX_SETTINGS_BYTES = 1024 * 1024
MAX_PRESETS = 100
_OUTPUT_FORMATS = frozenset({"png", "jpeg"})
_OUTPUT_DPIS = frozenset({72, 96, 150, 300})
_ANNOTATION_COLORS = frozenset({"red", "green", "magenta", "cyan", "yellow"})
_FORBIDDEN_SECRET_FIELDS = frozenset(
    {"api_key", "console_password_hash", "password", "token", "secret"}
)


class SettingsError(RuntimeError):
    pass


class SettingsValidationError(SettingsError, ValueError):
    pass


class SettingsPersistenceError(SettingsError):
    pass


class SettingsSecretBindingError(SettingsError):
    pass


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_non_secret_tree(value: object, *, path: str = "settings") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise SettingsValidationError(f"{path} contains a non-string key")
            if key.casefold() in _FORBIDDEN_SECRET_FIELDS:
                raise SettingsValidationError(f"{path} contains forbidden secret field {key!r}")
            _validate_non_secret_tree(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_non_secret_tree(child, path=f"{path}[{index}]")


def _validate_iso_timestamp(value: object, field: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise SettingsValidationError(f"{field} must be an ISO timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SettingsValidationError(f"{field} must be an ISO timestamp") from exc


def _validate_secret_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "generation"}:
        raise SettingsValidationError("secret_store metadata is invalid")
    if value.get("schema_version") != SECRET_SCHEMA_VERSION:
        raise SettingsValidationError("secret_store schema version is unsupported")
    generation = value.get("generation")
    if not isinstance(generation, str):
        raise SettingsValidationError("secret_store generation is invalid")
    try:
        uuid.UUID(generation)
    except (ValueError, AttributeError) as exc:
        raise SettingsValidationError("secret_store generation is invalid") from exc
    return copy.deepcopy(value)


def _validate_migration(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SettingsValidationError("migration must be an object")
    allowed = {
        "source",
        "version",
        "status",
        "source_sha256",
        "secret_generation",
        "started_at",
        "completed_at",
        "active_preset_repaired",
    }
    if not set(value).issubset(allowed):
        raise SettingsValidationError("migration contains unsupported fields")
    required = {
        "source",
        "version",
        "status",
        "source_sha256",
        "secret_generation",
        "started_at",
    }
    if not required.issubset(value):
        raise SettingsValidationError("migration is missing required fields")
    if value.get("source") != LEGACY_APP_ID or value.get("version") != 1:
        raise SettingsValidationError("migration source or version is invalid")
    if value.get("status") not in {"cleanup_pending", "complete"}:
        raise SettingsValidationError("migration status is invalid")
    fingerprint = value.get("source_sha256")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise SettingsValidationError("migration source fingerprint is invalid")
    generation = value.get("secret_generation")
    if not isinstance(generation, str):
        raise SettingsValidationError("migration secret generation is invalid")
    try:
        uuid.UUID(generation)
    except (ValueError, AttributeError) as exc:
        raise SettingsValidationError("migration secret generation is invalid") from exc
    _validate_iso_timestamp(value.get("started_at"), "migration.started_at")
    if "completed_at" in value:
        _validate_iso_timestamp(value["completed_at"], "migration.completed_at")
    if "active_preset_repaired" in value and not isinstance(
        value["active_preset_repaired"], bool
    ):
        raise SettingsValidationError("migration repair flag is invalid")
    return copy.deepcopy(value)


def validate_settings_document(document: object) -> dict[str, Any]:
    """Validate the complete v1 non-secret settings schema."""

    if not isinstance(document, dict):
        raise SettingsValidationError("settings must be a JSON object")
    _validate_non_secret_tree(document)
    allowed = {
        "schema_version",
        "last_model_index",
        "last_size_index",
        "last_ratio",
        "last_output_format",
        "last_output_dpi",
        "last_annotation_color",
        "save_dir",
        "prompt_save_length",
        "today_gen",
        "api_presets",
        "active_preset_id",
        "console_password_configured",
        "auto_save",
        "confirm_requests",
        "remember_geometry",
        "secret_store",
        "migration",
    }
    # The three output/annotation preferences were added while schema v1 was
    # already in the field.  Keep them optional on read so existing installs
    # upgrade in place instead of being rejected as corrupt settings.
    optional = {
        "secret_store",
        "migration",
        "last_output_format",
        "last_output_dpi",
        "last_annotation_color",
    }
    required = allowed - optional
    if not set(document).issubset(allowed) or not required.issubset(document):
        raise SettingsValidationError("settings fields do not match the supported schema")
    if document.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        raise SettingsValidationError("settings schema version is unsupported")

    # These are display/export preferences rather than integrity-bearing
    # fields.  Recover only their known-safe values while retaining strict
    # validation for paths, presets, credentials and the rest of the schema.
    document = copy.deepcopy(document)
    output_format = document.get("last_output_format")
    document["last_output_format"] = (
        output_format if isinstance(output_format, str) and output_format in _OUTPUT_FORMATS else "png"
    )
    output_dpi = document.get("last_output_dpi")
    document["last_output_dpi"] = (
        int(output_dpi)
        if not isinstance(output_dpi, bool)
        and isinstance(output_dpi, (int, float))
        and float(output_dpi) in _OUTPUT_DPIS
        else 300
    )
    annotation_color = document.get("last_annotation_color")
    document["last_annotation_color"] = (
        annotation_color
        if isinstance(annotation_color, str) and annotation_color in _ANNOTATION_COLORS
        else "red"
    )

    model_index = document.get("last_model_index")
    if not _is_int(model_index) or not 0 <= cast(int, model_index) < len(MODELS):
        raise SettingsValidationError("last_model_index is out of range")
    size_index = document.get("last_size_index")
    if not _is_int(size_index) or not 0 <= cast(int, size_index) < len(SIZES):
        raise SettingsValidationError("last_size_index is out of range")
    if document.get("last_ratio") not in RATIOS:
        raise SettingsValidationError("last_ratio is unsupported")
    save_dir = document.get("save_dir")
    if (
        not isinstance(save_dir, str)
        or not save_dir.strip()
        or len(save_dir) > 4096
        or "\x00" in save_dir
        or not Path(save_dir).expanduser().is_absolute()
    ):
        raise SettingsValidationError("save_dir is invalid")
    prompt_length = document.get("prompt_save_length")
    if not _is_int(prompt_length) or not 10 <= cast(int, prompt_length) <= 100:
        raise SettingsValidationError("prompt_save_length must be between 10 and 100")

    today = document.get("today_gen")
    if not isinstance(today, dict) or set(today) != {"date", "count"}:
        raise SettingsValidationError("today_gen is invalid")
    try:
        date.fromisoformat(cast(str, today.get("date")))
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError("today_gen.date is invalid") from exc
    count = today.get("count")
    if not _is_int(count) or cast(int, count) < 0:
        raise SettingsValidationError("today_gen.count is invalid")

    presets = document.get("api_presets")
    if not isinstance(presets, list) or len(presets) > MAX_PRESETS:
        raise SettingsValidationError("api_presets must be a bounded list")
    preset_ids: set[str] = set()
    for preset in presets:
        if not isinstance(preset, dict) or set(preset) != {
            "id",
            "name",
            "provider",
            "credential_ref",
        }:
            raise SettingsValidationError("api preset fields are invalid")
        preset_id = preset.get("id")
        if (
            not isinstance(preset_id, str)
            or not preset_id
            or len(preset_id) > 128
            or any(ord(character) < 32 for character in preset_id)
            or preset_id in preset_ids
        ):
            raise SettingsValidationError("api preset id is invalid or duplicated")
        preset_ids.add(preset_id)
        name = preset.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 256:
            raise SettingsValidationError("api preset name is invalid")
        if preset.get("provider") not in API_PROVIDERS:
            raise SettingsValidationError("api preset provider is unsupported")
        reference = preset.get("credential_ref")
        if not isinstance(reference, str) or len(reference) > 260:
            raise SettingsValidationError("api preset credential_ref is invalid")
        if reference and reference != f"api:{preset_id}":
            raise SettingsValidationError("api preset credential_ref does not match its id")

    active = document.get("active_preset_id")
    if not isinstance(active, str) or (active and active not in preset_ids):
        raise SettingsValidationError("active_preset_id does not reference a preset")
    if not isinstance(document.get("console_password_configured"), bool):
        raise SettingsValidationError("console_password_configured must be boolean")
    for preference in ("auto_save", "confirm_requests", "remember_geometry"):
        if not isinstance(document.get(preference), bool):
            raise SettingsValidationError(f"{preference} must be boolean")
    if "secret_store" in document:
        _validate_secret_metadata(document["secret_store"])
    if (
        any(preset["credential_ref"] for preset in presets)
        or document["console_password_configured"]
    ) and "secret_store" not in document:
        raise SettingsValidationError("credential references require secret_store metadata")
    if "migration" in document:
        migration = _validate_migration(document["migration"])
        if "secret_store" not in document:
            raise SettingsValidationError("migrated settings require secret_store metadata")
        if migration["secret_generation"] != document["secret_store"]["generation"]:
            raise SettingsValidationError("migration and secret store generations differ")
    return copy.deepcopy(document)


def default_settings_document(*, on_date: date | None = None) -> dict[str, Any]:
    current_date = on_date or date.today()
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "last_model_index": DEFAULT_MODEL_INDEX,
        "last_size_index": 0,
        "last_ratio": DEFAULT_RATIO,
        "last_output_format": "png",
        "last_output_dpi": 300,
        "last_annotation_color": "red",
        "save_dir": str(get_default_save_dir(create=False)),
        "prompt_save_length": PROMPT_SAVE_LENGTH_DEFAULT,
        "today_gen": {"date": current_date.isoformat(), "count": 0},
        "api_presets": [],
        "active_preset_id": "",
        "console_password_configured": False,
        "auto_save": True,
        "confirm_requests": True,
        "remember_geometry": True,
    }


def read_settings_document(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise
    if not 1 <= size <= MAX_SETTINGS_BYTES:
        raise SettingsValidationError("settings file size is invalid")
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SettingsValidationError("settings file is not valid UTF-8 JSON") from exc
    return validate_settings_document(document)


def write_settings_document(path: Path, document: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_settings_document(dict(document))
    encoded = json.dumps(validated, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    if len(encoded) > MAX_SETTINGS_BYTES:
        raise SettingsValidationError("settings file is oversized")
    previous = path.read_bytes() if path.is_file() else None
    try:
        atomic_write_bytes(path, encoded)
        persisted = read_settings_document(path)
        if persisted != validated:
            raise SettingsPersistenceError("persisted settings failed verification")
        return persisted
    except Exception:
        if previous is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            atomic_write_bytes(path, previous)
        raise


class SettingsService:
    """Thread-safe access to validated non-secret settings."""

    def __init__(
        self,
        data_dir: PathLike | None = None,
        *,
        path: PathLike | None = None,
        secret_store: SecretStore | None = None,
        auto_migrate: bool = True,
        legacy_data_dir: PathLike | None = None,
        legacy_settings_path: PathLike | None = None,
    ) -> None:
        environment_injected = DATA_DIR_ENV in os.environ
        explicitly_injected = (
            data_dir is not None
            or path is not None
            or secret_store is not None
            or environment_injected
        )
        if data_dir is None and path is None and secret_store is not None:
            self.data_dir = secret_store.path.parent
            self.data_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.data_dir = get_app_data_dir(data_dir)
        self.path = Path(path).expanduser() if path is not None else self.data_dir / "settings.json"
        if not self.path.is_absolute():
            raise ValueError("settings path must be absolute")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_store = secret_store or SecretStore(self.data_dir)
        self._lock = threading.RLock()

        selected_legacy_path: Path | None = None
        if legacy_settings_path is not None:
            selected_legacy_path = Path(legacy_settings_path).expanduser()
            if not selected_legacy_path.is_absolute():
                raise ValueError("legacy_settings_path must be absolute")
        elif legacy_data_dir is not None:
            selected_legacy_path = (
                get_legacy_app_data_dir(legacy_data_dir, create=False) / "settings.json"
            )
        elif not explicitly_injected:
            selected_legacy_path = get_legacy_app_data_dir(create=False) / "settings.json"

        if auto_migrate and selected_legacy_path is not None:
            # Local import avoids a module cycle: the migrator uses the pure
            # validation/write functions above, not this service class.
            from banana_prism.services.migration_service import (
                MigrationService,
                MigrationStateError,
            )

            migration_result = MigrationService(
                data_dir=self.data_dir,
                settings_path=self.path,
                legacy_settings_path=selected_legacy_path,
                secret_store=self.secret_store,
            ).migrate_if_needed()
            if not migration_result.cleanup_complete:
                raise MigrationStateError(
                    "legacy settings changed during migration; cleanup was not completed"
                )

        if self.path.exists():
            self._data = read_settings_document(self.path)
        else:
            candidate = default_settings_document()
            self._verify_secret_binding(candidate)
            self._data = write_settings_document(self.path, candidate)
        self.verify_secret_binding()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def reload(self) -> None:
        with self._lock:
            candidate = read_settings_document(self.path)
            self._verify_secret_binding(candidate)
            self._data = candidate

    def save(self) -> None:
        """Compatibility flush; setters already commit synchronously."""

        with self._lock:
            self._commit(self._data)

    def _commit(self, candidate: Mapping[str, Any]) -> None:
        self._verify_secret_binding(candidate)
        previous = copy.deepcopy(self._data)
        persisted = write_settings_document(self.path, candidate)
        try:
            self._verify_secret_binding(persisted)
        except Exception:
            write_settings_document(self.path, previous)
            raise
        self._data = persisted

    def _verify_secret_binding(self, document: Mapping[str, Any]) -> None:
        metadata = document.get("secret_store")
        if metadata is None:
            if self.secret_store.exists:
                try:
                    unreferenced = self.secret_store.load()
                except Exception as exc:
                    raise SettingsSecretBindingError(
                        "unreferenced secret store is unavailable"
                    ) from exc
                if (
                    unreferenced.get("credentials")
                    or unreferenced.get("console_auth") is not None
                    or unreferenced.get("migration_backup") is not None
                ):
                    raise SettingsSecretBindingError(
                        "secret store exists without matching settings metadata"
                    )
            return
        if not self.secret_store.exists:
            raise SettingsSecretBindingError("referenced secret store is missing")
        try:
            secrets = self.secret_store.load()
        except Exception as exc:
            raise SettingsSecretBindingError("referenced secret store is unavailable") from exc
        if secrets["generation"] != metadata["generation"]:
            raise SettingsSecretBindingError("secret store generation does not match settings")
        configured = bool(document["console_password_configured"])
        if (secrets.get("console_auth") is not None) != configured:
            raise SettingsSecretBindingError("console password state does not match secret store")
        credentials = secrets["credentials"]
        for preset in document["api_presets"]:
            reference = preset["credential_ref"]
            if not reference:
                continue
            stored = credentials.get(reference)
            if stored is None or stored.get("provider") != preset["provider"]:
                raise SettingsSecretBindingError(
                    "preset credential reference does not match secret store"
                )

    def verify_secret_binding(self) -> None:
        """Fail closed if settings and the encrypted store are different generations."""

        with self._lock:
            self._verify_secret_binding(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        if key.casefold() in _FORBIDDEN_SECRET_FIELDS:
            raise SettingsValidationError("secret values cannot be stored in settings")
        with self._lock:
            candidate = copy.deepcopy(self._data)
            candidate[key] = copy.deepcopy(value)
            self._commit(candidate)

    def set_many(self, values: Mapping[str, Any]) -> None:
        """Validate and atomically commit a group of non-secret preferences."""

        for key in values:
            if not isinstance(key, str):
                raise SettingsValidationError("setting names must be text")
            if key.casefold() in _FORBIDDEN_SECRET_FIELDS:
                raise SettingsValidationError("secret values cannot be stored in settings")
        with self._lock:
            candidate = copy.deepcopy(self._data)
            candidate.update(copy.deepcopy(dict(values)))
            self._commit(candidate)

    @property
    def last_model_index(self) -> int:
        return cast(int, self.get("last_model_index"))

    @last_model_index.setter
    def last_model_index(self, value: int) -> None:
        self.set("last_model_index", value)

    @property
    def last_size_index(self) -> int:
        return cast(int, self.get("last_size_index"))

    @last_size_index.setter
    def last_size_index(self, value: int) -> None:
        self.set("last_size_index", value)

    @property
    def last_ratio(self) -> str:
        return cast(str, self.get("last_ratio"))

    @last_ratio.setter
    def last_ratio(self, value: str) -> None:
        self.set("last_ratio", value)

    @property
    def last_output_format(self) -> str:
        return cast(str, self.get("last_output_format", "png"))

    @last_output_format.setter
    def last_output_format(self, value: str) -> None:
        self.set("last_output_format", value)

    @property
    def last_output_dpi(self) -> int:
        return cast(int, self.get("last_output_dpi", 300))

    @last_output_dpi.setter
    def last_output_dpi(self, value: int) -> None:
        self.set("last_output_dpi", value)

    @property
    def last_annotation_color(self) -> str:
        return cast(str, self.get("last_annotation_color", "red"))

    @last_annotation_color.setter
    def last_annotation_color(self, value: str) -> None:
        self.set("last_annotation_color", value)

    @property
    def save_dir(self) -> str:
        return cast(str, self.get("save_dir"))

    @save_dir.setter
    def save_dir(self, value: str) -> None:
        self.set("save_dir", value)

    @property
    def prompt_save_length(self) -> int:
        return cast(int, self.get("prompt_save_length"))

    @prompt_save_length.setter
    def prompt_save_length(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsValidationError("prompt_save_length must be an integer")
        self.set("prompt_save_length", max(10, min(100, value)))

    @property
    def api_presets(self) -> list[dict[str, str]]:
        return cast(list[dict[str, str]], self.get("api_presets"))

    @property
    def presets(self) -> tuple[ApiPreset, ...]:
        return tuple(
            ApiPreset(
                preset_id=item["id"],
                name=item["name"],
                provider=item["provider"],
                credential_ref=item["credential_ref"],
            )
            for item in self.api_presets
        )

    def list_presets(self) -> tuple[ApiPreset, ...]:
        return self.presets

    @property
    def active_preset_id(self) -> str:
        return cast(str, self.get("active_preset_id"))

    @active_preset_id.setter
    def active_preset_id(self, value: str) -> None:
        self.set("active_preset_id", value)

    @property
    def active_preset(self) -> dict[str, str] | None:
        active_id = self.active_preset_id
        return next((item for item in self.api_presets if item["id"] == active_id), None)

    @property
    def console_password_configured(self) -> bool:
        return cast(bool, self.get("console_password_configured"))

    def _record_secret_generation(
        self,
        generation: str,
        *,
        password_configured: bool | None = None,
    ) -> None:
        with self._lock:
            candidate = copy.deepcopy(self._data)
            candidate["secret_store"] = {
                "schema_version": SECRET_SCHEMA_VERSION,
                "generation": generation,
            }
            if password_configured is not None:
                candidate["console_password_configured"] = password_configured
            migration = candidate.get("migration")
            if isinstance(migration, dict):
                # Once users alter the store after migration, keep the migration
                # record linked to the current verified store generation.
                migration["secret_generation"] = generation
            self._commit(candidate)

    def record_secret_generation(
        self,
        generation: str,
        *,
        password_configured: bool | None = None,
    ) -> None:
        """Synchronize non-secret metadata after a verified secret-store commit."""

        self._record_secret_generation(
            generation,
            password_configured=password_configured,
        )

    def upsert_api_preset(
        self,
        *,
        preset_id: str,
        name: str,
        provider: str,
        api_key: str | None = None,
        make_active: bool = False,
    ) -> None:
        with self._lock:
            candidate = copy.deepcopy(self._data)
            presets = candidate["api_presets"]
            existing = next((item for item in presets if item["id"] == preset_id), None)
            if (
                not isinstance(preset_id, str)
                or not preset_id
                or len(preset_id) > 128
                or any(ord(character) < 32 for character in preset_id)
            ):
                raise SettingsValidationError("api preset id is invalid")
            if provider not in API_PROVIDERS:
                raise SettingsValidationError("api preset provider is unsupported")
            if not isinstance(name, str) or len(name) > 256:
                raise SettingsValidationError("api preset name is invalid")
            if api_key is not None and (
                not isinstance(api_key, str) or not api_key or len(api_key) > 4096
            ):
                raise SettingsValidationError("api_key length is invalid")
            if (
                existing is not None
                and api_key is None
                and existing["provider"] != provider
                and existing["credential_ref"]
            ):
                raise SettingsValidationError(
                    "changing provider requires a new API key"
                )
            reference = (
                existing["credential_ref"]
                if existing is not None and api_key is None
                else (f"api:{preset_id}" if api_key else "")
            )
            replacement = {
                "id": preset_id,
                "name": name.strip() or "未命名配置",
                "provider": provider,
                "credential_ref": reference,
            }
            if existing is None:
                presets.append(replacement)
            else:
                presets[presets.index(existing)] = replacement
            if make_active or not candidate["active_preset_id"]:
                candidate["active_preset_id"] = preset_id
            previous_secret = (
                self.secret_store.load()
                if api_key is not None and self.secret_store.exists
                else None
            )
            generation: str | None = None
            if api_key is not None:
                generation = self.secret_store.set_credential(
                    f"api:{preset_id}", provider, api_key
                )
                candidate["secret_store"] = {
                    "schema_version": SECRET_SCHEMA_VERSION,
                    "generation": generation,
                }
                migration = candidate.get("migration")
                if isinstance(migration, dict):
                    migration["secret_generation"] = generation
            try:
                self._commit(candidate)
            except Exception as exc:
                if generation is not None:
                    try:
                        if previous_secret is None:
                            self.secret_store.path.unlink(missing_ok=True)
                        else:
                            self.secret_store.save(previous_secret)
                    except Exception as rollback_exc:
                        raise SettingsPersistenceError(
                            "preset update failed and secret rollback also failed"
                        ) from rollback_exc
                raise

    def remove_api_preset(self, preset_id: str) -> None:
        with self._lock:
            candidate = copy.deepcopy(self._data)
            removed = next(
                (item for item in candidate["api_presets"] if item["id"] == preset_id),
                None,
            )
            if removed is None:
                return
            candidate["api_presets"].remove(removed)
            if candidate["active_preset_id"] == preset_id:
                candidate["active_preset_id"] = (
                    candidate["api_presets"][0]["id"] if candidate["api_presets"] else ""
                )
            previous_secret = (
                self.secret_store.load()
                if removed["credential_ref"] and self.secret_store.exists
                else None
            )
            generation = (
                self.secret_store.delete_credential(removed["credential_ref"])
                if removed["credential_ref"]
                else None
            )
            if generation is not None:
                candidate["secret_store"] = {
                    "schema_version": SECRET_SCHEMA_VERSION,
                    "generation": generation,
                }
                migration = candidate.get("migration")
                if isinstance(migration, dict):
                    migration["secret_generation"] = generation
            try:
                self._commit(candidate)
            except Exception as exc:
                if generation is not None and previous_secret is not None:
                    try:
                        self.secret_store.save(previous_secret)
                    except Exception as rollback_exc:
                        raise SettingsPersistenceError(
                            "preset deletion failed and secret rollback also failed"
                        ) from rollback_exc
                raise

    def upsert_preset(
        self,
        name: str,
        provider: str,
        api_key: str | None = None,
        preset_id: str | None = None,
        *,
        make_active: bool = False,
    ) -> dict[str, str]:
        """UI-facing preset mutation; plaintext is sent only to SecretStore."""

        selected_id = preset_id or f"preset_{uuid.uuid4().hex[:8]}"
        self.upsert_api_preset(
            preset_id=selected_id,
            name=name,
            provider=provider,
            api_key=api_key,
            make_active=make_active,
        )
        return next(item for item in self.api_presets if item["id"] == selected_id)

    def delete_preset(self, preset_id: str) -> None:
        self.remove_api_preset(preset_id)

    def set_active_preset(self, preset_id: str) -> None:
        self.active_preset_id = preset_id

    def get_api_key(self, preset_id: str | None = None) -> str | None:
        target = preset_id or self.active_preset_id
        preset = next((item for item in self.api_presets if item["id"] == target), None)
        if preset is None or not preset["credential_ref"]:
            return None
        return self.secret_store.get_credential(preset["credential_ref"])

    def has_credential(self, reference_or_preset_id: str) -> bool:
        if reference_or_preset_id.startswith("api:"):
            return self.secret_store.get_credential(reference_or_preset_id) is not None
        return self.get_api_key(reference_or_preset_id) is not None

    def has_api_key(self, reference_or_preset_id: str) -> bool:
        return self.has_credential(reference_or_preset_id)

    @property
    def api_key(self) -> str:
        """Compatibility getter; the value is never persisted in settings.json."""

        return self.get_api_key() or ""

    @api_key.setter
    def api_key(self, value: str) -> None:
        preset = self.active_preset
        if preset is None:
            self.upsert_api_preset(
                preset_id="preset_0",
                name="默认 (OpenRouter)",
                provider="openrouter",
                api_key=value,
                make_active=True,
            )
        else:
            self.upsert_api_preset(
                preset_id=preset["id"],
                name=preset["name"],
                provider=preset["provider"],
                api_key=value,
            )

    def get_today_generation_count(self, *, on_date: date | None = None) -> int:
        current = on_date or date.today()
        today = cast(dict[str, Any], self.get("today_gen"))
        return cast(int, today["count"]) if today["date"] == current.isoformat() else 0

    @property
    def today_generation_count(self) -> int:
        return self.get_today_generation_count()

    @property
    def today_count(self) -> int:
        return self.get_today_generation_count()

    def get_today_count(self) -> int:
        return self.get_today_generation_count()

    def increment_generation_count(self, *, on_date: date | None = None) -> int:
        current = (on_date or date.today()).isoformat()
        with self._lock:
            candidate = copy.deepcopy(self._data)
            today = candidate["today_gen"]
            if today["date"] != current:
                today = {"date": current, "count": 0}
                candidate["today_gen"] = today
            today["count"] += 1
            self._commit(candidate)
            return cast(int, today["count"])

    def increment_today_count(self) -> int:
        return self.increment_generation_count()

    def has_password(self) -> bool:
        from banana_prism.services.auth_service import AuthService

        return AuthService(self.secret_store, self).has_password()

    def verify_password(self, password: str) -> bool:
        from banana_prism.services.auth_service import AuthService

        return AuthService(self.secret_store, self).verify_password(password)

    def verify_console_password(self, password: str) -> bool:
        return self.verify_password(password)

    def unlock(self, password: str) -> bool:
        return self.verify_password(password)

    def set_password(self, password: str) -> None:
        from banana_prism.services.auth_service import AuthService

        AuthService(self.secret_store, self).set_password(password)

    def change_password(self, current_password: str, new_password: str) -> bool:
        if self.has_password() and not self.verify_password(current_password):
            return False
        self.set_password(new_password)
        return True


__all__ = [
    "MAX_SETTINGS_BYTES",
    "SettingsError",
    "SettingsPersistenceError",
    "SettingsSecretBindingError",
    "SettingsService",
    "SettingsValidationError",
    "default_settings_document",
    "read_settings_document",
    "validate_settings_document",
    "write_settings_document",
]
