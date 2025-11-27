#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install Clipboard-Sync to run at Windows startup

.DESCRIPTION
    Creates a scheduled task that starts clipboard-sync when the user logs in
#>

$taskName = "ClipboardSync"
$scriptPath = Join-Path $PSScriptRoot "run.ps1"

# Check if script exists
if (-not (Test-Path $scriptPath)) {
    Write-Host "[ERROR] run.ps1 not found at: $scriptPath" -ForegroundColor Red
    exit 1
}

# Remove existing task if present
$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "[INFO] Removing existing scheduled task..." -ForegroundColor Cyan
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

# Create the scheduled task
Write-Host "[INFO] Creating scheduled task: $taskName" -ForegroundColor Cyan

$workingDir = $PSScriptRoot
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`" start" -WorkingDirectory $workingDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 0)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Start Clipboard Sync at login" -Force | Out-Null

# Verify
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "[OK] Scheduled task created successfully!" -ForegroundColor Green
    Write-Host ""
    Write-Host "Clipboard-Sync will now start automatically when you log in." -ForegroundColor White
    Write-Host ""
    Write-Host "To remove: Unregister-ScheduledTask -TaskName '$taskName' -Confirm:`$false" -ForegroundColor Gray
} else {
    Write-Host "[ERROR] Failed to create scheduled task" -ForegroundColor Red
    exit 1
}
