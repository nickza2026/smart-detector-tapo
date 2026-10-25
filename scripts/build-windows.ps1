param([string]$PythonExecutable = "")

$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "Build Windows executables on Windows, not macOS/Linux." }
Set-Location (Split-Path $PSScriptRoot -Parent)

function Run-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

if ($PythonExecutable) {
    Run-Checked -Program $PythonExecutable -Arguments @("-c", "import sys; assert sys.version_info[:2] == (3, 12), 'Python 3.12 is required'")
    Run-Checked -Program $PythonExecutable -Arguments @("-m", "venv", ".venv-build")
} else {
    Run-Checked -Program "py" -Arguments @("-3.12", "-m", "venv", ".venv-build")
}
$Python = Join-Path (Get-Location) ".venv-build\Scripts\python.exe"
Run-Checked -Program $Python -Arguments @("-m", "pip", "install", "-r", "requirements-build.txt")
Run-Checked -Program $Python -Arguments @("-m", "pip", "check")
Run-Checked -Program $Python -Arguments @("-m", "unittest", "discover", "-s", "tests", "-v")
Run-Checked -Program $Python -Arguments @("-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--paths", ".", "--name", "SmartTapo", "entrypoints/desktop.py")
Run-Checked -Program $Python -Arguments @("-m", "scripts.smoke_packaged", "--dist", "dist")
Copy-Item "README.md" "dist/README.md" -Force
Write-Host "Built windowed SmartTapo.exe in dist. Double-click it for first-run setup. ADB and MEmu must be installed separately."
Write-Host "No camera or Pushover was contacted by this build. Live Windows integration still requires operator testing."
