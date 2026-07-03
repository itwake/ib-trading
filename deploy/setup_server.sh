#!/bin/bash
# 服务器初始化: venv + 依赖 + systemd 服务 (AlmaLinux 9 / 任意 systemd 发行版)
set -e
APP=/opt/ib-trading
cd "$APP"

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

cp deploy/autotrader.service /etc/systemd/system/
cp deploy/ibtrading-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable autotrader ibtrading-web

echo "完成。下一步:"
echo "  1) cp autotrader/config.example.json autotrader/config.json 并填写 webhook"
echo "  2) systemctl start ibtrading-web autotrader"
