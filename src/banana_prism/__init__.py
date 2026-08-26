"""BananaPrism application package."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


def _package_version() -> str:
    try:
        return version("banana-prism")
    except PackageNotFoundError:
        # Source checkouts deliberately read pyproject.toml instead of carrying a
        # second version literal. Frozen builds include distribution metadata.
        for parent in Path(__file__).resolve().parents:
            project_file = parent / "pyproject.toml"
            if project_file.is_file():
                try:
                    with project_file.open("rb") as stream:
                        return str(tomllib.load(stream)["project"]["version"])
                except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
                    break
        return "0+unknown"


__version__ = _package_version()

__all__ = ["__version__"]
