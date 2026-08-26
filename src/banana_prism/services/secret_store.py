"""DPAPI-backed storage for credentials and password verifiers.

The on-disk file contains only a Windows DPAPI blob.  There is deliberately no
plaintext fallback on non-Windows platforms: tests and future platform ports
must inject a backend implementing :class:`SecretProtectionBackend`.
"""

from __future__ import annotations

import base64
import copy
import ctypes
import hashlib
import json
import os
import stat
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from banana_prism.constants import API_PROVIDERS, APP_ID, SECRET_SCHEMA_VERSION
from banana_prism.utils.paths import PathLike, get_secret_store_path


MAX_SECRET_STORE_BYTES = 2 * 1024 * 1024
MAX_CREDENTIALS = 100
MAX_CREDENTIAL_VALUE_CHARS = 4096
MAX_LEGACY_BACKUP_BYTES = 1024 * 1024
DPAPI_DESCRIPTION = f"{APP_ID} credential store v{SECRET_SCHEMA_VERSION}"
DPAPI_ENTROPY = hashlib.sha256(
    f"{APP_ID}|credential-store|v{SECRET_SCHEMA_VERSION}".encode("utf-8")
).digest()


class SecretStoreError(RuntimeError):
    """Base error that never includes secret material in its message."""


class SecretBackendUnavailable(SecretStoreError):
    pass


class SecretStoreCorrupt(SecretStoreError):
    pass


class SecretValidationError(SecretStoreError, ValueError):
    pass


class SecretProtectionBackend(Protocol):
    """Injectable encryption boundary used by :class:`SecretStore`."""

    def protect(
        self,
        plaintext: bytes,
        *,
        description: str,
        entropy: bytes,
    ) -> bytes: ...

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes: ...


class _UnavailableBackend:
    def protect(
        self,
        plaintext: bytes,
        *,
        description: str,
        entropy: bytes,
    ) -> bytes:
        del plaintext, description, entropy
        raise SecretBackendUnavailable(
            "Windows DPAPI is unavailable; inject a platform secret backend"
        )

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        del ciphertext, entropy
        raise SecretBackendUnavailable(
            "Windows DPAPI is unavailable; inject a platform secret backend"
        )


if os.name == "nt":
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]


    class WindowsDpapiBackend:
        """Windows CurrentUser DPAPI backend.

        ``CRYPTPROTECT_LOCAL_MACHINE`` is intentionally never passed.  Therefore
        ciphertext is bound to the current Windows user and machine.
        """

        _CRYPTPROTECT_UI_FORBIDDEN = 0x1

        def __init__(self) -> None:
            self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),
                wintypes.LPCWSTR,
                ctypes.POINTER(_DATA_BLOB),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(_DATA_BLOB),
            ]
            self._crypt32.CryptProtectData.restype = wintypes.BOOL
            self._crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DATA_BLOB),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(_DATA_BLOB),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(_DATA_BLOB),
            ]
            self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
            self._kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
            self._kernel32.LocalFree.restype = wintypes.HLOCAL

        @staticmethod
        def _blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array[ctypes.c_char]]:
            buffer = ctypes.create_string_buffer(data, len(data))
            blob = _DATA_BLOB(
                len(data),
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
            )
            return blob, buffer

        def protect(
            self,
            plaintext: bytes,
            *,
            description: str,
            entropy: bytes,
        ) -> bytes:
            if not plaintext:
                raise SecretProtectionError("refusing to protect an empty payload")
            source, source_buffer = self._blob(plaintext)
            entropy_blob, entropy_buffer = self._blob(entropy)
            output = _DATA_BLOB()
            ok = self._crypt32.CryptProtectData(
                ctypes.byref(source),
                description,
                ctypes.byref(entropy_blob),
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            )
            # Keep the ctypes buffers alive until the native call returns.
            del source_buffer, entropy_buffer
            if not ok:
                code = ctypes.get_last_error()
                raise SecretProtectionError(f"DPAPI protect failed (WinError {code})")
            try:
                return ctypes.string_at(output.pbData, output.cbData)
            finally:
                if output.pbData:
                    self._kernel32.LocalFree(output.pbData)

        def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
            if not ciphertext:
                raise SecretStoreCorrupt("secret store is empty")
            source, source_buffer = self._blob(ciphertext)
            entropy_blob, entropy_buffer = self._blob(entropy)
            output = _DATA_BLOB()
            description = wintypes.LPWSTR()
            ok = self._crypt32.CryptUnprotectData(
                ctypes.byref(source),
                ctypes.byref(description),
                ctypes.byref(entropy_blob),
                None,
                None,
                self._CRYPTPROTECT_UI_FORBIDDEN,
                ctypes.byref(output),
            )
            del source_buffer, entropy_buffer
            if not ok:
                code = ctypes.get_last_error()
                raise SecretProtectionError(f"DPAPI unprotect failed (WinError {code})")
            try:
                return ctypes.string_at(output.pbData, output.cbData)
            finally:
                if output.pbData:
                    self._kernel32.LocalFree(output.pbData)
                if description:
                    self._kernel32.LocalFree(description)


else:

    class WindowsDpapiBackend(_UnavailableBackend):
        def __init__(self) -> None:
            raise SecretBackendUnavailable("Windows DPAPI is unavailable")


class SecretProtectionError(SecretStoreError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise SecretValidationError(f"{field} must be a non-empty ISO timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SecretValidationError(f"{field} is not an ISO timestamp") from exc
    return value


def _decode_b64(value: object, field: str, *, maximum: int = 4096) -> bytes:
    if not isinstance(value, str):
        raise SecretValidationError(f"{field} must be base64 text")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise SecretValidationError(f"{field} is invalid base64") from exc
    if not decoded or len(decoded) > maximum:
        raise SecretValidationError(f"{field} has an invalid length")
    return decoded


def validate_auth_record(record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise SecretValidationError("console_auth must be an object or null")
    expected = {"scheme", "iterations", "salt_b64", "dk_b64"}
    if set(record) != expected:
        raise SecretValidationError("console_auth fields are invalid")
    if record.get("scheme") != "pbkdf2-hmac-sha256":
        raise SecretValidationError("console_auth scheme is unsupported")
    iterations = record.get("iterations")
    if isinstance(iterations, bool) or not isinstance(iterations, int):
        raise SecretValidationError("console_auth iterations must be an integer")
    if not 1 <= iterations <= 10_000_000:
        raise SecretValidationError("console_auth iterations are out of range")
    salt = _decode_b64(record.get("salt_b64"), "console_auth.salt_b64", maximum=128)
    digest = _decode_b64(record.get("dk_b64"), "console_auth.dk_b64", maximum=128)
    if len(salt) < 16 or len(digest) < 16:
        raise SecretValidationError("console_auth salt or digest is too short")
    return copy.deepcopy(record)


def validate_secret_document(document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise SecretValidationError("secret document must be an object")
    allowed = {
        "schema_version",
        "generation",
        "created_at",
        "updated_at",
        "credentials",
        "console_auth",
        "migration_backup",
    }
    if not set(document).issubset(allowed):
        raise SecretValidationError("secret document contains unsupported fields")
    if document.get("schema_version") != SECRET_SCHEMA_VERSION:
        raise SecretValidationError("secret document schema version is unsupported")
    generation = document.get("generation")
    if not isinstance(generation, str):
        raise SecretValidationError("secret generation must be text")
    try:
        uuid.UUID(generation)
    except (ValueError, AttributeError) as exc:
        raise SecretValidationError("secret generation is invalid") from exc
    _validate_timestamp(document.get("created_at"), "created_at")
    _validate_timestamp(document.get("updated_at"), "updated_at")

    credentials = document.get("credentials")
    if not isinstance(credentials, dict) or len(credentials) > MAX_CREDENTIALS:
        raise SecretValidationError("credentials must be a bounded object")
    for reference, item in credentials.items():
        if (
            not isinstance(reference, str)
            or not reference.startswith("api:")
            or len(reference) > 260
            or any(ord(char) < 32 for char in reference)
        ):
            raise SecretValidationError("credential reference is invalid")
        if not isinstance(item, dict) or set(item) != {"kind", "provider", "value"}:
            raise SecretValidationError("credential entry fields are invalid")
        if item.get("kind") != "api_key":
            raise SecretValidationError("credential kind is unsupported")
        if item.get("provider") not in API_PROVIDERS:
            raise SecretValidationError("credential provider is unsupported")
        value = item.get("value")
        if not isinstance(value, str) or not value or len(value) > MAX_CREDENTIAL_VALUE_CHARS:
            raise SecretValidationError("credential value length is invalid")

    console_auth = document.get("console_auth")
    if console_auth is not None:
        validate_auth_record(console_auth)

    backup = document.get("migration_backup")
    if backup is not None:
        if not isinstance(backup, dict) or set(backup) != {
            "source",
            "source_sha256",
            "captured_at",
            "payload_b64",
        }:
            raise SecretValidationError("migration backup fields are invalid")
        if backup.get("source") != "NanaBananaStudio":
            raise SecretValidationError("migration backup source is invalid")
        fingerprint = backup.get("source_sha256")
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in fingerprint)
        ):
            raise SecretValidationError("migration backup fingerprint is invalid")
        _validate_timestamp(backup.get("captured_at"), "migration_backup.captured_at")
        _decode_b64(
            backup.get("payload_b64"),
            "migration_backup.payload_b64",
            maximum=MAX_LEGACY_BACKUP_BYTES,
        )
    return copy.deepcopy(document)


def new_secret_document() -> dict[str, Any]:
    now = _utc_now()
    return {
        "schema_version": SECRET_SCHEMA_VERSION,
        "generation": str(uuid.uuid4()),
        "created_at": now,
        "updated_at": now,
        "credentials": {},
        "console_auth": None,
    }


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Durably publish bytes through a same-directory temporary file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        try:
            os.chmod(path, mode)
        except OSError:
            # DPAPI remains the security boundary on Windows; chmod only narrows
            # permissions where the platform implements POSIX modes.
            pass
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


_T = TypeVar("_T")


class SecretStore:
    """Validated, atomic access to ``secrets.v1.dpapi``."""

    def __init__(
        self,
        data_dir: PathLike | None = None,
        *,
        path: PathLike | None = None,
        backend: SecretProtectionBackend | None = None,
    ) -> None:
        if path is None:
            self.path = get_secret_store_path(data_dir)
        else:
            self.path = Path(path).expanduser()
            if not self.path.is_absolute():
                raise ValueError("secret store path must be absolute")
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if backend is not None:
            self._backend = backend
        elif os.name == "nt":
            self._backend = WindowsDpapiBackend()
        else:
            self._backend = _UnavailableBackend()
        self._lock = threading.RLock()

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _decode_ciphertext(self, ciphertext: bytes) -> dict[str, Any]:
        try:
            plaintext = self._backend.unprotect(ciphertext, entropy=DPAPI_ENTROPY)
            if len(plaintext) > MAX_SECRET_STORE_BYTES:
                raise SecretStoreCorrupt("decrypted secret store is oversized")
            document = json.loads(plaintext.decode("utf-8"))
            return validate_secret_document(document)
        except SecretStoreError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            raise SecretStoreCorrupt("secret store cannot be decoded") from exc

    def load(self, *, required: bool = True) -> dict[str, Any]:
        with self._lock:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                if required:
                    raise
                return new_secret_document()
            if not 1 <= size <= MAX_SECRET_STORE_BYTES:
                raise SecretStoreCorrupt("secret store size is invalid")
            try:
                ciphertext = self.path.read_bytes()
            except OSError as exc:
                raise SecretStoreError("secret store could not be read") from exc
            return self._decode_ciphertext(ciphertext)

    def save(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Validate, protect, round-trip, atomically publish, and reread a document."""

        with self._lock:
            validated = validate_secret_document(dict(document))
            plaintext = json.dumps(
                validated,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            try:
                ciphertext = self._backend.protect(
                    plaintext,
                    description=DPAPI_DESCRIPTION,
                    entropy=DPAPI_ENTROPY,
                )
            except SecretStoreError:
                raise
            except Exception as exc:
                raise SecretProtectionError("secret protection backend failed") from exc
            if not ciphertext or len(ciphertext) > MAX_SECRET_STORE_BYTES:
                raise SecretProtectionError("protected secret store size is invalid")
            if self._decode_ciphertext(ciphertext) != validated:
                raise SecretProtectionError("protected secret store failed verification")

            previous: bytes | None = None
            if self.path.exists():
                previous = self.path.read_bytes()
            atomic_write_bytes(self.path, ciphertext)
            try:
                persisted = self.load()
                if persisted != validated:
                    raise SecretStoreCorrupt("persisted secret store failed verification")
            except Exception:
                if previous is None:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    atomic_write_bytes(self.path, previous)
                raise
            return persisted

    def update(
        self,
        mutator: Callable[[dict[str, Any]], _T],
    ) -> tuple[dict[str, Any], _T]:
        """Apply a mutation and commit it under the in-process store lock."""

        with self._lock:
            document = self.load(required=False)
            result = mutator(document)
            document["generation"] = str(uuid.uuid4())
            document["updated_at"] = _utc_now()
            return self.save(document), result

    def get_credential(self, reference: str) -> str | None:
        if not self.exists:
            return None
        document = self.load()
        item = document["credentials"].get(reference)
        if item is None:
            return None
        value = cast(str, item["value"])
        # A credential is most likely to leak through downstream provider error
        # text immediately after resolution.  Register it process-wide before
        # returning it so every LogService instance redacts that exact opaque
        # value, regardless of provider-specific key shape.
        from banana_prism.services.log_service import register_process_secret

        register_process_secret(value)
        return value

    def set_credential(self, reference: str, provider: str, value: str) -> str:
        def mutate(document: dict[str, Any]) -> None:
            document["credentials"][reference] = {
                "kind": "api_key",
                "provider": provider,
                "value": value,
            }

        document, _ = self.update(mutate)
        return cast(str, document["generation"])

    def delete_credential(self, reference: str) -> str | None:
        if not self.exists:
            return None

        def mutate(document: dict[str, Any]) -> None:
            document["credentials"].pop(reference, None)

        document, _ = self.update(mutate)
        return cast(str, document["generation"])

    def get_console_auth(self) -> dict[str, Any] | None:
        if not self.exists:
            return None
        value = self.load().get("console_auth")
        return copy.deepcopy(value) if value is not None else None

    def set_console_auth(self, record: Mapping[str, Any] | None) -> str:
        if record is not None:
            validate_auth_record(dict(record))

        def mutate(document: dict[str, Any]) -> None:
            document["console_auth"] = copy.deepcopy(record)

        document, _ = self.update(mutate)
        return cast(str, document["generation"])


__all__ = [
    "DPAPI_DESCRIPTION",
    "DPAPI_ENTROPY",
    "MAX_LEGACY_BACKUP_BYTES",
    "SecretBackendUnavailable",
    "SecretProtectionBackend",
    "SecretProtectionError",
    "SecretStore",
    "SecretStoreCorrupt",
    "SecretStoreError",
    "SecretValidationError",
    "WindowsDpapiBackend",
    "atomic_write_bytes",
    "new_secret_document",
    "validate_auth_record",
    "validate_secret_document",
]
