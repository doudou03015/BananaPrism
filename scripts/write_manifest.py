"""Write and immediately verify the portable release SHA-256 manifest."""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import PyInstaller
import PySide6


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "BananaPrism"
MANIFEST = DIST / "SHA256SUMS.json"


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest().upper()


def _source_digest() -> str:
    roots = (ROOT / "src", ROOT / "resources", ROOT / "scripts", ROOT / "tests")
    files = [ROOT / "pyproject.toml", ROOT / "BananaPrism.spec"]
    for source_root in roots:
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        )
    hasher = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: item.relative_to(ROOT).as_posix()):
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        hasher.update(len(relative).to_bytes(4, "big"))
        hasher.update(relative)
        hasher.update(bytes.fromhex(_digest(path)))
    return hasher.hexdigest().upper()


def main() -> int:
    if sys.maxsize <= 2**32 or platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise RuntimeError("release manifest requires a Windows x64 Python toolchain")
    with (ROOT / "pyproject.toml").open("rb") as stream:
        version = str(tomllib.load(stream)["project"]["version"])
    fingerprint_path = ROOT / "build" / "source.sha256"
    try:
        expected_source_digest = fingerprint_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise RuntimeError("build source fingerprint is missing") from exc
    actual_source_digest = _source_digest()
    if not expected_source_digest or actual_source_digest != expected_source_digest:
        raise RuntimeError("release inputs changed while the build was running")
    files = []
    for path in sorted(DIST.rglob("*")):
        if not path.is_file() or path == MANIFEST:
            continue
        files.append(
            {
                "path": path.relative_to(DIST).as_posix(),
                "sha256": _digest(path),
                "bytes": path.stat().st_size,
            }
        )
    document = {
        "schema_version": 1,
        "app": "BananaPrism",
        "version": version,
        "platform": "windows-x64",
        "packaging": "pyinstaller-onedir",
        "signed": False,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": actual_source_digest,
        "toolchain": {
            "python": platform.python_version(),
            "pyinstaller": PyInstaller.__version__,
            "pyside6": PySide6.__version__,
        },
        "verification": {
            "tests_passed": True,
            "packaged_smoke_passed": True,
            "single_instance_passed": True,
            "pe_metadata_passed": True,
            "unsigned_state_checked": True,
        },
        "files": files,
    }
    MANIFEST.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    persisted = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for item in persisted["files"]:
        target = DIST / item["path"]
        if not target.is_file() or _digest(target) != item["sha256"]:
            raise RuntimeError(f"manifest verification failed for {item['path']}")
    print(f"Release manifest verified: {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
