"""Console-password verification backed by the encrypted secret store."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from banana_prism.services.secret_store import (
    SecretStore,
    SecretValidationError,
    validate_auth_record,
)

if TYPE_CHECKING:
    from banana_prism.services.settings_service import SettingsService


PBKDF2_HASH_NAME = "sha256"
PBKDF2_ITERATIONS = 600_000
LEGACY_PBKDF2_ITERATIONS = 260_000
SALT_BYTES = 32
MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_CHARS = 1024


class AuthError(RuntimeError):
    pass


class PasswordPolicyError(AuthError, ValueError):
    pass


class AuthPersistenceError(AuthError):
    pass


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        PBKDF2_HASH_NAME,
        password.encode("utf-8"),
        salt,
        iterations,
    )


def make_password_record(
    password: str,
    *,
    iterations: int = PBKDF2_ITERATIONS,
    salt: bytes | None = None,
) -> dict[str, Any]:
    return _make_password_record(
        password,
        iterations=iterations,
        salt=salt,
        enforce_minimum=True,
    )


def _make_password_record(
    password: str,
    *,
    iterations: int,
    salt: bytes | None,
    enforce_minimum: bool,
) -> dict[str, Any]:
    if not isinstance(password, str):
        raise PasswordPolicyError("password must be text")
    minimum = MIN_PASSWORD_LENGTH if enforce_minimum else 0
    if not minimum <= len(password) <= MAX_PASSWORD_CHARS:
        raise PasswordPolicyError(
            f"password must contain {minimum} to {MAX_PASSWORD_CHARS} characters"
        )
    actual_salt = salt if salt is not None else os.urandom(SALT_BYTES)
    if len(actual_salt) != SALT_BYTES:
        raise PasswordPolicyError("password salt length is invalid")
    digest = _derive(password, actual_salt, iterations)
    return {
        "scheme": "pbkdf2-hmac-sha256",
        "iterations": iterations,
        "salt_b64": base64.b64encode(actual_salt).decode("ascii"),
        "dk_b64": base64.b64encode(digest).decode("ascii"),
    }


def legacy_hash_to_record(value: str) -> dict[str, Any]:
    """Convert ``salt_hex:dk_hex`` without needing the original password."""

    if not isinstance(value, str):
        raise SecretValidationError("legacy password verifier must be text")
    try:
        salt_hex, digest_hex = value.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        digest = bytes.fromhex(digest_hex)
    except (TypeError, ValueError) as exc:
        raise SecretValidationError("legacy password verifier is malformed") from exc
    if len(salt) != SALT_BYTES or len(digest) != hashlib.sha256().digest_size:
        raise SecretValidationError("legacy password verifier length is invalid")
    return {
        "scheme": "pbkdf2-hmac-sha256",
        "iterations": LEGACY_PBKDF2_ITERATIONS,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "dk_b64": base64.b64encode(digest).decode("ascii"),
    }


def verify_password_record(password: str, record: Mapping[str, Any]) -> bool:
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_CHARS:
        return False
    try:
        validated = validate_auth_record(dict(record))
        salt = base64.b64decode(validated["salt_b64"], validate=True)
        expected = base64.b64decode(validated["dk_b64"], validate=True)
        candidate = _derive(password, salt, validated["iterations"])
    except (SecretValidationError, ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def verify_legacy_hash(password: str, encoded: str) -> bool:
    try:
        record = legacy_hash_to_record(encoded)
    except SecretValidationError:
        return False
    return verify_password_record(password, record)


class AuthService:
    """Manage a self-describing password verifier inside :class:`SecretStore`."""

    def __init__(
        self,
        secret_store: SecretStore | SettingsService,
        settings: SettingsService | None = None,
    ) -> None:
        if isinstance(secret_store, SecretStore):
            self.secret_store = secret_store
            self.settings = settings
        else:
            # Preserve the legacy ``AuthService(settings)`` construction shape
            # while routing all verifier bytes through the new SecretStore.
            candidate = getattr(secret_store, "secret_store", None)
            if not isinstance(candidate, SecretStore):
                raise TypeError("settings service does not expose a SecretStore")
            self.secret_store = candidate
            self.settings = secret_store

    def _commit_record(self, record: Mapping[str, Any] | None) -> None:
        previous_exists = self.secret_store.exists
        previous = self.secret_store.load() if previous_exists else None
        generation = self.secret_store.set_console_auth(record)
        if self.settings is None:
            return
        try:
            self.settings.record_secret_generation(
                generation,
                password_configured=record is not None,
            )
        except Exception as exc:
            # Keep the security and UI state in one generation.  A failed
            # non-secret commit must not silently leave a different password in
            # effect than the settings UI reports.
            try:
                if previous is None:
                    self.secret_store.path.unlink(missing_ok=True)
                else:
                    self.secret_store.save(previous)
            except Exception as rollback_exc:
                raise AuthPersistenceError(
                    "password update failed and secret rollback also failed"
                ) from rollback_exc
            raise AuthPersistenceError("password update could not be committed") from exc

    def has_password(self) -> bool:
        if self.settings is not None:
            self.settings.verify_secret_binding()
            if not self.settings.console_password_configured:
                return False
        record = self.secret_store.get_console_auth()
        if record is None:
            return False
        validate_auth_record(record)
        return True

    def set_password(self, password: str) -> None:
        self._commit_record(make_password_record(password))

    def clear_password(self) -> None:
        self._commit_record(None)

    def verify_password(self, password: str) -> bool:
        if self.settings is not None:
            self.settings.verify_secret_binding()
            if not self.settings.console_password_configured:
                return False
        record = self.secret_store.get_console_auth()
        if record is None or not verify_password_record(password, record):
            return False
        iterations = record.get("iterations")
        if isinstance(iterations, int) and iterations < PBKDF2_ITERATIONS:
            # Opportunistic upgrade happens only after a successful proof of the
            # password and uses a fresh random salt.  Legacy AuthService callers
            # could create a verifier shorter than today's UI minimum, so an
            # already-valid legacy password is not locked out during upgrade.
            self._commit_record(
                _make_password_record(
                    password,
                    iterations=PBKDF2_ITERATIONS,
                    salt=None,
                    enforce_minimum=False,
                )
            )
        return True

    @staticmethod
    def verify_legacy_password(password: str, encoded: str) -> bool:
        return verify_legacy_hash(password, encoded)


__all__ = [
    "AuthError",
    "AuthPersistenceError",
    "AuthService",
    "LEGACY_PBKDF2_ITERATIONS",
    "MAX_PASSWORD_CHARS",
    "MIN_PASSWORD_LENGTH",
    "PBKDF2_HASH_NAME",
    "PBKDF2_ITERATIONS",
    "PasswordPolicyError",
    "legacy_hash_to_record",
    "make_password_record",
    "verify_legacy_hash",
    "verify_password_record",
]
