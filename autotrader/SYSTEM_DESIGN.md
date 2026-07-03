# autotrader 系统设计

目标：把"Finviz 抄底 → MOC 买入 → 隔夜 +1.5% 限价 → 盘前限价 → 开盘 0.3% 追踪"全流程自动化，
零人工操作，全程 Discord 可观测。策略规则以 2025-07~2026-07 全年真实成交审计为依据
（`../flex_audit2.py`、`../gate_on_real.py`）。

## 已锁定的决策（2026-07-03，与用户确认）

| 决策 | 取值 | 依据 |
|---|---|---|
| 全自动 | 无人工步骤；失败必告警 | 用户要求 1 |
| 环境闸门（放松版） | VIX≥17 或 SPY当日≤-0.25%，可配置 | 2026 真实成交：放行 74% 交易夜、保留利润 $3,223/$4,583，优于 18/-0.25 组合 |
| 杠杆 | 允许，上限 gross ≤ 2.0×NetLiq + 可用资金下限 $2,000 | 用户要求 3；下限防 margin call |
| 行业分散 | 默认不限制（sector_max_per_night=0），仅记录观察 | 用户判断"特殊时期才有问题"；日志留数据供复盘 |
| 出场 | 用户原流程：隔夜 OVERNIGHT 限价 +1.5% → 盘前 SMART 限价(outsideRth) → 开盘 TRAIL 0.3% | 全年真实审计：938 笔 +$4,798、胜率 70%，是账户唯一持续盈利引擎 |
| 上线方式 | mode=dry 先跑 → 纸账户(4002) → live | 防御新代码风险 |

## 模块

- `main.py` — CLI：`status` / `plan` / `seed` / `run`
- `engine.py` — 每日状态机（事件表驱动，闸门拦截只停买入链、卖出链永远执行）
- `broker.py` — ib_async 封装；`probe_handshake()` 原始 socket 检测网关挂死（2026-07-03 事故的教训）
- `calendar_util.py` — XNYS 官方日历：假日/半日市/DST 全自动（收盘锚定事件）
- `market_gate.py` — 闸门（yfinance 15 分钟延迟数据足够）
- `screener.py` — Finviz 抓取 + 平均分配（移植自 the-trading 1/2/3 号脚本）
- `storage.py` — SQLite 台账：lots 生命周期 / orders / events / snapshots / nightly_runs
- `notify.py` — Discord Webhook（config 里填 URL；空则只写日志）

## lot 生命周期

FILLED →(20:05 ET 挂隔夜限价)→ OVERNIGHT →(04:05 盘前限价)→ PREMARKET
→(09:31 撤限价改追踪)→ TRAILING →(成交/对账)→ CLOSED
每个阶段前先与 IB 实际持仓对账（`_resync_lots_with_positions`），成交即关 lot。

## 运行

```powershell
cd C:\CCWork\ib-trading\autotrader
..\.venv\Scripts\python.exe main.py status   # 体检: 日历/闸门/网关/账户
..\.venv\Scripts\python.exe main.py seed     # 导入现有持仓为 lot
..\.venv\Scripts\python.exe main.py run      # 常驻 (当前 mode=dry 只记日志不下单)
```

开机自启（确认 dry 跑通后）：任务计划程序 → 登录时启动上述 run 命令。

## 上线前置条件（Phase 0 清单）

1. Gateway 配置 → API → Precautions → 勾选 **Bypass Order Precautions for API Orders**
   （否则 OVERNIGHT/TRAIL 单会被 10329 拦截——2 月日志与 7/2 AEHR 卖单 Cancelled 的原因）
2. `config.json` 填 `notify.discord_webhook`
3. dry 模式完整跑 ≥3 个交易日，核对日志中每个 [DRY] 订单
4. 纸账户（Gateway 登纸账户或第二实例，port 4002）跑 ≥1 周
5. `mode` 改 `live`，首周 `nightly_max_usd` 降到 5000

## 2026-07-04 增量（部署于 192.168.8.237, http://192.168.8.237）

- 台账真实化：平仓回填真实成交价/盈亏并播报；成交确认按 execId 幂等
- 心跳：10 分钟网关/隧道探测（状态变化告警）+ 交易时段整点心跳 + RTH 每 30 分钟保证金缓冲检查（<12% critical）
- 面板 v2：实时闸门、延迟报价与浮盈、净值曲线、下一动作倒计时、lot 筛选、月度盈亏图、**暂停开仓开关**（只停买入链）
- 影子出场实验：每笔平仓自动回填 T+1/T+2 收盘假想盈亏，面板对比三种出场规则的同批实盘结果
- 运维：deploy/update.sh 一键更新；每日 journal.db 备份（systemd timer，保留 30 份）+ 日志清理

## 已知限制 / 待办

- 网关生命周期：接 IBC 实现周级免 2FA 自动重启（现在依赖 Gateway 自带 AutoRestart + Windows 隧道）
- Finviz 页面结构变化会抛异常 → 已有告警，备选方案是 IB Scanner API
- 面板无鉴权，仅限内网使用；如需公网访问先加反代 + Basic Auth
