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
- ~~影子出场实验：每笔平仓自动回填 T+1/T+2 收盘假想盈亏~~（2026-07-11 用户决定停用并移除，见下）
- 运维：deploy/update.sh 一键更新；每日 journal.db 备份（systemd timer，保留 30 份）+ 日志清理

## 2026-07-11 增量（实盘第一周复盘的修复与观测）

实盘第一周（07-06~07-10）暴露三个生产 bug（复盘详情见当周对话记录），本次全部修复：

1. **成交丢失 → 0 价强平**：`reqExecutions` 只返回网关登录时区"当日午夜以来"的成交；
   上海时区网关的午夜=12:00 ET，上午追踪单成交到 16:20 日报时已不可见，
   15 手被 `resync@日报` 以 pnl=0 强平（07-09/07-10 实际约 -$260 被清零）。修复三层：
   - 所有成交按 execId 固化进 `fills` 表（跨会话累积，`_persist_fills`）；
   - 新增 **11:55 ET 午间对账**（`midday_reconcile`，偏移可配）赶在窗口翻页前固化上午成交；
   - resync 绝不写 0 价：优先 fills 表 vwap 回填，查无成交按行情估价（`resync-est`，告警，
     事后用 `deploy/repair_lots.py` + Flex CSV 修正）；持仓快照可疑（竞态）时禁用估价路径。
   - 治标建议：把 Gateway jts.ini `[Logon] TimeZone` 改为 America/New_York（须与上述代码修复同时存在，
     否则盲区只是挪到隔夜时段）。
2. **TRAILING 孤儿仓**：追踪单 tif=DAY 过期后，卖出链三个步骤的状态过滤都不含 TRAILING，
   lot 永远不再有卖单（本周 VERA lot31 即此症 + 成交丢失成幽灵仓）。修复：隔夜/盘前步骤接管
   TRAILING（open_trail 故意不加——盘中重启补执行会撤单重挂、重置追踪高水位）；
   resync 增加同票多 lot 的 FIFO 缺口关闭（幽灵仓自愈）。
3. **nightly_runs 的 vix/spy 全 0**：build_plan 的 INSERT OR REPLACE 整行覆盖 gate_check 写入的
   真实值。修复：record_run 改 UPSERT，0 值不覆盖非 0 旧值。

策略观测变更（用户 2026-07-11 决定）：

- **停用 N+1/N+2 影子出场实验**（shadow.py 删除，面板移除）——用户明确不再考虑该出场策略。
- **新增开盘卖出时机观测**：daily_report 抓取当日持有/平仓标的的 RTH 1 分钟K线存 `minute_bars` 表
  （JSON），供离线复盘"追踪单何时挂、挂多宽"（模拟不同挂单时点 × 回撤比例的出场结果）。
- **新增候选追踪**：每晚把跌幅榜前 `screener.watch_n`（默认 20）名全部登记进 `watchlist` 表
  （含未买入，带 IB 行业分类），次日日报用日线回填结果（次日最高触及 收盘×(1+止盈%) 记命中）；
  面板「每晚决策」页按名次段/板块聚合，用于回答"该买第几名到第几名""哪些板块的大跌不该接"。

## 已知限制 / 待办

- 网关生命周期：接 IBC 实现周级免 2FA 自动重启（现在依赖 Gateway 自带 AutoRestart + Windows 隧道）
- Finviz 页面结构变化会抛异常 → 已有告警，备选方案是 IB Scanner API
- 面板无鉴权，仅限内网使用；如需公网访问先加反代 + Basic Auth
