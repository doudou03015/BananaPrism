"""Snapshot release inputs so a build cannot attest to moving source."""

from __future__ import annotations

from pathlib import Path

from write_manifest import ROOT, _source_digest


def main() -> int:
    target = ROOT / "build" / "source.sha256"
    target.parent.mkdir(parents=True, exist_ok=True)
    fingerprint = _source_digest()
    target.write_text(fingerprint + "\n", encoding="ascii", newline="\n")
    print(f"Source fingerprint captured: {fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
