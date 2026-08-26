"""Generate a PyInstaller Windows version resource from pyproject.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    numeric = [int(part) for part in version.split("+")[0].split("-")[0].split(".")]
    numeric = (numeric + [0, 0, 0, 0])[:4]
    dotted = ".".join(str(part) for part in numeric)
    target = ROOT / "build" / "version_info.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({', '.join(map(str, numeric))}),
    prodvers=({', '.join(map(str, numeric))}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'080404B0',
        [StringStruct(u'CompanyName', u'BananaPrism'),
         StringStruct(u'FileDescription', u'BananaPrism — AI 图像工作室'),
         StringStruct(u'FileVersion', u'{dotted}'),
         StringStruct(u'InternalName', u'BananaPrism'),
         StringStruct(u'LegalCopyright', u'Copyright (c) 2026'),
         StringStruct(u'OriginalFilename', u'BananaPrism.exe'),
         StringStruct(u'ProductName', u'BananaPrism'),
         StringStruct(u'ProductVersion', u'{dotted}')])
    ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    # Build scripts consume this generated value instead of carrying another
    # version literal.  ``pyproject.toml`` remains the only editable source.
    (ROOT / "build" / "version.txt").write_text(
        f"{version}\n",
        encoding="utf-8",
    )
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
