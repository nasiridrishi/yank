#Requires -Version 5.1
<#
.SYNOPSIS
    Clipboard-Sync Process Manager for Windows

.DESCRIPTION
    Manages the clipboard-sync Python application lifecycle using background jobs.
    Supports start, stop, restart, attach, logs, tail, status, pair, join, unpair commands.

.PARAMETER Command
    The command to execute: start, stop, restart, attach, logs, tail, status, pair, join, unpair, help
    Default: start

.EXAMPLE
    .\run.ps1 start
    .\run.ps1 pair
    .\run.ps1 join 192.168.1.5 123456
    .\run.ps1 status
#>

param(
    [Parameter(Position = 0)]
    [string]$Command = "start",

    [Parameter(Position = 1)]
    [string]$Arg1 = "",

    [Parameter(Position = 2)]
    [string]$Arg2 = "",

    [Parameter()]
    [string]$Peer = "",

    [Parameter()]
    [switch]$Verbose,

    [Parameter()]
    [switch]$NoSecurity
)

# Configuration
$SessionName = "clipboard-sync"
$VenvPath = ".\venv"
$LogDir = ".\logs"
$LogFile = "$LogDir\clipboard-sync.log"
$PidFile = "$LogDir\clipboard-sync.pid"
$ScriptRoot = $PSScriptRoot
if (-not $ScriptRoot) { $ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $ScriptRoot) { $ScriptRoot = Get-Location }

# Resolve to absolute paths
$VenvPath = Join-Path $ScriptRoot "venv"
$LogDir = Join-Path $ScriptRoot "logs"
$LogFile = Join-Path $LogDir "clipboard-sync.log"
$PidFile = Join-Path $LogDir "clipboard-sync.pid"

# Helper Functions
function Write-Info { param($Message) Write-Host "[INFO] $Message" -ForegroundColor Cyan }
function Write-Success { param($Message) Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Error { param($Message) Write-Host "[ERROR] $Message" -ForegroundColor Red }
function Write-Warn { param($Message) Write-Host "[WARN] $Message" -ForegroundColor Yellow }

function Ensure-LogDirectory {
    if (-not (Test-Path $LogDir)) {
        New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
        Write-Info "Created logs directory: $LogDir"
    }
}

function Get-ProcessId {
    if (Test-Path $PidFile) {
        $storedPid = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($storedPid -and $storedPid -match '^\d+$') {
            return [int]$storedPid
        }
    }
    return $null
}

function Test-ProcessRunning {
    $processId = Get-ProcessId
    if ($processId) {
        $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($proc -and -not $proc.HasExited) {
            return $true
        }
    }

    # Also check by window title as fallback
    $procs = Get-Process | Where-Object { $_.MainWindowTitle -like "*$SessionName*" }
    if ($procs) {
        return $true
    }

    return $false
}

function Get-RunningProcess {
    $processId = Get-ProcessId
    if ($processId) {
        $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($proc -and -not $proc.HasExited) {
            return $proc
        }
    }
    return $null
}

function Show-Help {
    Write-Host ""
    Write-Host "Yank - LAN Clipboard Sync" -ForegroundColor Cyan
    Write-Host "=========================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Usage: .\run.ps1 [command] [options]" -ForegroundColor White
    Write-Host ""
    Write-Host "Process Commands:" -ForegroundColor Yellow
    Write-Host "  start [options]  - Start clipboard sync (default)"
    Write-Host "    -Peer IP       - Connect to specific IP"
    Write-Host "    -Verbose       - Enable verbose logging"
    Write-Host "    -NoSecurity    - Disable encryption (not recommended)"
    Write-Host "  stop             - Stop the running application"
    Write-Host "  restart          - Restart the application"
    Write-Host "  attach           - Show running process information"
    Write-Host "  logs             - View the complete log file"
    Write-Host "  tail             - Follow logs in real-time (Ctrl+C to exit)"
    Write-Host ""
    Write-Host "Security Commands:" -ForegroundColor Yellow
    Write-Host "  pair             - Enter pairing mode (display PIN)"
    Write-Host "  join <IP> <PIN>  - Pair with another device"
    Write-Host "  unpair           - Remove current pairing"
    Write-Host "  status           - Show pairing and process status"
    Write-Host ""
    Write-Host "Configuration:" -ForegroundColor Yellow
    Write-Host "  config                    - Show current configuration"
    Write-Host "  config --set KEY VALUE    - Set a configuration value"
    Write-Host "  config --reset            - Reset to defaults"
    Write-Host ""
    Write-Host "Other:" -ForegroundColor Yellow
    Write-Host "  help             - Display this help message"
    Write-Host ""
    Write-Host "Examples:" -ForegroundColor Yellow
    Write-Host "  .\run.ps1 pair                          # Display PIN for pairing"
    Write-Host "  .\run.ps1 join 192.168.1.5 123456       # Pair with device"
    Write-Host "  .\run.ps1 start                         # Start syncing (encrypted)"
    Write-Host "  .\run.ps1 start -Verbose                # Start with debug logging"
    Write-Host "  .\run.ps1 start -Peer 192.168.1.5       # Connect to specific IP"
    Write-Host "  .\run.ps1 start -NoSecurity             # Start without encryption"
    Write-Host "  .\run.ps1 config --set sync_text false  # Disable text sync"
    Write-Host ""
    Write-Host "Files:" -ForegroundColor Gray
    Write-Host "  Config:     sync_config.json"
    Write-Host "  Ignore:     .syncignore"
    Write-Host "  Log File:   $LogFile"
    Write-Host ""
}

function Start-Application {
    param(
        [string]$PeerIP = "",
        [switch]$VerboseMode,
        [switch]$NoSecurityMode
    )

    if (Test-ProcessRunning) {
        Write-Warn "Application is already running!"
        Write-Info "Use '.\run.ps1 restart' to restart or '.\run.ps1 status' to check status."
        return
    }

    # Verify virtual environment exists
    $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    if (-not (Test-Path $activateScript)) {
        Write-Error "Virtual environment not found at: $VenvPath"
        Write-Info "Please create a virtual environment first: python -m venv venv"
        return
    }

    Ensure-LogDirectory

    # Build extra arguments
    $extraArgs = ""
    if ($PeerIP) {
        $extraArgs += " --peer $PeerIP"
    }
    if ($VerboseMode) {
        $extraArgs += " --verbose"
    }
    if ($NoSecurityMode) {
        $extraArgs += " --no-security"
    }

    Write-Info "Starting $SessionName..."

    # Create a wrapper script that activates venv and runs the application
    $wrapperScript = @"
Set-Location '$ScriptRoot'
& '$activateScript'
`$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path '$LogFile' -Value "`n========================================`n[`$timestamp] Starting $SessionName`n========================================"
python -m main start$extraArgs 2>&1 | Tee-Object -FilePath '$LogFile' -Append
"@

    # Start the process in a new hidden PowerShell window
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($wrapperScript))

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = "powershell.exe"
    $startInfo.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $encodedCommand"
    $startInfo.UseShellExecute = $true
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden

    try {
        $process = [System.Diagnostics.Process]::Start($startInfo)

        # Save PID
        $process.Id | Out-File -FilePath $PidFile -Force

        Start-Sleep -Seconds 2

        if (Test-ProcessRunning) {
            Write-Success "Application started successfully!"
            Write-Info "Process ID: $($process.Id)"
            Write-Info "Log file: $LogFile"
            Write-Info "Use '.\run.ps1 tail' to follow logs"
        }
        else {
            Write-Error "Application may have failed to start. Check logs:"
            Write-Info ".\run.ps1 logs"
        }
    }
    catch {
        Write-Error "Failed to start application: $_"
    }
}

function Stop-Application {
    $processId = Get-ProcessId

    if (-not $processId) {
        Write-Warn "No PID file found. Application may not be running."

        # Try to find by name anyway
        $pythonProcs = Get-Process -Name "python" -ErrorAction SilentlyContinue
        if ($pythonProcs) {
            Write-Info "Found Python processes. Checking if any are clipboard-sync..."
        }
        return
    }

    $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue

    if ($proc) {
        Write-Info "Stopping $SessionName (PID: $processId)..."

        try {
            # Try graceful stop first
            $proc.CloseMainWindow() | Out-Null
            $proc.WaitForExit(5000) | Out-Null

            if (-not $proc.HasExited) {
                Write-Warn "Graceful shutdown timed out, forcing stop..."
                $proc.Kill()
            }

            Write-Success "Application stopped."
        }
        catch {
            Write-Error "Error stopping process: $_"
        }
    }
    else {
        Write-Warn "Process with PID $processId is not running."
    }

    # Clean up PID file
    if (Test-Path $PidFile) {
        Remove-Item $PidFile -Force
    }
}

function Restart-Application {
    Write-Info "Restarting $SessionName..."
    Stop-Application
    Start-Sleep -Seconds 2
    Start-Application
}

function Show-Status {
    Write-Host ""
    Write-Host "Clipboard-Sync Status" -ForegroundColor Cyan
    Write-Host "=====================" -ForegroundColor Cyan

    $processId = Get-ProcessId
    $proc = Get-RunningProcess

    if ($proc) {
        Write-Success "Application is RUNNING"
        Write-Host ""
        Write-Host "Process Details:" -ForegroundColor Yellow
        Write-Host "  PID:        $($proc.Id)"
        Write-Host "  Name:       $($proc.ProcessName)"
        Write-Host "  Start Time: $($proc.StartTime)"
        Write-Host "  CPU Time:   $($proc.TotalProcessorTime)"
        Write-Host "  Memory:     $([math]::Round($proc.WorkingSet64 / 1MB, 2)) MB"
    }
    else {
        Write-Warn "Application is NOT RUNNING"
        if ($processId) {
            Write-Info "Stale PID file found (PID: $processId). Cleaning up..."
            Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Host ""
    Write-Host "Log File:" -ForegroundColor Yellow
    if (Test-Path $LogFile) {
        $logInfo = Get-Item $LogFile
        Write-Host "  Path:     $LogFile"
        Write-Host "  Size:     $([math]::Round($logInfo.Length / 1KB, 2)) KB"
        Write-Host "  Modified: $($logInfo.LastWriteTime)"
    }
    else {
        Write-Host "  No log file found"
    }
    Write-Host ""
}

function Show-Attach {
    $proc = Get-RunningProcess

    if (-not $proc) {
        Write-Warn "Application is not running."
        Write-Info "Use '.\run.ps1 start' to start the application."
        return
    }

    Write-Host ""
    Write-Host "Attached to $SessionName" -ForegroundColor Cyan
    Write-Host "========================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Process Information:" -ForegroundColor Yellow
    Write-Host "  PID:         $($proc.Id)"
    Write-Host "  Name:        $($proc.ProcessName)"
    Write-Host "  Start Time:  $($proc.StartTime)"
    Write-Host "  CPU Time:    $($proc.TotalProcessorTime)"
    Write-Host "  Memory:      $([math]::Round($proc.WorkingSet64 / 1MB, 2)) MB"
    Write-Host "  Threads:     $($proc.Threads.Count)"
    Write-Host ""
    Write-Host "Note: Windows background processes don't have interactive terminals." -ForegroundColor Gray
    Write-Host "Use '.\run.ps1 tail' to view live output." -ForegroundColor Gray
    Write-Host ""
}

function Show-Logs {
    if (-not (Test-Path $LogFile)) {
        Write-Warn "Log file not found: $LogFile"
        Write-Info "The application may not have been started yet."
        return
    }

    Write-Info "Displaying log file: $LogFile"
    Write-Host "========================================" -ForegroundColor Gray
    Get-Content $LogFile
    Write-Host "========================================" -ForegroundColor Gray
    Write-Info "End of log file"
}

function Show-Tail {
    if (-not (Test-Path $LogFile)) {
        Write-Warn "Log file not found: $LogFile"
        Write-Info "The application may not have been started yet."
        Write-Info "Waiting for log file to be created..."

        # Wait for log file to appear
        $timeout = 30
        $waited = 0
        while (-not (Test-Path $LogFile) -and $waited -lt $timeout) {
            Start-Sleep -Seconds 1
            $waited++
        }

        if (-not (Test-Path $LogFile)) {
            Write-Error "Log file did not appear after $timeout seconds."
            return
        }
    }

    Write-Info "Following log file: $LogFile"
    Write-Info "Press Ctrl+C to stop..."
    Write-Host "========================================" -ForegroundColor Gray

    Get-Content $LogFile -Wait -Tail 50
}

function Start-Pairing {
    # Verify virtual environment exists
    $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    if (-not (Test-Path $activateScript)) {
        Write-Error "Virtual environment not found at: $VenvPath"
        return
    }

    Write-Info "Starting pairing mode..."

    # Run pairing in foreground (interactive)
    $pairingScript = @"
Set-Location '$ScriptRoot'
& '$activateScript'
python -m main pair
"@

    powershell -NoProfile -ExecutionPolicy Bypass -Command $pairingScript
}

function Join-Device {
    param($Host, $Pin)

    if (-not $Host) {
        Write-Error "Host IP is required. Usage: .\run.ps1 join <IP> <PIN>"
        return
    }

    if (-not $Pin) {
        Write-Error "PIN is required. Usage: .\run.ps1 join <IP> <PIN>"
        return
    }

    # Verify virtual environment exists
    $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    if (-not (Test-Path $activateScript)) {
        Write-Error "Virtual environment not found at: $VenvPath"
        return
    }

    Write-Info "Connecting to $Host..."

    # Run join in foreground (interactive)
    $joinScript = @"
Set-Location '$ScriptRoot'
& '$activateScript'
python -m main join $Host $Pin
"@

    powershell -NoProfile -ExecutionPolicy Bypass -Command $joinScript
}

function Remove-Pairing {
    # Verify virtual environment exists
    $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    if (-not (Test-Path $activateScript)) {
        Write-Error "Virtual environment not found at: $VenvPath"
        return
    }

    # Run unpair in foreground (interactive - needs confirmation)
    $unpairScript = @"
Set-Location '$ScriptRoot'
& '$activateScript'
python -m main unpair
"@

    powershell -NoProfile -ExecutionPolicy Bypass -Command $unpairScript
}

function Show-Security {
    # Verify virtual environment exists
    $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    if (-not (Test-Path $activateScript)) {
        Write-Error "Virtual environment not found at: $VenvPath"
        return
    }

    # Run status in foreground
    $statusScript = @"
Set-Location '$ScriptRoot'
& '$activateScript'
python -m main status
"@

    powershell -NoProfile -ExecutionPolicy Bypass -Command $statusScript
}

function Show-Config {
    param($SetKey, $SetValue)

    # Verify virtual environment exists
    $activateScript = Join-Path $VenvPath "Scripts\Activate.ps1"
    if (-not (Test-Path $activateScript)) {
        Write-Error "Virtual environment not found at: $VenvPath"
        return
    }

    # Build command based on arguments
    if ($SetKey -eq "--reset") {
        $configCmd = "python -m main config --reset"
    }
    elseif ($SetKey -eq "--set" -and $SetValue) {
        # Need to get the actual key and value from Arg2 (which would be the key)
        # This is a bit awkward with positional params
        $configCmd = "python -m main config --set $SetValue $Arg2"
    }
    elseif ($SetKey -and $SetKey -ne "--set" -and $SetKey -ne "--reset") {
        # Assume it's --set KEY VALUE format
        $configCmd = "python -m main config --set $SetKey $SetValue"
    }
    else {
        $configCmd = "python -m main config"
    }

    $configScript = @"
Set-Location '$ScriptRoot'
& '$activateScript'
$configCmd
"@

    powershell -NoProfile -ExecutionPolicy Bypass -Command $configScript
}

# Main execution
switch ($Command) {
    "start"    { Start-Application -PeerIP $Peer -VerboseMode:$Verbose -NoSecurityMode:$NoSecurity }
    "stop"     { Stop-Application }
    "restart"  { Restart-Application }
    "attach"   { Show-Attach }
    "logs"     { Show-Logs }
    "tail"     { Show-Tail }
    "status"   { Show-Status }
    "pair"     { Start-Pairing }
    "join"     { Join-Device -Host $Arg1 -Pin $Arg2 }
    "unpair"   { Remove-Pairing }
    "security" { Show-Security }
    "config"   { Show-Config -SetKey $Arg1 -SetValue $Arg2 }
    "help"     { Show-Help }
    default    { Start-Application -PeerIP $Peer -VerboseMode:$Verbose -NoSecurityMode:$NoSecurity }
}
