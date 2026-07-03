# Windows -> Linux 服务器的 SSH 反向隧道: 服务器上的 127.0.0.1:4003 => 本机 IB Gateway 4001
# Gateway 看到的连接来源是 127.0.0.1 (经隧道), 无需修改 TrustedIPs。
# 由 register_tunnel_task.ps1 注册为开机自启计划任务。
$server = "root@192.168.8.237"
while ($true) {
    & ssh.exe -N -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
        -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new `
        -R 127.0.0.1:4003:127.0.0.1:4001 $server
    Start-Sleep -Seconds 15
}
