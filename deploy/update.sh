#!/bin/bash
# 一键更新: git pull + 依赖 + 重启服务 + 验证重启真实生效
# (2026-07 教训: 曾出现 restart 报 active 但守护进程未换新, 旧代码继续交易两晚)
set -e
cd /opt/ib-trading
BEFORE=$(git rev-parse HEAD)
git pull -q
AFTER=$(git rev-parse HEAD)
git rev-parse --short HEAD > VERSION
if git diff --name-only "$BEFORE" "$AFTER" | grep -q requirements.txt; then
  .venv/bin/pip install -q -r requirements.txt
fi
PID_OLD=$(systemctl show -p MainPID --value autotrader)
systemctl restart autotrader ibtrading-web
sleep 3
systemctl is-active autotrader ibtrading-web
PID_NEW=$(systemctl show -p MainPID --value autotrader)
if [ "$PID_NEW" = "$PID_OLD" ] || [ "$PID_NEW" = "0" ]; then
  echo "❌ autotrader 重启未生效 (PID $PID_OLD -> $PID_NEW), 检查 systemctl status autotrader"
  exit 1
fi
# 孤儿进程检测: systemd 之外不允许有第二个 main.py
N_PROC=$(pgrep -fc "python main.py run" || true)
if [ "$N_PROC" -gt 1 ]; then
  echo "❌ 检测到 $N_PROC 个 main.py 进程 (孤儿进程会占用 clientId 并用旧代码交易!), 请排查: pgrep -af 'main.py run'"
  exit 1
fi
echo "updated: $BEFORE -> $AFTER  (daemon PID $PID_OLD -> $PID_NEW)"
