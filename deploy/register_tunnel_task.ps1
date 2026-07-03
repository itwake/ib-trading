# Register auto-start tunnel task (runs as current user, needs key-based ssh to server)
$script = Join-Path $PSScriptRoot "windows_tunnel.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "ib-trading-tunnel" -Action $action -Trigger $trigger `
    -Settings $settings -Force
Start-ScheduledTask -TaskName "ib-trading-tunnel"
Write-Host "Tunnel task registered and started: ib-trading-tunnel"
