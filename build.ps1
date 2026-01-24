# Build standalone executable for Yank clipboard sync (Windows)
# Usage: .\build.ps1

$ErrorActionPreference = "Stop"

Write-Host "=== Yank Build Script ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Platform: Windows"
Write-Host ""

# Check for virtual environment
if (Test-Path "venv\Scripts\Activate.ps1") {
    Write-Host "Activating virtual environment..."
    & "venv\Scripts\Activate.ps1"
} else {
    Write-Host "Warning: No virtual environment found. Using system Python." -ForegroundColor Yellow
}

# Check for PyInstaller
try {
    python -c "import PyInstaller" 2>$null
} catch {
    Write-Host "Installing PyInstaller..."
    pip install pyinstaller
}

# Clean previous builds
Write-Host "Cleaning previous builds..."
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

# Build
Write-Host ""
Write-Host "Building standalone executable..."
python -m PyInstaller yank.spec --clean

# Show result
Write-Host ""
Write-Host "=== Build Complete ===" -ForegroundColor Green
Write-Host ""

if (Test-Path "dist\yank.exe") {
    Get-Item "dist\yank.exe" | Select-Object Name, Length, LastWriteTime
    Write-Host ""
    Write-Host "Executable created at: dist\yank.exe"
    Write-Host ""
    Write-Host "To run: .\dist\yank.exe start"
    Write-Host "For help: .\dist\yank.exe --help"
}
