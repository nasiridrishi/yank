#Requires -Version 5.1
<#
.SYNOPSIS
    Automated setup script for Yank - LAN Clipboard Sync (Windows)

.DESCRIPTION
    Automatically sets up the Python environment and installs Yank with Windows dependencies.
#>

Write-Host ""
Write-Host "==================================" -ForegroundColor Cyan
Write-Host "  Yank - LAN Clipboard Sync" -ForegroundColor Cyan
Write-Host "  Automated Setup" -ForegroundColor Cyan
Write-Host "==================================" -ForegroundColor Cyan
Write-Host ""

# Check Python installation
Write-Host "Checking Python installation..." -ForegroundColor Yellow

$pythonCmd = $null
foreach ($cmd in @("python", "python3", "py")) {
    try {
        $version = & $cmd --version 2>&1
        if ($version -match "Python (\d+\.\d+)") {
            $pythonCmd = $cmd
            $pythonVersion = $Matches[1]
            break
        }
    } catch {
        continue
    }
}

if (-not $pythonCmd) {
    Write-Host "[ERROR] Python not found" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please install Python 3.8 or higher from:" -ForegroundColor Yellow
    Write-Host "  https://www.python.org/downloads/windows/" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Make sure to check 'Add Python to PATH' during installation!" -ForegroundColor Yellow
    exit 1
}

Write-Host "[OK] Found Python $pythonVersion using command: $pythonCmd" -ForegroundColor Green
Write-Host ""

# Check Python version (require 3.8+)
$versionParts = $pythonVersion.Split('.')
$majorVersion = [int]$versionParts[0]
$minorVersion = [int]$versionParts[1]

if ($majorVersion -lt 3 -or ($majorVersion -eq 3 -and $minorVersion -lt 8)) {
    Write-Host "[ERROR] Python 3.8 or higher required (found $pythonVersion)" -ForegroundColor Red
    exit 1
}

# Create virtual environment
if (Test-Path "venv") {
    Write-Host "[OK] Virtual environment already exists" -ForegroundColor Green
} else {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    & $pythonCmd -m venv venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] Failed to create virtual environment" -ForegroundColor Red
        exit 1
    }
    Write-Host "[OK] Virtual environment created" -ForegroundColor Green
}
Write-Host ""

# Activate virtual environment
Write-Host "Activating virtual environment..." -ForegroundColor Yellow
$activateScript = ".\venv\Scripts\Activate.ps1"

if (-not (Test-Path $activateScript)) {
    Write-Host "[ERROR] Activation script not found at $activateScript" -ForegroundColor Red
    exit 1
}

& $activateScript

# Upgrade pip
Write-Host "Upgrading pip, setuptools, and wheel..." -ForegroundColor Yellow
python -m pip install --upgrade pip setuptools wheel --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "[OK] Build tools upgraded" -ForegroundColor Green
} else {
    Write-Host "[WARNING] Failed to upgrade build tools, continuing anyway..." -ForegroundColor Yellow
}
Write-Host ""

# Install package with Windows dependencies
Write-Host "Installing Yank with Windows dependencies..." -ForegroundColor Yellow
Write-Host "(This may take a few minutes, especially for pywin32...)" -ForegroundColor Gray
Write-Host ""

python -m pip install -e ".[windows]"

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "[OK] Yank installed successfully!" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "[ERROR] Installation failed" -ForegroundColor Red
    exit 1
}
Write-Host ""

# Verify installation
Write-Host "Verifying installation..." -ForegroundColor Yellow
$verifyScript = @"
from yank.platform import get_platform_info
print('[OK] Platform detection:', get_platform_info().display_name)
"@

python -c $verifyScript 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "[OK] Installation verified" -ForegroundColor Green
} else {
    Write-Host "[WARNING] Could not verify installation" -ForegroundColor Yellow
}
Write-Host ""

Write-Host "==================================" -ForegroundColor Cyan
Write-Host "  Setup Complete!" -ForegroundColor Green
Write-Host "==================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host ""
Write-Host "1. Activate the virtual environment:" -ForegroundColor White
Write-Host "   .\venv\Scripts\Activate.ps1" -ForegroundColor Cyan
Write-Host ""
Write-Host "2. Pair with another device:" -ForegroundColor White
Write-Host "   .\run.ps1 pair" -ForegroundColor Cyan
Write-Host ""
Write-Host "3. Start syncing:" -ForegroundColor White
Write-Host "   .\run.ps1 start" -ForegroundColor Cyan
Write-Host ""
Write-Host "4. View help:" -ForegroundColor White
Write-Host "   .\run.ps1 help" -ForegroundColor Cyan
Write-Host ""
