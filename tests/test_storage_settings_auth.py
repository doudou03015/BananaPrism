from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

import pytest

from banana_prism.constants import DEFAULT_MODEL_INDEX, MODELS
from banana_prism.services.auth_service import (
    LEGACY_PBKDF2_ITERATIONS,
    PBKDF2_ITERATIONS,
    AuthService,
    legacy_hash_to_record,
    make_password_record,
    verify_legacy_hash,
)
from banana_prism.services.log_service import LogService
from banana_prism.services.secret_store import (
    SecretProtectionError,
    SecretStore,
    SecretStoreCorrupt,
)
from banana_prism.services.settings_service import (
    SettingsService,
    SettingsSecretBindingError,
    SettingsValidationError,
)


class AuthenticatedTestBackend:
    """Injectable test backend with confidentiality and tamper detection."""

    def __init__(self, key: bytes = b"storage-test-key") -> None:
        self.key = hashlib.sha256(key).digest()

    def protect(self, plaintext: bytes, *, description: str, entropy: bytes) -> bytes:
        stream = hashlib.sha256(self.key + entropy + description.encode()).digest()
        encrypted = bytes(byte ^ stream[index % len(stream)] for index, byte in enumerate(plaintext))
        return hmac.digest(self.key, entropy + encrypted, "sha256") + encrypted

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        if len(ciphertext) < 32:
            raise SecretStoreCorrupt("test ciphertext is truncated")
        mac, encrypted = ciphertext[:32], ciphertext[32:]
        if not hmac.compare_digest(mac, hmac.digest(self.key, entropy + encrypted, "sha256")):
            raise SecretStoreCorrupt("test ciphertext authentication failed")
        stream = hashlib.sha256(
            self.key + entropy + b"BananaPrism credential store v1"
        ).digest()
        return bytes(byte ^ stream[index % len(stream)] for index, byte in enumerate(encrypted))


def make_services(tmp_path: Path) -> tuple[SecretStore, SettingsService]:
    store = SecretStore(tmp_path, backend=AuthenticatedTestBackend())
    settings = SettingsService(
        tmp_path,
        secret_store=store,
        auto_migrate=False,
    )
    return store, settings


def test_secret_store_is_encrypted_validated_and_tamper_evident(tmp_path: Path) -> None:
    store = SecretStore(tmp_path, backend=AuthenticatedTestBackend())
    generation = store.set_credential("api:preset_0", "openrouter", "opaque-value-123")

    raw = store.path.read_bytes()
    assert b"opaque-value-123" not in raw
    assert store.load()["generation"] == generation
    assert store.get_credential("api:preset_0") == "opaque-value-123"

    raw = bytearray(raw)
    raw[-1] ^= 1
    store.path.write_bytes(raw)
    with pytest.raises(SecretStoreCorrupt):
        store.load()


def test_settings_reject_secret_fields_and_preserve_last_good_file(tmp_path: Path) -> None:
    _, settings = make_services(tmp_path)
    before = settings.path.read_bytes()

    with pytest.raises(SettingsValidationError):
        settings.set("api_key", "must-not-be-written")
    with pytest.raises(SettingsValidationError):
        settings.active_preset_id = "missing"

    assert settings.path.read_bytes() == before
    assert b"must-not-be-written" not in before


def test_ui_preset_metadata_is_nonsecret_and_key_resolution_is_explicit(tmp_path: Path) -> None:
    store, settings = make_services(tmp_path)
    preset = settings.upsert_preset(
        "Primary",
        "openrouter",
        "opaque-provider-key",
        make_active=True,
    )

    assert set(preset) == {"id", "name", "provider", "credential_ref"}
    assert settings.active_preset == preset
    assert settings.get_api_key(preset["id"]) == "opaque-provider-key"
    assert b"opaque-provider-key" not in settings.path.read_bytes()
    assert b"opaque-provider-key" not in store.path.read_bytes()

    logs = LogService(logs_dir=tmp_path / "logs")
    logs.error("provider echoed opaque-provider-key in a 401 response")
    log_text = next((tmp_path / "logs").glob("*.log")).read_text(encoding="utf-8")
    assert "opaque-provider-key" not in log_text

    settings.delete_preset(preset["id"])
    assert settings.api_presets == []
    assert settings.active_preset is None
    assert settings.get_api_key(preset["id"]) is None


def test_preset_provider_change_requires_new_key_and_failed_settings_commit_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, settings = make_services(tmp_path)
    settings.upsert_api_preset(
        preset_id="preset_0",
        name="Primary",
        provider="openrouter",
        api_key="original-key",
        make_active=True,
    )
    with pytest.raises(SettingsValidationError):
        settings.upsert_api_preset(
            preset_id="preset_0",
            name="Primary",
            provider="aihubmix",
            api_key=None,
        )
    assert settings.get_api_key("preset_0") == "original-key"

    def fail_commit(candidate: object) -> None:
        raise OSError("injected settings failure")

    monkeypatch.setattr(settings, "_commit", fail_commit)
    with pytest.raises(OSError):
        settings.upsert_api_preset(
            preset_id="preset_0",
            name="Primary",
            provider="openrouter",
            api_key="replacement-key",
        )
    assert settings.get_api_key("preset_0") == "original-key"


def test_settings_defaults_validation_and_today_rollover(tmp_path: Path) -> None:
    _, settings = make_services(tmp_path)
    assert settings.last_model_index == DEFAULT_MODEL_INDEX
    assert settings.last_size_index == 0
    assert settings.last_ratio == "1:1"
    assert settings.last_output_format == "png"
    assert settings.last_output_dpi == 300
    assert settings.last_annotation_color == "red"
    assert settings.prompt_save_length == 30
    assert settings.today_count == 0

    settings.prompt_save_length = 500
    assert settings.prompt_save_length == 100
    assert settings.increment_generation_count(on_date=__import__("datetime").date(2026, 8, 26)) == 1
    assert settings.get_today_generation_count(on_date=__import__("datetime").date(2026, 8, 27)) == 0

    settings.set_many(
        {
            "last_output_format": "jpeg",
            "last_output_dpi": 150,
            "last_annotation_color": "green",
        }
    )
    settings.reload()
    assert settings.last_output_format == "jpeg"
    assert settings.last_output_dpi == 150
    assert settings.last_annotation_color == "green"

    document = json.loads(settings.path.read_text(encoding="utf-8"))
    document.update(
        {
            "last_output_format": "gif",
            "last_output_dpi": -1,
            "last_annotation_color": "transparent",
        }
    )
    settings.path.write_text(json.dumps(document), encoding="utf-8")
    settings.reload()
    assert settings.last_output_format == "png"
    assert settings.last_output_dpi == 300
    assert settings.last_annotation_color == "red"

    document = json.loads(settings.path.read_text(encoding="utf-8"))
    document["last_ratio"] = "99:1"
    settings.path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SettingsValidationError):
        settings.reload()


def test_grouped_settings_update_is_atomic_on_validation_failure(tmp_path: Path) -> None:
    _, settings = make_services(tmp_path)
    before = settings.path.read_bytes()

    with pytest.raises(SettingsValidationError):
        settings.set_many({"save_dir": str(tmp_path / "images"), "prompt_save_length": 999})

    assert settings.path.read_bytes() == before
    assert settings.save_dir != str(tmp_path / "images")


@pytest.mark.parametrize(
    ("model_index", "model_id"),
    [
        (0, "google/gemini-3.1-flash-image"),
        (1, "google/gemini-2.5-flash-image"),
        (2, "google/gemini-3-pro-image"),
    ],
)
def test_existing_settings_keep_model_selection_when_optional_preferences_are_filled(
    tmp_path: Path, model_index: int, model_id: str
) -> None:
    _, settings = make_services(tmp_path)
    document = settings.as_dict()
    document["last_model_index"] = model_index
    for optional_preference in (
        "last_output_format", "last_output_dpi", "last_annotation_color"
    ):
        document.pop(optional_preference)
    settings.path.write_text(json.dumps(document), encoding="utf-8")

    _, reopened = make_services(tmp_path)
    assert reopened.last_model_index == model_index
    assert MODELS[reopened.last_model_index].model_id == model_id
    assert reopened.last_output_format == "png"


def test_nano_banana_21_selection_round_trips_through_settings(tmp_path: Path) -> None:
    _, settings = make_services(tmp_path)
    settings.last_model_index = 0
    settings.last_model_index = DEFAULT_MODEL_INDEX

    _, reopened = make_services(tmp_path)
    assert MODELS[reopened.last_model_index].model_id == "google/gemini-nano-banana-2.1"
    assert json.loads(reopened.path.read_text(encoding="utf-8"))["last_model_index"] == 3


def test_legacy_password_verifies_then_upgrades_to_600k(tmp_path: Path) -> None:
    store, settings = make_services(tmp_path)
    password = "banana-secret"
    salt = bytes(range(32))
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        LEGACY_PBKDF2_ITERATIONS,
    )
    encoded = f"{salt.hex()}:{digest.hex()}"
    assert verify_legacy_hash(password, encoded)
    assert not verify_legacy_hash("wrong-value", encoded)

    generation = store.set_console_auth(legacy_hash_to_record(encoded))
    settings.record_secret_generation(generation, password_configured=True)
    auth = AuthService(store, settings)

    assert not auth.verify_password("wrong-value")
    assert store.get_console_auth()["iterations"] == LEGACY_PBKDF2_ITERATIONS
    assert auth.verify_password(password)
    upgraded = store.get_console_auth()
    assert upgraded is not None
    assert upgraded["iterations"] == PBKDF2_ITERATIONS
    assert settings.console_password_configured is True
    assert b"banana-secret" not in store.path.read_bytes()


def test_failed_secret_backend_never_falls_back_to_plaintext(tmp_path: Path) -> None:
    class FailingBackend:
        def protect(self, plaintext: bytes, *, description: str, entropy: bytes) -> bytes:
            raise SecretProtectionError("expected failure")

        def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
            raise SecretProtectionError("expected failure")

    store = SecretStore(tmp_path, backend=FailingBackend())
    with pytest.raises(SecretProtectionError):
        store.set_credential("api:preset_0", "openrouter", "never-plaintext")
    assert not store.path.exists()
    assert not any(tmp_path.glob("*.json"))


def test_settings_generation_binding_rejects_secret_rollback_and_missing_store(
    tmp_path: Path,
) -> None:
    store, settings = make_services(tmp_path)
    generation_a = store.set_console_auth(
        make_password_record("password-a", iterations=1, salt=b"a" * 32)
    )
    settings.record_secret_generation(generation_a, password_configured=True)
    ciphertext_a = store.path.read_bytes()

    generation_b = store.set_console_auth(
        make_password_record("password-b", iterations=1, salt=b"b" * 32)
    )
    settings.record_secret_generation(generation_b, password_configured=True)
    store.path.write_bytes(ciphertext_a)

    with pytest.raises(SettingsSecretBindingError):
        settings.verify_secret_binding()
    with pytest.raises(SettingsSecretBindingError):
        AuthService(store, settings).verify_password("password-a")
    with pytest.raises(SettingsSecretBindingError):
        SettingsService(tmp_path, secret_store=store, auto_migrate=False)

    store.path.unlink()
    with pytest.raises(SettingsSecretBindingError):
        settings.verify_secret_binding()


def test_sensitive_store_without_settings_metadata_fails_closed(tmp_path: Path) -> None:
    store = SecretStore(tmp_path, backend=AuthenticatedTestBackend())
    store.set_console_auth(
        make_password_record("password-a", iterations=1, salt=b"a" * 32)
    )

    with pytest.raises(SettingsSecretBindingError):
        SettingsService(tmp_path, secret_store=store, auto_migrate=False)
    assert not (tmp_path / "settings.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration")
def test_windows_dpapi_current_user_round_trip_uses_injected_temp_dir(
    tmp_path: Path,
) -> None:
    store = SecretStore(tmp_path)
    store.set_credential("api:windows", "openrouter", "dpapi-round-trip-value")

    assert store.get_credential("api:windows") == "dpapi-round-trip-value"
    assert b"dpapi-round-trip-value" not in store.path.read_bytes()
