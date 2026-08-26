"""Wait for and verify the frozen Windows executable in an isolated profile."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

import pefile


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "BananaPrism" / "BananaPrism.exe"
SINGLE_INSTANCE_EXIT_CODE = 3


def _verify_pe_metadata(expected_version: str) -> None:
    image = pefile.PE(str(EXE))
    try:
        if image.FILE_HEADER.Machine != 0x8664:
            raise RuntimeError("packaged executable is not Windows x64")
        security = image.OPTIONAL_HEADER.DATA_DIRECTORY[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_SECURITY"]
        ]
        if security.Size != 0:
            raise RuntimeError("release unexpectedly contains a code signature")
        entries: dict[str, str] = {}
        for group in getattr(image, "FileInfo", ()):
            for block in group:
                for table in getattr(block, "StringTable", ()):
                    entries.update(
                        {
                            key.decode("utf-8", "replace"): value.decode("utf-8", "replace")
                            for key, value in table.entries.items()
                        }
                    )
        numeric = expected_version.split("+", 1)[0].split("-", 1)[0].split(".")
        dotted = ".".join((numeric + ["0", "0", "0", "0"])[:4])
        expected = {
            "FileVersion": dotted,
            "ProductVersion": dotted,
            "ProductName": "BananaPrism",
            "OriginalFilename": "BananaPrism.exe",
        }
        if any(entries.get(key) != value for key, value in expected.items()):
            raise RuntimeError(f"packaged PE version metadata mismatch: {entries!r}")
    finally:
        image.close()

    production_inputs = [ROOT / "pyproject.toml", ROOT / "BananaPrism.spec"]
    production_inputs.extend((ROOT / "src").rglob("*.py"))
    production_inputs.extend((ROOT / "resources").rglob("*"))
    newest_input = max(path.stat().st_mtime for path in production_inputs if path.is_file())
    if EXE.stat().st_mtime + 1 < newest_input:
        raise RuntimeError("packaged executable is older than a production source input")


def _environment(profile: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["BANANAPRISM_DATA_DIR"] = str(profile)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    return environment


def _run(profile: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(EXE), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=_environment(profile),
    )


def _verify_two_process_lock(profile: Path) -> None:
    lock_path = profile / ".BananaPrism.instance.lock"
    owner = subprocess.Popen(
        [str(EXE), "--smoke-test", "--smoke-hold-ms", "3000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_environment(profile),
    )
    try:
        deadline = time.monotonic() + 10
        while not lock_path.is_file() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if not lock_path.is_file():
            stdout, stderr = owner.communicate(timeout=5)
            raise RuntimeError(
                "packaged lock owner did not acquire the profile lock: "
                f"exit={owner.returncode}, stdout={stdout.strip()!r}, stderr={stderr.strip()!r}"
            )

        contender = _run(profile, "--smoke-test")
        if contender.returncode != SINGLE_INSTANCE_EXIT_CODE:
            raise RuntimeError(
                "packaged second instance was not rejected: "
                f"expected exit {SINGLE_INSTANCE_EXIT_CODE}, got {contender.returncode}; "
                f"stderr={contender.stderr.strip()!r}"
            )

        stdout, stderr = owner.communicate(timeout=30)
        if owner.returncode != 0:
            raise RuntimeError(
                "packaged lock owner failed: "
                f"exit={owner.returncode}, stdout={stdout.strip()!r}, stderr={stderr.strip()!r}"
            )
    finally:
        if owner.poll() is None:
            owner.terminate()
            owner.wait(timeout=10)


def main() -> int:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        expected_version = str(tomllib.load(stream)["project"]["version"])
    if not EXE.is_file():
        raise RuntimeError(f"packaged executable is missing: {EXE}")
    for document in ("README.md", "PRIVACY.md", "CHANGELOG.md"):
        if not (EXE.parent / document).is_file():
            raise RuntimeError(f"packaged documentation is missing: {document}")
    _verify_pe_metadata(expected_version)

    with tempfile.TemporaryDirectory(prefix="BananaPrism-packaged-smoke-") as temporary:
        profile = Path(temporary)
        version = _run(profile, "--version")
        if version.returncode != 0 or version.stdout.strip() != expected_version:
            raise RuntimeError(
                "packaged runtime version mismatch: "
                f"expected {expected_version!r}, got {version.stdout.strip()!r}; "
                f"stderr={version.stderr.strip()!r}"
            )

        smoke = _run(profile, "--smoke-test")
        if smoke.returncode != 0:
            raise RuntimeError(
                f"packaged smoke test failed ({smoke.returncode}): {smoke.stderr.strip()}"
            )
        settings_path = profile / "settings.json"
        if not settings_path.is_file():
            raise RuntimeError("packaged smoke test did not initialise isolated settings")
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        serialized = json.dumps(settings, ensure_ascii=False).casefold()
        if '"api_key"' in serialized or '"console_password_hash"' in serialized:
            raise RuntimeError("isolated settings contains a forbidden plaintext secret field")

        _verify_two_process_lock(profile)

    print(f"Packaged verification passed: BananaPrism {expected_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
