$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Assert-LastExitCode([string] $step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$step failed with exit code $LASTEXITCODE"
    }
}

# Keep release builds offline and record the exact source/toolchain fingerprint.
# Installation instructions are documented separately; this check reports
# missing dependencies without asking pip to modify the user's environment.
python -c "import PIL, PyInstaller, PySide6, pytest"
Assert-LastExitCode 'Build dependency check'
python -c "import platform, struct; assert struct.calcsize('P') == 8 and platform.machine().lower() in {'amd64', 'x86_64'}, 'Windows x64 Python is required'"
Assert-LastExitCode 'Windows x64 toolchain check'
python scripts\write_source_fingerprint.py
Assert-LastExitCode 'Source fingerprint capture'
python scripts\generate_icon.py
Assert-LastExitCode 'Icon generation'
python scripts\generate_version_info.py
Assert-LastExitCode 'Version resource generation'
python -m pytest -p no:cacheprovider
Assert-LastExitCode 'Test suite'
python scripts\capture_ui.py
Assert-LastExitCode 'Offscreen UI capture'
python -m PyInstaller --noconfirm --clean BananaPrism.spec
Assert-LastExitCode 'PyInstaller build'
Copy-Item -LiteralPath README.md, PRIVACY.md, CHANGELOG.md -Destination dist\BananaPrism -Force
python scripts\verify_package.py
Assert-LastExitCode 'Packaged verification'
python scripts\write_manifest.py
Assert-LastExitCode 'Release manifest verification'

Write-Output 'Build complete: dist\BananaPrism\BananaPrism.exe'
