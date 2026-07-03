#!/bin/bash
# 一键更新: git pull + 依赖 + 重启服务
set -e
cd /opt/ib-trading
BEFORE=$(git rev-parse HEAD)
git pull -q
AFTER=$(git rev-parse HEAD)
if git diff --name-only "$BEFORE" "$AFTER" | grep -q requirements.txt; then
  .venv/bin/pip install -q -r requirements.txt
fi
systemctl restart autotrader ibtrading-web
sleep 2
systemctl is-active autotrader ibtrading-web
echo "updated: $BEFORE -> $AFTER"
