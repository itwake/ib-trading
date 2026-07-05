# ib-trading

美股"当日大跌股收盘买入、次日反弹卖出"策略的全自动交易与观测系统（IBKR）。

## 策略（由全年真实成交审计定型）

- **入场**：收盘前 Finviz 筛选当日大跌（中大盘、>$15、放量），跌幅榜第 2~11 名，MOC 收盘竞价买入
- **环境闸门**：仅在 VIX≥17 或 SPY 当日≤-0.25% 的晚上开仓（真实成交验证：放行日单笔收益约为拦截日 2.6 倍）
- **出场**：隔夜 OVERNIGHT 场所限价 +1.5% → 盘前 SMART 限价（outsideRth）→ 开盘撤单改 0.3% 追踪卖出，当日必清仓
- **风控**：总持仓 ≤ 2×净值；可用资金下限；全程 Discord 告警

## 组件

| 目录 | 内容 |
|---|---|
| `autotrader/` | 常驻交易守护进程（Python + ib_async + NYSE 官方日历），SQLite 台账 |
| `webapp/` | FastAPI 观测面板：账户/持仓/挂单状态、逐笔历史、每晚闸门决策、事件日志 |
| `analysis/` | 策略研究与回测脚本（日志重演、多变体回测、全年 Flex 成交审计） |
| `deploy/` | Linux systemd 部署 + Windows→服务器 SSH 反向隧道（Gateway 免改信任配置） |

## 部署拓扑

```
Windows(家) ── IB Gateway :4001  (API 设置里把服务器 IP 加入 Trusted IPs)
   ▲  局域网直连 192.168.8.223:4001
Linux 服务器 /opt/ib-trading
   ├─ autotrader.service   (守护进程)
   └─ ibtrading-web.service (http://<server>/ 观测面板)
```

备选：Gateway 不便加白名单时，可用 Windows→服务器的 SSH 反向隧道
（`deploy/windows_tunnel.ps1`，服务器侧 127.0.0.1:4003 → Gateway 4001，来源伪装为本机）。

## 快速开始

```bash
# 服务器 (AlmaLinux 9 / 任意 systemd 发行版)
cd /opt && git clone https://github.com/itwake/ib-trading && cd ib-trading
bash deploy/setup_server.sh          # 建 venv、装依赖、装 systemd 服务
cp autotrader/config.example.json autotrader/config.json   # 填 webhook 等
systemctl start ibtrading-web autotrader
```

Windows 端隧道：`powershell deploy/register_tunnel_task.ps1`（注册开机自启计划任务）。

上线顺序：`mode=dry`（只记日志）→ 纸账户 → live。Gateway 需勾选
API → Precautions → **Bypass Order Precautions for API Orders**（否则隔夜/追踪单被 10329 拦截）。
