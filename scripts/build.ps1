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
python scripts\generate_icon.py
Assert-LastExitCode 'Icon generation'
python scripts\generate_version_info.py
Assert-LastExitCode 'Version resource generation'
# Generated brand assets are release inputs; capture their final bytes before
# verification so a toolchain update cannot invalidate an otherwise sound build.
python scripts\write_source_fingerprint.py
Assert-LastExitCode 'Source fingerprint capture'
python -m pytest -p no:cacheprovider
Assert-LastExitCode 'Test suite'
python scripts\capture_ui.py
Assert-LastExitCode 'Offscreen UI capture'
# Third-party PATH entries can supply incompatible ICU/OpenSSL/CRT DLLs even
# when source execution uses the correct system libraries. Freeze against only
# the selected Python environment and Windows; restore the caller's PATH after.
$buildPython = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$previousBuildPath = $env:PATH
try {
    $env:PATH = @(
        (Split-Path -Parent $buildPython),
        (Join-Path $env:SystemRoot 'System32'),
        $env:SystemRoot
    ) -join ';'
    & $buildPython -m PyInstaller --noconfirm --clean BananaPrism.spec
    Assert-LastExitCode 'PyInstaller build'
} finally {
    $env:PATH = $previousBuildPath
}
Copy-Item -LiteralPath README.md, PRIVACY.md, CHANGELOG.md -Destination dist\BananaPrism -Force
New-Item -ItemType Directory -Path dist\BananaPrism\docs -Force | Out-Null
Copy-Item -LiteralPath docs\nano-banana-2.1.md -Destination dist\BananaPrism\docs -Force
python scripts\verify_package.py
Assert-LastExitCode 'Packaged verification'
python scripts\write_manifest.py
Assert-LastExitCode 'Release manifest verification'

Write-Output 'Build complete: dist\BananaPrism\BananaPrism.exe'
