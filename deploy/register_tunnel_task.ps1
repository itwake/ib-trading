# 注册开机自启的隧道计划任务 (以当前用户运行, 需 ssh 密钥免密登录服务器)
$script = Join-Path $PSScriptRoot "windows_tunnel.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "ib-trading-tunnel" -Action $action -Trigger $trigger `
    -Settings $settings -Force
Start-ScheduledTask -TaskName "ib-trading-tunnel"
Write-Host "隧道任务已注册并启动: ib-trading-tunnel"
