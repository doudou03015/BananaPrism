"""Filesystem locations used by BananaPrism.

Path resolution is deliberately injectable.  Tests and portable deployments can
set ``BANANAPRISM_DATA_DIR`` (or pass ``data_dir`` explicitly) and will never
touch the user's real roaming profile.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TypeAlias

from banana_prism.constants import APP_ID, LEGACY_APP_ID


PathLike: TypeAlias = str | os.PathLike[str]
DATA_DIR_ENV = "BANANAPRISM_DATA_DIR"
SAVE_DIR_ENV = "BANANAPRISM_SAVE_DIR"


def _absolute_path(value: PathLike, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path


def _platform_data_root() -> Path:
    if sys.platform == "win32":
        configured = os.environ.get("APPDATA")
        return Path(configured) if configured else Path.home() / "AppData" / "Roaming"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    configured = os.environ.get("XDG_CONFIG_HOME")
    return Path(configured) if configured else Path.home() / ".config"


def get_app_data_dir(
    data_dir: PathLike | None = None,
    *,
    create: bool = True,
) -> Path:
    """Return the BananaPrism data directory.

    Precedence is an explicit argument, ``BANANAPRISM_DATA_DIR``, then the
    platform roaming/configuration directory.  Overrides must be absolute so a
    changed working directory cannot redirect secrets unexpectedly.
    """

    if data_dir is not None:
        path = _absolute_path(data_dir, label="data_dir")
    elif configured := os.environ.get(DATA_DIR_ENV):
        path = _absolute_path(configured, label=DATA_DIR_ENV)
    else:
        path = _platform_data_root() / APP_ID
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_legacy_app_data_dir(
    legacy_data_dir: PathLike | None = None,
    *,
    create: bool = False,
) -> Path:
    """Return NanaBananaStudio's legacy data directory.

    The new data-directory environment override is intentionally *not* reused
    here.  Callers using an injected BananaPrism directory must explicitly inject
    a legacy directory as well; this prevents tests from probing real AppData.
    """

    path = (
        _absolute_path(legacy_data_dir, label="legacy_data_dir")
        if legacy_data_dir is not None
        else _platform_data_root() / LEGACY_APP_ID
    )
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_default_save_dir(
    save_dir: PathLike | None = None,
    *,
    create: bool = False,
) -> Path:
    """Return the default image directory without creating it by default."""

    if save_dir is not None:
        path = _absolute_path(save_dir, label="save_dir")
    elif configured := os.environ.get(SAVE_DIR_ENV):
        path = _absolute_path(configured, label=SAVE_DIR_ENV)
    else:
        path = Path.home() / "Pictures" / APP_ID
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_settings_path(data_dir: PathLike | None = None) -> Path:
    return get_app_data_dir(data_dir) / "settings.json"


def get_secret_store_path(data_dir: PathLike | None = None) -> Path:
    return get_app_data_dir(data_dir) / "secrets.v1.dpapi"


def get_logs_dir(data_dir: PathLike | None = None, *, create: bool = True) -> Path:
    path = get_app_data_dir(data_dir) / "logs"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_resource_path(relative: PathLike) -> Path:
    """Resolve a packaged or source-tree resource without allowing traversal."""

    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise ValueError("resource path must be relative and stay under resources")
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root is not None:
        resources = Path(bundle_root) / "resources"
    else:
        resources = Path(__file__).resolve().parents[3] / "resources"
    resolved = (resources / requested).resolve(strict=False)
    resource_root = resources.resolve(strict=False)
    try:
        resolved.relative_to(resource_root)
    except ValueError as exc:
        raise ValueError("resource path escapes the resources directory") from exc
    return resolved


__all__ = [
    "DATA_DIR_ENV",
    "SAVE_DIR_ENV",
    "PathLike",
    "get_app_data_dir",
    "get_default_save_dir",
    "get_legacy_app_data_dir",
    "get_logs_dir",
    "get_resource_path",
    "get_secret_store_path",
    "get_settings_path",
]
