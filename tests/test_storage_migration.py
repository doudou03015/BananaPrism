from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import hmac
import json
from pathlib import Path

import pytest

import banana_prism.services.migration_service as migration_module
import banana_prism.services.settings_service as settings_module
from banana_prism.services.migration_service import (
    LegacySettingsValidationError,
    MigrationError,
    MigrationService,
    MigrationStateError,
)
from banana_prism.services.secret_store import (
    SecretProtectionError,
    SecretStore,
    SecretStoreCorrupt,
)
from banana_prism.services.settings_service import SettingsService
from banana_prism.utils.paths import DATA_DIR_ENV


class AuthenticatedTestBackend:
    def __init__(self) -> None:
        self.key = hashlib.sha256(b"migration-test-key").digest()

    def protect(self, plaintext: bytes, *, description: str, entropy: bytes) -> bytes:
        stream = hashlib.sha256(self.key + entropy + description.encode()).digest()
        encrypted = bytes(byte ^ stream[index % len(stream)] for index, byte in enumerate(plaintext))
        return hmac.digest(self.key, entropy + encrypted, "sha256") + encrypted

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        if len(ciphertext) < 32:
            raise SecretStoreCorrupt("test ciphertext truncated")
        mac, encrypted = ciphertext[:32], ciphertext[32:]
        expected = hmac.digest(self.key, entropy + encrypted, "sha256")
        if not hmac.compare_digest(mac, expected):
            raise SecretStoreCorrupt("test ciphertext invalid")
        stream = hashlib.sha256(
            self.key + entropy + b"BananaPrism credential store v1"
        ).digest()
        return bytes(byte ^ stream[index % len(stream)] for index, byte in enumerate(encrypted))


def legacy_document() -> dict[str, object]:
    salt = bytes(range(32))
    digest = hashlib.pbkdf2_hmac(
        "sha256", b"legacy-pass", salt, 260_000
    )
    return {
        "api_key": "stale-root-key-must-not-win",
        "console_password_hash": f"{salt.hex()}:{digest.hex()}",
        "last_model_index": 1,
        "last_size_index": 2,
        "last_ratio": "4:3",
        "prompt_save_length": 35,
        "today_gen": {"date": "2026-05-29", "count": 6},
        "api_presets": [
            {
                "id": "preset_0",
                "name": "Primary",
                "provider": "openrouter",
                "api_key": "primary-key",
            },
            {
                "id": "preset_1",
                "name": "Backup",
                "provider": "aihubmix",
                "api_key": "backup-key",
            },
        ],
        "active_preset_id": "preset_0",
    }


def write_legacy(directory: Path) -> tuple[Path, bytes]:
    directory.mkdir(parents=True)
    path = directory / "settings.json"
    raw = json.dumps(legacy_document(), ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(raw)
    return path, raw


def test_migration_encrypts_backup_verifies_then_scrubs_and_is_idempotent(
    tmp_path: Path,
) -> None:
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    legacy_path, original_raw = write_legacy(legacy_dir)
    store = SecretStore(new_dir, backend=AuthenticatedTestBackend())

    settings = SettingsService(
        new_dir,
        secret_store=store,
        legacy_data_dir=legacy_dir,
    )

    assert settings.get_api_key("preset_0") == "primary-key"
    assert settings.get_api_key("preset_1") == "backup-key"
    assert settings.api_key == "primary-key"
    assert settings.last_model_index == 1
    assert b"primary-key" not in settings.path.read_bytes()
    assert b"primary-key" not in store.path.read_bytes()
    assert b"stale-root-key-must-not-win" not in store.path.read_bytes()
    assert settings.get("migration")["status"] == "complete"

    secret_document = store.load()
    backup = secret_document["migration_backup"]
    assert base64.b64decode(backup["payload_b64"], validate=True) == original_raw
    assert secret_document["credentials"]["api:preset_0"]["value"] == "primary-key"
    assert "stale-root-key-must-not-win" not in {
        item["value"] for item in secret_document["credentials"].values()
    }

    scrubbed = json.loads(legacy_path.read_text(encoding="utf-8"))
    assert "api_key" not in scrubbed
    assert "console_password_hash" not in scrubbed
    assert all("api_key" not in item for item in scrubbed["api_presets"])
    assert scrubbed["_bananaprism_migration"]["status"] == "complete"

    before = (settings.path.read_bytes(), store.path.read_bytes(), legacy_path.read_bytes())
    result = MigrationService(
        new_dir,
        legacy_data_dir=legacy_dir,
        secret_store=store,
    ).migrate_if_needed()
    after = (settings.path.read_bytes(), store.path.read_bytes(), legacy_path.read_bytes())
    assert result.status == "complete"
    assert result.cleanup_complete
    assert before == after


def test_migration_resumes_after_secret_commit_without_scrubbing_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    legacy_path, original_raw = write_legacy(legacy_dir)
    store = SecretStore(new_dir, backend=AuthenticatedTestBackend())
    migrator = MigrationService(
        new_dir,
        legacy_data_dir=legacy_dir,
        secret_store=store,
    )
    original_writer = migration_module.write_settings_document

    def fail_settings_commit(path: Path, document: object) -> None:
        raise OSError("injected settings commit failure")

    monkeypatch.setattr(migration_module, "write_settings_document", fail_settings_commit)
    with pytest.raises(OSError):
        migrator.migrate_if_needed()

    assert store.path.exists()  # encrypted backup committed and can be resumed
    assert not (new_dir / "settings.json").exists()
    assert legacy_path.read_bytes() == original_raw  # never scrubbed before full verify

    monkeypatch.setattr(migration_module, "write_settings_document", original_writer)
    result = migrator.migrate_if_needed()
    assert result.status == "migrated"
    assert result.cleanup_complete
    assert "api_key" not in json.loads(legacy_path.read_text(encoding="utf-8"))


def test_migration_protection_failure_leaves_legacy_byte_for_byte(
    tmp_path: Path,
) -> None:
    class FailingBackend:
        def protect(self, plaintext: bytes, *, description: str, entropy: bytes) -> bytes:
            raise SecretProtectionError("injected protection failure")

        def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
            raise SecretProtectionError("injected protection failure")

    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    legacy_path, original_raw = write_legacy(legacy_dir)
    store = SecretStore(new_dir, backend=FailingBackend())

    with pytest.raises(SecretProtectionError):
        MigrationService(
            new_dir,
            legacy_data_dir=legacy_dir,
            secret_store=store,
        ).migrate_if_needed()

    assert legacy_path.read_bytes() == original_raw
    assert not store.path.exists()
    assert not (new_dir / "settings.json").exists()


def test_injected_data_dir_never_probes_real_legacy_appdata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    injected = tmp_path / "portable"
    appdata_trap = tmp_path / "real-appdata-trap"
    trap_legacy = appdata_trap / "NanaBananaStudio"
    trap_path, trap_raw = write_legacy(trap_legacy)
    monkeypatch.setenv(DATA_DIR_ENV, str(injected))
    monkeypatch.setenv("APPDATA", str(appdata_trap))
    store = SecretStore(injected, backend=AuthenticatedTestBackend())

    migrator = MigrationService(secret_store=store)
    assert migrator.legacy_settings_path is None
    result = migrator.migrate_if_needed()

    assert result.status == "legacy_probe_disabled"
    assert trap_path.read_bytes() == trap_raw
    assert not (injected / "settings.json").exists()
    assert not store.path.exists()


def test_injected_secret_store_alone_never_probes_appdata_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    portable = tmp_path / "secret-store-only"
    appdata_trap = tmp_path / "appdata-trap"
    trap_path, trap_raw = write_legacy(appdata_trap / "NanaBananaStudio")
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)
    monkeypatch.setenv("APPDATA", str(appdata_trap))

    def forbidden_legacy_probe(*_args, **_kwargs):
        raise AssertionError("real legacy resolver must not be called")

    monkeypatch.setattr(
        migration_module,
        "get_legacy_app_data_dir",
        forbidden_legacy_probe,
    )
    monkeypatch.setattr(
        settings_module,
        "get_legacy_app_data_dir",
        forbidden_legacy_probe,
    )
    store = SecretStore(portable, backend=AuthenticatedTestBackend())

    migrator = MigrationService(secret_store=store)
    assert migrator.data_dir == portable
    assert migrator.legacy_settings_path is None
    assert migrator.migrate_if_needed().status == "legacy_probe_disabled"

    settings = SettingsService(secret_store=store)
    assert settings.data_dir == portable
    assert settings.path == portable / "settings.json"
    assert settings.api_presets == []
    assert trap_path.read_bytes() == trap_raw
    assert not store.path.exists()


def test_legacy_fingerprint_conflict_makes_settings_fail_closed_without_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    legacy_path, _original_raw = write_legacy(legacy_dir)
    changed = legacy_document()
    changed["external_update"] = "must-survive"
    changed_raw = json.dumps(changed, ensure_ascii=False, indent=2).encode("utf-8")
    store = SecretStore(new_dir, backend=AuthenticatedTestBackend())
    original_scrub = MigrationService._scrub_legacy
    injected = False

    def change_before_scrub(self: MigrationService, fingerprint: str) -> str:
        nonlocal injected
        if not injected:
            legacy_path.write_bytes(changed_raw)
            injected = True
        return original_scrub(self, fingerprint)

    monkeypatch.setattr(MigrationService, "_scrub_legacy", change_before_scrub)
    with pytest.raises(MigrationStateError, match="cleanup was not completed"):
        SettingsService(
            new_dir,
            secret_store=store,
            legacy_data_dir=legacy_dir,
        )

    assert injected
    assert legacy_path.read_bytes() == changed_raw
    assert b"primary-key" in changed_raw
    pending = json.loads((new_dir / "settings.json").read_text(encoding="utf-8"))
    assert pending["migration"]["status"] == "cleanup_pending"
    assert store.exists


def test_complete_marker_failure_restores_original_legacy_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    legacy_path, original_raw = write_legacy(legacy_dir)
    store = SecretStore(new_dir, backend=AuthenticatedTestBackend())
    migrator = MigrationService(
        new_dir,
        legacy_data_dir=legacy_dir,
        secret_store=store,
    )
    original_writer = migration_module.write_settings_document

    def fail_complete_marker(path: Path, document: object):
        if (
            isinstance(document, dict)
            and isinstance(document.get("migration"), dict)
            and document["migration"].get("status") == "complete"
        ):
            raise OSError("injected complete-marker failure")
        return original_writer(path, document)

    monkeypatch.setattr(
        migration_module,
        "write_settings_document",
        fail_complete_marker,
    )
    with pytest.raises(OSError, match="complete-marker failure"):
        migrator.migrate_if_needed()

    assert legacy_path.read_bytes() == original_raw
    pending = json.loads((new_dir / "settings.json").read_text(encoding="utf-8"))
    assert pending["migration"]["status"] == "cleanup_pending"

    monkeypatch.setattr(migration_module, "write_settings_document", original_writer)
    result = migrator.migrate_if_needed()
    assert result.status == "complete"
    assert result.cleanup_complete


def test_scrub_verification_failure_rolls_back_original_legacy_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    legacy_path, original_raw = write_legacy(legacy_dir)
    store = SecretStore(new_dir, backend=AuthenticatedTestBackend())
    migrator = MigrationService(
        new_dir,
        legacy_data_dir=legacy_dir,
        secret_store=store,
    )
    original_atomic_write = migration_module.atomic_write_bytes
    corrupted_once = False

    def corrupt_first_scrub(path: Path, content: bytes, *, mode: int = 0o600) -> None:
        nonlocal corrupted_once
        if Path(path) == legacy_path and not corrupted_once:
            corrupted_once = True
            original_atomic_write(Path(path), b"{}\n", mode=mode)
            return
        original_atomic_write(Path(path), content, mode=mode)

    monkeypatch.setattr(
        migration_module,
        "atomic_write_bytes",
        corrupt_first_scrub,
    )
    with pytest.raises(MigrationError, match="secret fields were not fully scrubbed"):
        migrator.migrate_if_needed()

    assert corrupted_once
    assert legacy_path.read_bytes() == original_raw
    pending = json.loads((new_dir / "settings.json").read_text(encoding="utf-8"))
    assert pending["migration"]["status"] == "cleanup_pending"

    monkeypatch.setattr(
        migration_module,
        "atomic_write_bytes",
        original_atomic_write,
    )
    result = migrator.migrate_if_needed()
    assert result.status == "complete"
    assert result.cleanup_complete


def test_root_only_key_migrates_but_explicit_empty_preset_list_is_authoritative(
    tmp_path: Path,
) -> None:
    for name, document, expected_key in (
        ("root-only", {"api_key": "root-only-key"}, "root-only-key"),
        ("explicit-empty", {"api_key": "stale-root", "api_presets": []}, None),
    ):
        legacy_dir = tmp_path / name / "legacy"
        new_dir = tmp_path / name / "new"
        legacy_dir.mkdir(parents=True)
        legacy_path = legacy_dir / "settings.json"
        original = json.dumps(document).encode("utf-8")
        legacy_path.write_bytes(original)
        store = SecretStore(new_dir, backend=AuthenticatedTestBackend())

        settings = SettingsService(
            new_dir,
            secret_store=store,
            legacy_data_dir=legacy_dir,
        )

        assert settings.get_api_key() == expected_key
        assert b"root-only-key" not in settings.path.read_bytes()
        assert b"stale-root" not in store.path.read_bytes()
        scrubbed = json.loads(legacy_path.read_text(encoding="utf-8"))
        assert "api_key" not in scrubbed


def test_invalid_active_preset_is_repaired_without_losing_credentials(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    legacy_dir.mkdir()
    document = legacy_document()
    document["active_preset_id"] = "missing-preset"
    (legacy_dir / "settings.json").write_text(
        json.dumps(document), encoding="utf-8"
    )
    store = SecretStore(new_dir, backend=AuthenticatedTestBackend())

    settings = SettingsService(
        new_dir,
        secret_store=store,
        legacy_data_dir=legacy_dir,
    )

    assert settings.active_preset_id == "preset_0"
    assert settings.get_api_key("preset_0") == "primary-key"
    assert settings.get("migration")["active_preset_repaired"] is True


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: document.update(api_presets="not-a-list"),
        lambda document: document["api_presets"].append(
            dict(document["api_presets"][0])
        ),
        lambda document: document["api_presets"][0].update(provider="unknown"),
        lambda document: document["api_presets"][0].update(api_key="x" * 4097),
    ],
    ids=("wrong-type", "duplicate-id", "unknown-provider", "oversized-key"),
)
def test_invalid_legacy_presets_fail_closed_without_modifying_source(
    tmp_path: Path,
    mutate,
) -> None:
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    legacy_dir.mkdir()
    document = legacy_document()
    mutate(document)
    legacy_path = legacy_dir / "settings.json"
    original = json.dumps(document).encode("utf-8")
    legacy_path.write_bytes(original)
    store = SecretStore(new_dir, backend=AuthenticatedTestBackend())

    with pytest.raises(LegacySettingsValidationError):
        MigrationService(
            new_dir,
            legacy_data_dir=legacy_dir,
            secret_store=store,
        ).migrate_if_needed()

    assert legacy_path.read_bytes() == original
    assert not store.path.exists()
    assert not (new_dir / "settings.json").exists()


@pytest.mark.parametrize("raw", [b"{not-json", b"[]", b""])
def test_malformed_legacy_settings_fail_closed(tmp_path: Path, raw: bytes) -> None:
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    legacy_dir.mkdir()
    legacy_path = legacy_dir / "settings.json"
    legacy_path.write_bytes(raw)
    store = SecretStore(new_dir, backend=AuthenticatedTestBackend())

    with pytest.raises(LegacySettingsValidationError):
        MigrationService(
            new_dir,
            legacy_data_dir=legacy_dir,
            secret_store=store,
        ).migrate_if_needed()

    assert legacy_path.read_bytes() == raw
    assert not store.path.exists()
    assert not (new_dir / "settings.json").exists()


def test_two_simultaneous_migrators_are_serialized_and_idempotent(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new"
    write_legacy(legacy_dir)
    backend = AuthenticatedTestBackend()

    def migrate() -> str:
        store = SecretStore(new_dir, backend=backend)
        return MigrationService(
            new_dir,
            legacy_data_dir=legacy_dir,
            secret_store=store,
        ).migrate_if_needed().status

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _index: migrate(), range(2)))

    assert sorted(statuses) == ["complete", "migrated"]
    store = SecretStore(new_dir, backend=backend)
    settings = SettingsService(new_dir, secret_store=store, auto_migrate=False)
    assert settings.get_api_key("preset_0") == "primary-key"
    scrubbed = json.loads((legacy_dir / "settings.json").read_text(encoding="utf-8"))
    assert "api_key" not in scrubbed
