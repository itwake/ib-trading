# SSH reverse tunnel: server 127.0.0.1:4003 => local IB Gateway 4001
# Gateway sees connections from 127.0.0.1 (via tunnel), no TrustedIPs change needed.
$server = "root@192.168.8.237"
while ($true) {
    & ssh.exe -N -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
        -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new `
        -R 127.0.0.1:4003:127.0.0.1:4001 $server
    Start-Sleep -Seconds 15
}
