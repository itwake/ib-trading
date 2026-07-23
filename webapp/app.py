# -*- coding: utf-8 -*-
"""观测面板: 读取 autotrader 的 SQLite 台账, 提供 API + 静态页面。
运行: uvicorn app:app --host 0.0.0.0 --port 80  (工作目录 webapp/)
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import FileResponse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "autotrader"))

from broker import probe_handshake  # noqa: E402
from common import load_config, now_et  # noqa: E402
from market_gate import check_gate  # noqa: E402
import calendar_util as cal  # noqa: E402

cfg = load_config()
app = FastAPI(title="ib-trading 观测面板")
_cache = {}


def _sanitize(v):
    """递归清除 NaN/Inf, 保证 JSON 可序列化。"""
    import math
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, dict):
        return {k: _sanitize(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_sanitize(x) for x in v]
    return v


def _kv_set(key, obj):
    import json
    conn = sqlite3.connect(cfg["db_path"])
    conn.execute("CREATE TABLE IF NOT EXISTS kv_cache (key TEXT PRIMARY KEY, json TEXT, updated_at TEXT)")
    conn.execute("INSERT OR REPLACE INTO kv_cache VALUES (?,?,?)",
                 (key, json.dumps(obj, ensure_ascii=False), datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()


def _kv_get(key):
    import json
    try:
        rows = q("SELECT json, updated_at FROM kv_cache WHERE key=?", (key,))
        if rows:
            return json.loads(rows[0]["json"]), rows[0]["updated_at"]
    except Exception:
        pass
    return None, None


def cached(key, ttl, fn):
    """先内存缓存; 计算失败时回退到 SQLite 里最后一次成功的值 (标注 stale)。"""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = _sanitize(fn())
        _cache[key] = (time.time(), val)
        _kv_set(key, val)
        return val
    except Exception as e:
        old, ts = _kv_get(key)
        if old is not None:
            if isinstance(old, dict):
                old = {**old, "stale": True, "as_of": ts}
            return old
        return {"error": str(e), "stale": True}


def q(sql, args=()):
    conn = sqlite3.connect(cfg["db_path"])
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    return rows


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


def _sched_details(schedule, open_lots, snap):
    """给事件表的每一项生成精确明细: 会对哪些票、以什么价、下什么单。"""
    g, sc, b, ex = cfg["gate"], cfg["screener"], cfg["budget"], cfg["exits"]
    limit_lots = [l for l in open_lots if l["state"] in ("FILLED", "OVERNIGHT", "PREMARKET", "TRAILING")]
    lots_line = "、".join(f"{l['symbol']}×{l['qty']} @≥{l['target_price']}" for l in limit_lots) \
        or "（当前无待挂仓位；若今晚有新买入将一并挂单）"
    trail_line = "、".join(f"{l['symbol']}×{l['qty']}" for l in limit_lots) or "（当前无待挂仓位）"
    est = ""
    if snap and snap.get("netliq"):
        v = min(b["nightly_max_usd"], max(0.0, b["gross_max_x_netliq"] * snap["netliq"] - snap["gross"]))
        est = f"，按最新快照估算 ≈ ${v:,.0f}"
    det = {
        "gate_check": f"实时取 VIX/SPY 判定今晚是否开仓（VIX≥{g['vix_min']} 或 SPY≤{g['spy_max_pct']}% 即放行；被拦截则跳过今晚买入，卖出链不受影响）",
        "build_plan": f"若放行：抓 Finviz 跌幅榜（两页），跳过前 {sc['skip_rank']} 名从第 {sc['skip_rank'] + 1} 名起取 {sc['n_stocks']} 只——无价/ETF/超单股配额的跳过并由后位递补；预算 = min(${b['nightly_max_usd']:,}, {b['gross_max_x_netliq']}×净值−现持仓){est}，每只配额固定 = 预算/{sc['n_stocks']}；可用资金低于 ${b['min_available_funds_usd']:,} 则放弃",
        "submit_moc": f"对选出的 ~{sc['n_stocks']} 只各提交 MOC 收盘竞价买单（每只≈预算/{sc['n_stocks']}，按官方收盘价成交，NYSE 截止 15:50 ET）",
        "confirm_fills": f"拉取今日买入成交，逐笔登记 lot，目标价 = 成交均价 ×{1 + ex['overnight_target_pct'] / 100:.3f}",
        "overnight_sells": f"挂 OVERNIGHT 场所限价卖单（防超卖清点后，+{ex['overnight_target_pct']}% 起、市价更高则跟随抬价）：{lots_line}",
        "premarket_sells": f"盘前挂 SMART 限价卖单（outsideRth，同价规则）：{lots_line}",
        "open_trail": f"撤系统限价卖单，改挂 {ex['trail_pct']}% 追踪卖出：{trail_line}",
        "pre_attrib": "预买裁决（LLM）：用买入时点可知的信息对候选归因并给 skip/caution/ok 裁决与风险分。2026-07-22 起赋权：skip 且风险≥否决线由确定性规则剔除并递补（每晚上限3只），其余仅记分，被否决者影子收益照记",
        "midday_reconcile": "午间对账：固化上午成交并回填平仓（上海时区网关的成交查询窗口 12:00 ET 翻页，须赶在此前）",
        "daily_report": "与 IB 对账（回填真实卖出价与盈亏并播报）→ 净值快照 → Discord 日报 → 分钟线存档 → 候选追踪回填",
    }
    for ev in schedule:
        ev["detail"] = det.get(ev["name"], "")


@app.get("/api/overview")
def overview():
    today = now_et().date()
    target = today if cal.is_trading_day(today) else cal.next_trading_day(today)
    schedule = [{"name": n, "ts": ts.strftime("%m-%d %H:%M ET"), "iso": ts.isoformat()}
                for n, ts in cal.todays_schedule(cfg, target)]
    snap = q("SELECT * FROM snapshots ORDER BY date DESC LIMIT 1")
    run = q("SELECT * FROM nightly_runs ORDER BY date DESC LIMIT 1")
    open_lots = q("SELECT * FROM lots WHERE state NOT IN ('CLOSED','ERROR') ORDER BY lot_id DESC")
    _sched_details(schedule, open_lots, snap[0] if snap else None)
    gw = probe_handshake(cfg["ib"]["host"], cfg["ib"]["port"], timeout=8)
    wd = None
    try:
        import json as _json
        row = q("SELECT value FROM control WHERE key='watchdog'")
        if row:
            wd = _json.loads(row[0]["value"])
    except Exception:
        pass
    return {
        "watchdog": wd,
        "now_et": now_et().strftime("%Y-%m-%d %H:%M ET"),
        "mode": cfg["mode"],
        "gate_cfg": cfg["gate"],
        "target_day": str(target),
        "schedule": schedule,
        "gateway_ok": gw,
        "snapshot": snap[0] if snap else None,
        "last_run": run[0] if run else None,
        "open_lots": open_lots,
    }


@app.get("/api/lots")
def lots(limit: int = 200):
    return q("SELECT * FROM lots ORDER BY lot_id DESC LIMIT ?", (limit,))


@app.get("/api/sectors")
def sectors_map():
    """{symbol: 板块} 映射, 供前端给所有含代码的表补板块列。"""
    try:
        return {r["symbol"]: r["sector"] for r in q("SELECT symbol, sector FROM sectors") if r["sector"]}
    except Exception:
        return {}


@app.get("/api/runs")
def runs(limit: int = 90):
    rows = q("SELECT * FROM nightly_runs ORDER BY date DESC LIMIT ?", (limit,))
    try:  # 每晚实际买入标的的板块构成
        secs = q("SELECT l.entry_date d, COALESCE(NULLIF(s.sector,''),'未知') sector, COUNT(*) n"
                 " FROM lots l LEFT JOIN sectors s ON l.symbol=s.symbol GROUP BY l.entry_date, sector")
        by_date = {}
        for r in secs:
            by_date.setdefault(r["d"], []).append((r["sector"], r["n"]))
        for r in rows:
            lst = sorted(by_date.get(r["date"], []), key=lambda x: -x[1])
            r["sectors"] = "、".join(f"{s}×{n}" for s, n in lst) or "—"
    except Exception:
        pass
    return rows


@app.get("/api/snapshots")
def snapshots(limit: int = 250):
    return q("SELECT * FROM snapshots ORDER BY date ASC LIMIT ?", (limit,))


@app.get("/api/events")
def events(limit: int = 200):
    rows = q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
    for r in rows:
        try:  # 服务器本地时间 -> 带时区 ISO, 前端按所选时区显示
            r["iso"] = datetime.fromisoformat(r["ts"]).astimezone().isoformat()
        except Exception:
            r["iso"] = None
    return rows


@app.get("/api/calendar")
def calendar_month(ym: str = ""):
    """月历: 交易日/休市/半日市 + 过去日期叠加闸门判定与已实现盈亏 + 持仓财报日。"""
    import calendar as pycal
    from datetime import date as _date
    today = now_et().date()
    if ym:
        y, m = map(int, ym.split("-"))
    else:
        y, m = today.year, today.month
    ndays = pycal.monthrange(y, m)[1]
    days = []
    for i in range(1, ndays + 1):
        d = _date(y, m, i)
        trading = cal.is_trading_day(d)
        early, close = False, None
        if trading:
            c = cal.market_close_et(d)
            close = c.strftime("%H:%M")
            early = (c.hour, c.minute) < (16, 0)
        days.append({"date": str(d), "dow": d.weekday(), "trading": trading,
                     "early": early, "close": close})
    like = f"{y:04d}-{m:02d}-%"
    runs = {r["date"]: r for r in q("SELECT date, gate_pass, vix, spy_pct, n_planned FROM nightly_runs WHERE date LIKE ?", (like,))}
    realized = {r["d"]: r["pnl"] for r in q(
        "SELECT exit_date d, ROUND(SUM(pnl),0) pnl FROM lots WHERE state='CLOSED' AND exit_date LIKE ? GROUP BY exit_date", (like,))}

    def fetch_earnings():
        import yfinance as yf
        syms = [r["symbol"] for r in q("SELECT DISTINCT symbol FROM lots WHERE state NOT IN ('CLOSED','ERROR')")]
        out = {}
        for s in syms:
            try:
                edates = yf.Ticker(s).get_earnings_dates(limit=4)
                if edates is not None:
                    for ts in edates.index:
                        out.setdefault(str(ts.date()), []).append(s)
            except Exception:
                pass
        return out

    earnings = cached("earnings", 21600, fetch_earnings)
    if not isinstance(earnings, dict) or earnings.get("stale") and "error" in earnings:
        earnings = {}
    return {"ym": f"{y:04d}-{m:02d}", "today": str(today), "first_dow": _date(y, m, 1).weekday(),
            "days": days, "runs": runs, "realized": realized,
            "earnings": {k: v for k, v in earnings.items() if isinstance(v, list) and k.startswith(f"{y:04d}-{m:02d}")}}


@app.get("/api/history")
def history(limit: int = 2000):
    """全年 Flex 审计导入的历史片段 (deploy/import_history.py)。"""
    try:
        rows = q("SELECT * FROM history_episodes ORDER BY entry_date DESC LIMIT ?", (limit,))
    except Exception:
        rows = []
    return rows


@app.get("/api/gate/live")
def gate_live():
    def probe():
        passed, vix, spy, reason = check_gate(cfg)
        if passed is None:
            raise RuntimeError(reason)  # 触发回退到最后一次成功值
        return {"passed": passed, "vix": vix, "spy_pct": spy, "reason": reason}
    return cached("gate", 300, probe)


@app.get("/api/quotes")
def quotes():
    """未平 lot 的延迟报价与浮盈估算 (yfinance, 60s 缓存, 失败回退最后成功值)。"""
    lots = q("SELECT symbol, qty, entry_price, target_price FROM lots WHERE state NOT IN ('CLOSED','ERROR')")
    if not lots:
        return {"quotes": [], "unrealized": 0}

    def build():
        import yfinance as yf
        syms = sorted({l["symbol"] for l in lots})
        data = yf.download(syms, period="5d", interval="1d", progress=False,
                           auto_adjust=False, group_by="ticker", threads=True)
        px = {}
        for s in syms:
            try:
                # group_by="ticker" 对单一 symbol 也返回两层列 (2026-07-15 单持仓日发现),
                # 按实际列层级判断, 不能按 symbol 数量判断
                df = data[s] if data.columns.nlevels > 1 else data
                px[s] = float(df["Close"].dropna().iloc[-1])
            except Exception:
                px[s] = None
        if all(v is None for v in px.values()):
            raise RuntimeError("行情全部获取失败")
        out, unreal = [], 0.0
        for l in lots:
            p = px.get(l["symbol"])
            u = (p - l["entry_price"]) * l["qty"] if p else None
            if u:
                unreal += u
            out.append({**l, "last": round(p, 2) if p else None,
                        "unrealized": round(u, 2) if u is not None else None,
                        "pct": round((p / l["entry_price"] - 1) * 100, 2) if p else None})
        return {"quotes": out, "unrealized": round(unreal, 2)}

    resp = cached("quotes_resp", 60, build)
    # stale 回退只能救短暂故障, 不能把已卖掉的旧持仓当现状展示:
    # 按当前台账过滤缓存行, 只留还在持有的 symbol
    if isinstance(resp, dict) and resp.get("stale"):
        open_syms = {l["symbol"] for l in lots}
        rows = [r for r in resp.get("quotes", []) if r.get("symbol") in open_syms]
        resp = {**resp, "quotes": rows,
                "unrealized": round(sum(r.get("unrealized") or 0 for r in rows), 2)}
    return resp


@app.get("/api/lots/summary")
def lots_summary():
    rows = q("SELECT COUNT(*) n, ROUND(COALESCE(SUM(pnl),0),0) pnl,"
             " SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins"
             " FROM lots WHERE state='CLOSED' AND exit_price>0")
    return rows[0] if rows else {}


@app.get("/api/control")
def get_control():
    rows = q("SELECT key, value, updated_at FROM control")
    return {r["key"]: {"value": r["value"], "updated_at": r["updated_at"]} for r in rows}


@app.post("/api/control/pause_buys/{value}")
def set_pause(value: int):
    conn = sqlite3.connect(cfg["db_path"])
    conn.execute("CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute("INSERT OR REPLACE INTO control VALUES ('pause_buys',?,?)",
                 (str(value), datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return {"pause_buys": value}


# ================= 配置管理 =================
EDITABLE = {
    "mode": ("enum", ["dry", "live"]),
    "ib.host": ("str",),
    "ib.port": ("int", 1, 65535),
    "ib.client_id": ("int", 0, 999),
    "gate.enabled": ("bool",),
    "gate.vix_min": ("num", 0, 100),
    "gate.spy_max_pct": ("num", -10, 0),
    "screener.n_stocks": ("int", 1, 20),
    "screener.skip_rank": ("int", 0, 10),
    "screener.lot_size": ("int", 1, 100),
    "screener.watch_n": ("int", 0, 50),
    "budget.nightly_max_usd": ("num", 0, 1000000),
    "budget.gross_max_x_netliq": ("num", 0, 4),
    "budget.min_available_funds_usd": ("num", 0, 1000000),
    "budget.per_stock_max_pct": ("num", 1, 100),
    "exits.overnight_target_pct": ("num", 0.1, 10),
    "exits.trail_pct": ("num", 0.05, 5),
    "exits.follow_market": ("bool",),
    "exits.follow_buffer_pct": ("num", 0.0, 2),
    "schedule_et.overnight_sells_offset_min": ("num", -30, 220),
    "schedule_et.premarket_sells_offset_min": ("num", -30, 300),
    "schedule_et.open_trail_offset_min": ("num", -60, 180),
    "schedule_et.midday_reconcile_offset_min": ("num", 30, 148),
    "pre_judg.apply": ("bool",),
    "pre_judg.veto_risk_min": ("num", 5, 10),
    "pre_judg.veto_max_n": ("int", 0, 10),
    "pre_judg.extra_n": ("int", 15, 25),
    "pre_judg.concurrency": ("int", 1, 6),
    "pre_judg.call_timeout_s": ("num", 45, 200),
    "pre_judg.overall_timeout_s": ("num", 60, 300),
    "mood.vix_flip": ("num", 10, 40),
    "mood.term_flip": ("num", 0.8, 1.2),
    "mood.vvix_flip": ("num", 80, 150),
    "risk.cushion_alert_pct": ("num", 1, 50),
    "notify.heartbeat_minutes": ("int", 0, 1440),
    "notify.discord_webhook": ("str",),
    "flex.token": ("str",),
    "flex.query_id": ("str",),
}


def _coerce(spec, v):
    kind = spec[0]
    if kind == "bool":
        return bool(v) if isinstance(v, bool) else str(v).lower() in ("1", "true", "on")
    if kind == "enum":
        if v not in spec[1]:
            raise ValueError(f"必须是 {spec[1]}")
        return v
    if kind == "str":
        return str(v)
    x = float(v)
    if not (spec[1] <= x <= spec[2]):
        raise ValueError(f"范围 {spec[1]}~{spec[2]}")
    return int(x) if kind == "int" else x


@app.get("/api/config")
def get_config():
    import json as _json
    with open(os.path.join(HERE, "..", "autotrader", "config.json"), encoding="utf-8") as f:
        raw = _json.load(f)
    out = {}
    for path in EDITABLE:
        node = raw
        for k in path.split("."):
            node = node.get(k, None) if isinstance(node, dict) else None
        out[path] = node
    if out.get("notify.discord_webhook"):
        # 面板无鉴权: 只回"已设置", 不回前缀 (前 45 字符会连 webhook id 一起暴露)
        out["notify.discord_webhook"] = "…(已设置)"
    if out.get("flex.token"):
        out["flex.token"] = out["flex.token"][:6] + "…(已设置)"
    # 类型规格随值一起下发: 前端据此渲染控件 (勾选框/下拉/带范围的数字框),
    # 不再靠页面里硬编码的字段表猜类型 —— 新增可配项不会再"进了接口却看不见"。
    specs = {}
    for path, spec in EDITABLE.items():
        s = {"kind": spec[0]}
        if spec[0] == "enum":
            s["choices"] = list(spec[1])
        elif spec[0] in ("num", "int"):
            s["min"], s["max"] = spec[1], spec[2]
        specs[path] = s
    return {"fields": out, "specs": specs}


@app.post("/api/config")
async def set_config(updates: dict):
    import json as _json
    path_cfg = os.path.join(HERE, "..", "autotrader", "config.json")
    with open(path_cfg, encoding="utf-8") as f:
        raw = _json.load(f)
    changed = []
    for path, val in updates.items():
        if path not in EDITABLE:
            return {"ok": False, "error": f"不可配置项: {path}"}
        if path in ("notify.discord_webhook", "flex.token") and "…(已设置)" in str(val):
            continue  # 掩码值原样回传 = 未修改, 绝不能把省略号写进配置
        if val is None or (isinstance(val, str) and val.strip() == ""):
            # 空 = 该项在 config.json 里还没有值且用户也没填 = 不改动。
            # 否则老配置每缺一个键, 保存时就会以 float('') 抛错并**整份拒绝**,
            # 让用户在同一次提交里的其他改动一起丢失。
            continue
        try:
            val = _coerce(EDITABLE[path], val)
        except Exception as e:
            return {"ok": False, "error": f"{path}: {e}"}
        node = raw
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        if node.get(keys[-1]) != val:
            node[keys[-1]] = val
            changed.append(f"{path}={val if 'webhook' not in path else '(已更新)'}")
    if not changed:
        return {"ok": True, "result": "无变更"}
    # 原子写: 守护进程随时可能在读 config.json, 半截文件会让它读到坏 JSON
    tmp = path_cfg + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        _json.dump(raw, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path_cfg)
    cfg.clear()
    cfg.update(load_config())
    conn = sqlite3.connect(cfg["db_path"])
    conn.execute("INSERT INTO events VALUES (?,?,?)",
                 (datetime.now().isoformat(), "config", "; ".join(changed)))
    conn.commit()
    conn.close()
    try:
        from notify import Notifier
        Notifier(cfg).send("[配置] 修改: " + "; ".join(changed), "warn")
    except Exception:
        pass
    ib_changed = any(c.startswith("ib.") for c in changed)
    tail = "。守护进程在执行下一个动作前会重读配置，因此对今天尚未执行的步骤即时生效。"
    if any(c.startswith("schedule_et.") for c in changed):
        tail += "⚠️ 但今日时刻表已在开机时算定：改动执行时点要今天就重排，须点「重启守护进程」。"
    if ib_changed:
        tail = ("。⚠️ IB 连接参数已改：面板的手动操作/报价会立即用新地址；"
                "守护进程会在下一个动作自动用新地址连接，但为确保当前会话切换，建议点「重启守护进程」。")
    return {"ok": True, "result": "已保存: " + "; ".join(changed) + tail}


@app.post("/api/action/restart_daemon")
async def restart_daemon():
    import subprocess
    try:
        subprocess.run(["systemctl", "restart", "autotrader"], check=True, timeout=30)
        return {"ok": True, "result": "守护进程已重启"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ================= 手动操作 (独立于守护进程, clientId+2 直连 IB) =================
import asyncio  # noqa: E402

_action_lock = asyncio.Lock()


def _manual_engine():
    from engine import Engine
    cfg2 = {**cfg, "ib": {**cfg["ib"], "client_id": cfg["ib"]["client_id"] + 2}}
    return Engine(cfg2)


async def _run_manual(name, coro_factory):
    async with _action_lock:
        eng = _manual_engine()
        eng.db.event("manual", name)
        try:
            await eng.broker.connect(retries=1)
            result = await coro_factory(eng)
            eng.notify.send(f"[手动] {name} 已执行 (mode={cfg['mode']})")
            return {"ok": True, "result": str(result) if result is not None else "done"}
        except Exception as e:
            eng.notify.send(f"[手动] {name} 失败: {e}", "warn")
            return {"ok": False, "error": str(e)}
        finally:
            eng.broker.disconnect()


STEPS = {
    "overnight_sells": "挂隔夜限价卖单",
    "premarket_sells": "挂盘前限价卖单",
    "open_trail": "改挂追踪卖单",
    "confirm_fills": "成交确认入账",
    "daily_report": "对账+日报",
    "buy_flow": "闸门+选股+MOC买入",
}


@app.post("/api/action/step/{step}")
async def action_step(step: str):
    if step not in STEPS:
        return {"ok": False, "error": "未知步骤"}
    d = now_et().date()

    async def run(eng):
        if step == "buy_flow":
            passed = await eng.do_gate_check(d)
            if not passed:
                return "闸门拦截, 未买入"
            ok = await eng.do_build_plan(d)
            if not ok:
                return "无可买标的/预算不足"
            # 与定时链同构: 提交前必须先跑裁决, 否则手动测试测不到否决逻辑
            # (2026-07-22: 手动跑了一次买入流程, 十笔单在选股后 0.6 秒内就发出去了)。
            # 截止时刻用交易所 MOC 收单 (收盘前 10 分钟) 而非时刻表里的 submit_moc:
            # 手动是"点完即发", 已过收单时刻则这批单本就排给下一场收盘, 无需抢时间。
            try:  # 非交易日 (周末测试) 取不到收盘时刻: 视为无截止
                cutoff = cal.market_close_et(d) - timedelta(minutes=10)
                cutoff = cutoff if now_et() < cutoff else None
            except Exception:
                cutoff = None
            await eng.do_pre_attrib(d, deadline=cutoff)
            vetoed = getattr(eng, "_pre_vetoed", None) or []
            await eng.do_submit_moc(d)
            tail = ("；裁决否决 " + ", ".join(f"{t}(风险{r:g})" for t, r in vetoed)
                    + " 并已递补" if vetoed else "")
            return f"MOC 已提交 ({len(eng.plan)} 只){tail}"
        return await getattr(eng, "do_" + step)(d)

    return await _run_manual(STEPS[step], run)


@app.post("/api/action/sell_lot/{lot_id}/{how}")
async def action_sell_lot(lot_id: int, how: str):
    lots = q("SELECT * FROM lots WHERE lot_id=?", (lot_id,))
    if not lots:
        return {"ok": False, "error": "lot 不存在"}
    lot = lots[0]

    async def run(eng):
        await eng.broker.cancel_open_sells(lot["symbol"])  # 只撤系统自己的单
        await asyncio.sleep(0.5)
        qty, pos, pending = await eng.broker.sellable(lot["symbol"], lot["qty"])
        if qty <= 0:
            return f"跳过: 持仓 {pos}, 在途卖单 {pending}(含手动) — 防超卖"
        if how == "market":
            return await eng.broker.sell_market(lot["symbol"], qty)
        if how == "trail":
            return await eng.broker.sell_trail(lot["symbol"], qty, cfg["exits"]["trail_pct"])
        return await eng.broker.sell_premarket(lot["symbol"], qty, lot["target_price"])

    label = {"market": "市价卖出", "trail": "追踪卖出", "limit": "目标价限价卖出"}.get(how, how)
    return await _run_manual(f"{label} {lot['symbol']} x{lot['qty']} (lot {lot_id})", run)


@app.post("/api/action/set_target/{lot_id}/{price}")
async def action_set_target(lot_id: int, price: float):
    conn = sqlite3.connect(cfg["db_path"])
    conn.execute("UPDATE lots SET target_price=? WHERE lot_id=?", (price, lot_id))
    conn.execute("INSERT INTO events VALUES (?,?,?)",
                 (datetime.now().isoformat(), "manual", f"改目标价 lot {lot_id} -> {price}"))
    conn.commit()
    conn.close()
    return {"ok": True, "result": f"lot {lot_id} 目标价已改为 {price} (下次挂单生效)"}


@app.post("/api/action/cancel_all_sells")
async def action_cancel_all():
    async def run(eng):
        await eng.broker.cancel_open_sells(None)
        return "已请求撤销全部在途卖单"
    return await _run_manual("撤销全部卖单", run)


@app.post("/api/action/flatten_all")
async def action_flatten():
    async def run(eng):
        pos = await eng.broker.positions()
        for p in pos:
            await eng.broker.cancel_open_sells(p.contract.symbol)  # 只撤系统自己的单
        await asyncio.sleep(0.5)
        n, skipped = 0, []
        for p in pos:
            sym = p.contract.symbol
            qty, held, pending = await eng.broker.sellable(sym, int(p.position))
            if qty <= 0:
                skipped.append(f"{sym}(在途{pending})")
                continue
            await eng.broker.sell_market(sym, qty)
            n += 1
        msg = f"已对 {n} 只持仓提交市价清仓单"
        if skipped:
            msg += f"; 防超卖跳过: {', '.join(skipped)} (有手动在途卖单)"
        return msg
    return await _run_manual("一键清仓 (全部市价卖出)", run)


EV_ACT = {"gate_check": "闸门判定", "build_plan": "选股与预算", "pre_attrib": "预买影子裁决",
          "submit_moc": "提交 MOC 买单", "confirm_fills": "成交入台账",
          "overnight_sells": "挂隔夜限价卖单", "premarket_sells": "挂盘前限价卖单",
          "open_trail": "改挂追踪卖单", "midday_reconcile": "午间对账",
          "daily_report": "对账与日报"}
ORDER_ACT = {"MOC_BUY": "提交 MOC 买单", "OVERNIGHT_SELL": "挂隔夜限价卖单",
             "PREMARKET_SELL": "挂盘前限价卖单", "TRAIL_SELL": "挂追踪卖单"}


@app.get("/api/timeline")
def timeline(limit: int = 400):
    """事件列表: 逐条粒度。future=可预知的每一步(单票一行), past=已发生的每张单/每笔成交。"""
    now = now_et()
    today = now.date()
    target = today if cal.is_trading_day(today) else cal.next_trading_day(today)
    open_lots = q("SELECT * FROM lots WHERE state NOT IN ('CLOSED','ERROR') ORDER BY lot_id")
    limit_lots = [l for l in open_lots if l["state"] in ("FILLED", "OVERNIGHT", "PREMARKET", "TRAILING")]
    snap = q("SELECT * FROM snapshots ORDER BY date DESC LIMIT 1")
    g, sc, b, ex = cfg["gate"], cfg["screener"], cfg["budget"], cfg["exits"]
    est = None
    if snap and snap[0].get("netliq"):
        est = min(b["nightly_max_usd"], max(0.0, b["gross_max_x_netliq"] * snap[0]["netliq"] - snap[0]["gross"]))

    future = []
    for name, ts in cal.todays_schedule(cfg, target):
        if ts.astimezone(now.tzinfo) <= now:
            continue
        iso = ts.isoformat()
        act = EV_ACT.get(name, name)
        if name in ("overnight_sells", "premarket_sells"):
            venue = "隔夜 OVERNIGHT" if name == "overnight_sells" else "盘前 outsideRth"
            if limit_lots:
                for l in limit_lots:
                    future.append(dict(iso=iso, act=act, symbol=l["symbol"], qty=l["qty"],
                                       price=f"≥{l['target_price']}",
                                       note=f"{venue}; 市价更高则跟随抬价; 挂单前防超卖清点"))
            else:
                future.append(dict(iso=iso, act=act, symbol="—", qty=None, price=None,
                                   note="当前无待挂仓位; 今晚若有新买入将逐票挂单"))
        elif name == "open_trail":
            if limit_lots:
                for l in limit_lots:
                    future.append(dict(iso=iso, act=act, symbol=l["symbol"], qty=l["qty"],
                                       price=f"{ex['trail_pct']}% 追踪",
                                       note="先撤系统限价单; 从高点回落即市价成交"))
            else:
                future.append(dict(iso=iso, act=act, symbol="—", qty=None, price=None, note="当前无待挂仓位"))
        elif name == "gate_check":
            future.append(dict(iso=iso, act=act, symbol="市场", qty=None, price=None,
                               note=f"VIX≥{g['vix_min']} 或 SPY≤{g['spy_max_pct']}% 即放行, 否则今晚不买"))
        elif name == "build_plan":
            note = f"Finviz 跌幅榜第 {sc['skip_rank'] + 1} 名起取 {sc['n_stocks']} 只 (无价/ETF/超配额由后位递补)"
            if est is not None:
                note += f"; 预算估算 ${est:,.0f}"
            future.append(dict(iso=iso, act=act, symbol="待选股", qty=sc["n_stocks"], price=None, note=note))
        elif name == "submit_moc":
            per = f"每只 ≈ ${est / sc['n_stocks']:,.0f}" if est else f"每只 ≈ 预算/{sc['n_stocks']}"
            future.append(dict(iso=iso, act=act, symbol="待选股", qty=sc["n_stocks"],
                               price="收盘竞价", note=f"{per}; 具体标的 15:38 选股后可知"))
        else:
            future.append(dict(iso=iso, act=act, symbol="—", qty=None, price=None,
                               note="对账/入账/日报" if name != "confirm_fills" else f"逐笔登记, 目标=成交价×{1 + ex['overnight_target_pct'] / 100:.3f}"))

    past = []
    for o in q("SELECT * FROM orders ORDER BY placed_at DESC LIMIT ?", (limit,)):
        past.append(dict(iso=o["placed_at"], act=ORDER_ACT.get(o["kind"], o["kind"]),
                         symbol=o["symbol"], qty=o["qty"],
                         price=(o["note"] if o["kind"] == "TRAIL_SELL" else (o["limit_price"] or "市价")),
                         status="done", note=o["note"] if o["kind"] != "TRAIL_SELL" else ""))
    for l in q("SELECT * FROM lots ORDER BY lot_id DESC LIMIT 200"):
        past.append(dict(iso=l["created_at"], act="买入成交", symbol=l["symbol"], qty=l["qty"],
                         price=round(l["entry_price"], 2), status="done",
                         note=f"目标价 {l['target_price']}"))
        if l["state"] == "CLOSED" and (l["exit_price"] or 0) > 0:
            pct = (l["exit_price"] / l["entry_price"] - 1) * 100
            past.append(dict(iso=f"{l['exit_date']}T23:59:00", act="卖出成交", symbol=l["symbol"],
                             qty=l["qty"], price=l["exit_price"],
                             status="done", note=f"{pct:+.2f}%, ${l['pnl']:+,.0f} ({l['exit_how']})"))
    for e in q("SELECT * FROM executions ORDER BY id DESC LIMIT 200"):
        if e["step"] in ("gate_check", "daily_report", "build_plan") or e["status"] != "ok":
            st = "done" if e["status"] == "ok" else ("skipped" if e["status"] == "skipped" else "error")
            first = (e["detail"] or "").split("\n")[0][:160]
            if e["step"] == "gate_check":
                st = "gate_pass" if "放行" in first else ("gate_block" if "拦截" in first or "暂停" in first else st)
            past.append(dict(iso=e["started_at"], act=EV_ACT.get(e["step"], e["step"]), symbol="—",
                             qty=None, price=None, status=st, note=first))
    past.sort(key=lambda r: str(r["iso"]), reverse=True)
    return {"future": future, "past": past[:limit], "target_day": str(target)}


@app.get("/api/executions")
def executions(limit: int = 300):
    rows = q("SELECT * FROM executions ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        try:
            r["iso"] = datetime.fromisoformat(r["started_at"]).isoformat()
        except Exception:
            r["iso"] = None
    return rows


@app.get("/api/gate/history")
def gate_history(days: int = 180):
    """VIX 与 SPY 日涨跌的历史序列 + 闸门阈值, 供决策页图表。"""
    def build():
        import yfinance as yf
        mkt = yf.download(["SPY", "^VIX"], period=f"{days + 30}d", interval="1d",
                          progress=False, auto_adjust=True, group_by="ticker")
        vix = {str(k.date()): round(float(v), 2) for k, v in mkt["^VIX"]["Close"].dropna().items()}
        spy_pct_s = (mkt["SPY"]["Close"].dropna().pct_change() * 100).dropna()
        dates = [str(k.date()) for k in spy_pct_s.index][-days:]
        spy_pct = {str(k.date()): round(float(v), 2) for k, v in spy_pct_s.items()}
        g = cfg["gate"]
        return {"dates": dates,
                "vix": [vix.get(d) for d in dates],
                "spy_pct": [spy_pct.get(d) for d in dates],
                "passed": [bool((vix.get(d) or 0) >= g["vix_min"] or (spy_pct.get(d) or 0) <= g["spy_max_pct"]) for d in dates],
                "vix_min": g["vix_min"], "spy_max_pct": g["spy_max_pct"]}
    return cached("gate_hist", 3600, build)


def _pct_rank(series, v):
    """v 在 series 中的百分位 (0-100), 样本不足返回 None。"""
    vals = [x for x in series if x is not None]
    if v is None or len(vals) < 20:
        return None
    return round(100.0 * sum(1 for x in vals if x <= v) / len(vals), 0)


@app.get("/api/mood")
def mood():
    """市场氛围: mood_daily 日度序列 + 自产指标 (候选池采样), 服务端判级, 仅观察。"""
    rows = q("SELECT * FROM mood_daily ORDER BY date")
    out, comp_parts = [], []
    if not rows:
        return {"rows": [], "as_of": None}
    hist = rows[-252:]
    cur = rows[-1]

    def add(key, label, value, state, note, tip):
        out.append({"key": key, "label": label, "value": value, "state": state,
                    "note": note, "tip": tip})

    v = cur.get("vix")
    if v is not None:
        st = ("ok", "黄金区") if v >= 19 else (("warn", "过渡带") if v >= 17 else ("muted", "弱区"))
        add("vix", "VIX", f"{v:.1f}", st[0], st[1],
            "全年审计: VIX≥19 纪律单每万 +$140~194, <19 为负; 阈值 17→22 收益单调上升。绿=对本策略有利。")
        comp_parts.append(_pct_rank([r["vix"] for r in hist], v))
    term = (cur.get("vix") / cur.get("vix3m")) if cur.get("vix") and cur.get("vix3m") else None
    if term is not None:
        st = ("ok", "倒挂·恐慌") if term >= 1.0 else (("warn", "趋紧") if term >= 0.93 else ("muted", "升水·平静"))
        add("term", "VIX/VIX3M 期限结构", f"{term:.3f}", st[0], st[1],
            "Nagel 2012: 反转收益被近端恐慌强预测。>1 (倒挂) = 反转策略最好的环境。")
        comp_parts.append(_pct_rank([(r["vix"] / r["vix3m"]) if r.get("vix") and r.get("vix3m")
                                     else None for r in hist], term))
    vv = cur.get("vvix")
    if vv is not None:
        st = ("warn", "转折预警") if vv >= 110 else ("muted", "平稳")
        add("vvix", "VVIX (波动率的波动率)", f"{vv:.0f}", st[0], st[1],
            "期权市场给'VIX 自身要动'定价: >110 常领先 VIX 上穿, 是黄金区开启的前哨。")
        comp_parts.append(_pct_rank([r["vvix"] for r in hist], vv))
    pc = cur.get("pc_ratio")
    if pc is not None:
        st = ("ok", "看跌拥挤·反指") if pc >= 1.2 else (("err", "自满") if pc <= 0.8 else ("muted", "中性"))
        add("pc", f"Put/Call ({cur.get('pc_src') or '?'})", f"{pc:.2f}", st[0], st[1],
            "经典逆向指标: 看跌拥挤 (高) 往往接近局部底部; 过低 = 自满。cboe=官方总比值, spy_opt=期权链自算兜底。")
        comp_parts.append(_pct_rank([r["pc_ratio"] for r in hist], pc))
    if len(rows) >= 6 and cur.get("hyg_ief") and rows[-6].get("hyg_ief"):
        chg = (cur["hyg_ief"] / rows[-6]["hyg_ief"] - 1) * 100
        st = ("err", "信用趋弱·跳空风险↑") if chg <= -0.5 else (("warn", "走弱") if chg < 0 else ("muted", "平稳"))
        add("credit", "信用比价 HYG/IEF (5日)", f"{chg:+.2f}%", st[0], st[1],
            "信用债相对国债走弱常领先股票持续下跌 — 对隔夜持仓者是跳空风险温度计 (MGY/PEGA 式风险)。")
    if len(rows) >= 6 and cur.get("rsp_spy") and rows[-6].get("rsp_spy"):
        chg = (cur["rsp_spy"] / rows[-6]["rsp_spy"] - 1) * 100
        st = ("err", "中盘承压·伪平静") if chg <= -0.5 else (("warn", "偏弱") if chg < 0 else ("muted", "健康"))
        add("breadth", "广度比价 RSP/SPY (5日)", f"{chg:+.2f}%", st[0], st[1],
            "等权跌、市值指数稳 = 少数大票撑指数而中盘普跌 — 恰是本策略候选池 (cap_midover) 承压的伪装环境。")
    on20 = [r["spy_on_pct"] for r in rows[-20:] if r.get("spy_on_pct") is not None]
    id20 = [r["spy_id_pct"] for r in rows[-20:] if r.get("spy_id_pct") is not None]
    if len(on20) >= 10:
        s_on, s_id = sum(on20), sum(id20)
        st = ("ok", "隔夜溢价在") if s_on > 0 else ("err", "隔夜溢价消失")
        add("onid", f"SPY 隔夜/日内 ({len(on20)}日累计)", f"{s_on:+.1f}% / {s_id:+.1f}%", st[0], st[1],
            "Lou-Polk-Skouras: 反转利润在隔夜。隔夜累计为正 = 本策略的结构性顺风还在。")
    fg = cur.get("fear_greed")
    if fg is not None:
        st = (("ok", "极度恐慌·反转沃土") if fg <= 25 else ("warn", "恐慌") if fg <= 45
              else ("err", "极度贪婪") if fg >= 75 else ("muted", "中性"))
        add("fg", "CNN Fear&Greed", f"{fg:.0f}", st[0], st[1],
            "CNN 七因子合成 (0=极恐 100=极贪)。非官方接口, 断了会自动降级留空。低分对反转策略有利。")
    na = cur.get("naaim")
    if na is not None:
        st = (("ok", "经理人低仓·悲观") if na <= 30 else ("err", "满仓拥挤") if na >= 90 else ("muted", "中性"))
        add("naaim", "NAAIM 经理人仓位", f"{na:.0f}", st[0], st[1],
            "主动经理人平均股票敞口 (周频)。极低 = 专业盘悲观 (逆向偏多); 极高 = 拥挤。")
    # ---- 自产: 候选池即市场情绪采样器 ----
    w = q("SELECT AVG(fomo_score) f, COUNT(fomo_score) nf,"
          " SUM(CASE WHEN pre_verdict='skip' THEN 1 ELSE 0 END)*1.0/NULLIF(COUNT(pre_verdict),0) sk,"
          " SUM(CASE WHEN news_class=1 THEN 1 ELSE 0 END)*1.0/NULLIF(COUNT(news_class),0) h1"
          " FROM watchlist WHERE date=(SELECT MAX(date) FROM watchlist)")
    if w and w[0]["nf"]:
        f = w[0]["f"]
        st = ("err", "过热夜") if f >= 25 else (("warn", "升温") if f >= 15 else ("muted", "常规"))
        add("nfomo", "夜均 FOMO 分", f"{f:.1f}", st[0], st[1],
            "候选池平均 FOMO 分 (0-100)。普跌期夜均 13-22; 07-16 顶部夜 22.1 — 夜均升高 = 跌的是前期热门票, 情绪转折信号。")
    if w and w[0]["sk"] is not None:
        sk = w[0]["sk"] * 100
        st = ("err", "雷区密度高") if sk >= 50 else (("warn", "偏高") if sk >= 30 else ("muted", "正常"))
        add("skip", "裁决 skip 率 (昨夜)", f"{sk:.0f}%", st[0], st[1],
            "LLM 裁决为 skip 的候选占比。财报季常 >60% — Chan 2003: 硬事件驱动的大跌续跌概率高, skip 率高的夜整体胜率低。")
    if w and w[0]["h1"] is not None:
        h1 = w[0]["h1"] * 100
        st = ("err", "硬事件主导") if h1 >= 50 else (("warn", "偏高") if h1 >= 30 else ("muted", "板块/无消息为主"))
        add("hard", "①硬事件占比 (昨夜)", f"{h1:.0f}%", st[0], st[1],
            "归因为个股硬事件的候选占比。高 = 跌幅榜由财报/评级等信息驱动 (最不利的夜型); 低 = 流动性/板块驱动 (反弹更可靠)。")
    comps = [c for c in comp_parts if c is not None]
    composite = round(sum(comps) / len(comps), 0) if comps else None
    return {"rows": out, "as_of": cur["date"], "n_days": len(rows),
            "composite": composite,
            "composite_tip": "恐慌温度 0-100: VIX/期限结构/VVIX/PutCall 各自在近一年中的百分位取均值。"
                             "越高越接近历史级恐慌 — 按全年审计, 高分环境才是本策略的收益密度区。"}


@app.get("/api/gate/shadow_sim")
def gate_shadow_sim():
    """如果闸门开着会怎样: 用每晚影子判定 × 该批次真实盈亏做的实盘对比 (非回测)。"""
    rows = q("SELECT r.date, r.gate_shadow, r.vix, r.spy_pct, b.pnl, b.n FROM nightly_runs r"
             " JOIN (SELECT entry_date, ROUND(SUM(pnl),2) pnl, COUNT(*) n FROM lots"
             "       WHERE state='CLOSED' GROUP BY entry_date) b ON b.entry_date=r.date"
             " WHERE r.gate_shadow IS NOT NULL ORDER BY r.date")
    cum_a = cum_s = avoided = missed = 0.0
    skipped = 0
    series = []
    for r in rows:
        pnl = r["pnl"] or 0.0
        cum_a += pnl
        if r["gate_shadow"]:
            cum_s += pnl
        else:
            skipped += 1
            if pnl < 0:
                avoided += -pnl
            else:
                missed += pnl
        series.append({"date": r["date"], "shadow": int(r["gate_shadow"]), "pnl": pnl,
                       "vix": r["vix"], "cum_actual": round(cum_a, 2), "cum_gated": round(cum_s, 2)})
    return {"series": series, "nights": len(rows), "skipped": skipped,
            "cum_actual": round(cum_a, 2), "cum_gated": round(cum_s, 2),
            "avoided_loss": round(avoided, 2), "missed_gain": round(missed, 2)}


@app.get("/api/weekly")
def weekly_reports(limit: int = 8):
    try:
        return q("SELECT * FROM weekly_reports ORDER BY week DESC LIMIT ?", (int(limit),))
    except Exception:
        return []


@app.get("/api/briefs")
def briefs(kind: str = "weekend", limit: int = 4):
    try:
        return q("SELECT * FROM briefs WHERE kind=? ORDER BY date DESC LIMIT ?",
                 (kind, int(limit)))
    except Exception:
        return []


@app.get("/api/watchlist")
def watchlist(days: int = 60):
    """候选追踪: 每晚跌幅榜前 N 名 (含未买入) 的次日结果, 按名次段/板块聚合。"""
    try:
        rows = q("SELECT * FROM watchlist WHERE date >= date('now', ?) ORDER BY date DESC, rank",
                 (f"-{int(days)} day",))
    except Exception:
        rows = []
    sc = cfg["screener"]
    skip, n = sc.get("skip_rank", 1), sc.get("n_stocks", 10)

    def bucket(r):
        if r["rank"] <= skip:
            return f"1.榜首前{skip}名(跳过)"
        if r["rank"] <= skip + n:
            return f"2.买入区(第{skip + 1}~{skip + n}名)"
        return f"3.观察区(第{skip + n + 1}名起)"

    def agg(key_fn):
        m = {}
        for r in rows:
            if r.get("shadow_ret_pct") is None:
                continue
            k = key_fn(r)
            if k is None:
                continue
            a = m.setdefault(k, {"n": 0, "hit": 0, "ret": 0.0, "bought": 0})
            a["n"] += 1
            a["hit"] += r["target_hit"] or 0
            a["ret"] += r["shadow_ret_pct"]
            a["bought"] += r["bought"] or 0
        return [{"key": k, "n": v["n"], "bought": v["bought"],
                 "hit_rate": round(100 * v["hit"] / v["n"]),
                 "avg_ret_pct": round(v["ret"] / v["n"], 2)}
                for k, v in sorted(m.items())]

    # 观察指标分桶: 每个维度带"假设"说明 (可解释性要求)。方向全部预先声明,
    # 升级为过滤规则的标准: ≥4-8周 + 每桶样本充足 + 高低VIX两环境方向一致。
    def _b(v, edges_labels):
        for edge, label in edges_labels:
            if v < edge:
                return label
        return edges_labels[-1][1]

    TAGS = [
        ("财报关联", "假设: 财报驱动的下跌次日续跌而非反弹 (PEAD 文献 + 全年审计财报单净亏 -$833)。预期: 财报跌命中率显著更低。",
         lambda r: None if r.get("earn_recent") is None else ("财报跌" if r["earn_recent"] else "无财报")),
        ("收盘位置CLV", "假设(反直觉): 收在当日最低附近 (CLV<0.2) = 尾盘抛压式下跌、非信息性, 次日反弹更强 (IBS 从业者共识 + End-of-Day Reversal 2024)。",
         lambda r: None if r.get("clv") is None else
         ("① 收在低位<0.2" if r["clv"] < 0.2 else ("② 中间 0.2~0.5" if r["clv"] < 0.5 else "③ 收复 >0.5"))),
        ("归一化跌幅z", "假设: 跌幅超出自身周均波动越多 (z≥2) 反弹期望越大——但仅限无消息下跌; z<1 只是该股日常噪音。",
         lambda r: None if r.get("zscore") is None else
         ("③ z≥2 异常深" if r["zscore"] >= 2 else ("② z 1~2" if r["zscore"] >= 1 else "① z<1 波动内"))),
        ("趋势位置", "假设: 200日均线上方的回调可买, 均线下方的破位下跌续跌风险高 (George-Hwang 2004 锚定效应 + Connors 从业者回测)。",
         lambda r: None if r.get("vs_200dma") is None else ("200日线上方" if r["vs_200dma"] >= 0 else "200日线下方")),
        ("板块对照", "假设: 随板块同跌 = 系统性抛压、易反弹; 跑输板块≥2pp 的独跌 = 个股自身信息、续跌风险高 (IONS 传染案例的量化版)。",
         lambda r: None if r.get("rel_drop") is None else ("独跌(跑输≥2pp)" if r["rel_drop"] <= -2 else "随板块")),
        ("做空比例", "假设: 高做空 (≥15% float) 的大跌更可能是知情做空, 命中率更低 (Boehmer-Jones-Zhang 2008); 不建议反向博轧空。",
         lambda r: None if r.get("si_pct") is None else ("高做空 ≥15%" if r["si_pct"] >= 15 else "正常 <15%")),
        ("尾盘形态", "假设: 最后30分钟加速下跌 (≤-1%) = 日终价格压力而非信息, 次日反弹更强 (End-of-Day Reversal, SSRN 2024)。",
         lambda r: None if r.get("last30_pct") is None else ("尾盘加速跌 ≤-1%" if r["last30_pct"] <= -1 else "尾盘平稳")),
        ("跳空占比", "假设: 大幅低开 (开盘缺口≤-2%) 多绑定隔夜新闻 = 信息性下跌, 命中率更低; 盘中阴跌更可能是流动性。",
         lambda r: None if r.get("gap_pct") is None else ("跳空低开 ≤-2%" if r["gap_pct"] <= -2 else "盘中跌为主")),
        ("增发/货架", "假设: 近7日提交过增发/货架文件 (424B*/S-1/S-3等, SEC EDGAR) 的下跌被折价定价与 ATM 卖压压制, 反弹受阻 (Bradley-Yuan; 实盘 MDA 案例)。",
         lambda r: None if r.get("dilution") is None else ("近7日有增发文件" if r["dilution"] else "无增发文件")),
        ("当日停牌", "假设: 当日触发停牌/LULD 熔断的深跌股, 价格发现被延迟, 次日沿原方向续跌而非反弹 (Kim-Rhee 1997)。",
         lambda r: None if r.get("halted") is None else ("当日停牌/熔断" if r["halted"] else "未停牌")),
        ("入场星期", "假设: 周五入场需持仓过周末、扛两天新闻空窗 (BE 做空战周末案例), 跳空风险高于周一~四的常规隔夜。",
         lambda r: "周" + "一二三四五"[datetime.strptime(r["date"], "%Y-%m-%d").weekday()]
         if datetime.strptime(r["date"], "%Y-%m-%d").weekday() < 5 else None),
        ("跌幅深度", "回测证据 (2026-07, 自建 7,998 事件研究): 单日 ≤-10% 的极深跌 T+1 期望全场最差 (硬消息浓度高), -7~-10% 次之——温和跌幅段或为期望最好区。观察实盘验证; 样本够后可检验 skip_rank 是否该加大。",
         lambda r: None if r.get("change_pct") is None else
         ("③ 极深 ≤-10%" if r["change_pct"] <= -10 else
          ("② 深 -7~-10%" if r["change_pct"] <= -7 else "① 温和 >-7%"))),
        ("FOMO分", "数值化 FOMO 指数 0-100 (用户设计的加权模型): 动量偏离40% (>50日线60%封顶) + 量能异常30% (当日量4×90日均量封顶) + 长期动量30% (一年+300%封顶), 全 yfinance 口径、历史可回补。回测证据: 过热股大跌命中率最高但左尾最肥、期望最差; FOMO×温和跌隔夜承接最强。",
         lambda r: None if r.get("fomo_score") is None else
         ("③ 过热 ≥60" if r["fomo_score"] >= 60 else
          ("② 升温 30~60" if r["fomo_score"] >= 30 else "① 常规 <30"))),
        ("异动归因", "假设 (Chan 2003, 全链证据最强的分类器): ①个股硬事件的大跌→次日续跌; ③查无消息的大跌→反弹最强; ②板块联动居中。归因由服务器 codex+网络搜索每晚自动完成 (逐票一句话依据存档); 与硬标签(财报/增发/停牌)不一致 = 归因质量红旗。",
         lambda r: {1: "① 硬事件", 2: "② 板块联动", 3: "③ 查无消息"}.get(
             int(r["news_class"])) if r.get("news_class") is not None else None),
        ("二元事件前瞻", "假设: 持仓窗口内有已排期二元事件 (财报/FDA/判决/交割/解禁) 的候选, 隔夜风险不对称 (全年审计财报单净亏 -$833 的推广)。归因时由 codex 顺路检查未来 2 个交易日。",
         lambda r: None if r.get("binevent") is None else ("有已排期事件" if r["binevent"] else "无排期事件")),
        ("LLM预买裁决", "判断记分板 (维度15): 15:39 用买入时点可知的信息, LLM 对候选给出 skip/caution/ok 裁决与 0~10 风险分。2026-07-22 起赋权 (首夜 PEGA 风险10→次晨-13.7% 后用户决定): skip 且风险≥否决线(默认9) 由确定性规则剔除并按原名次递补, 每晚上限3只; 其余裁决继续只记分。被否决者影子收益照记——对照『被否决者影子 vs 递补者实盘』即否决规则的真实贡献。假设: skip 桶次日表现显著更差; prompt 版本 pv 变更后分数重新起算。",
         lambda r: ("🚫 已否决" if r.get("pre_vetoed") else
                    {"skip": "⛔ skip", "caution": "⚠️ caution", "ok": "✓ ok"}.get(
                        str(r.get("pre_verdict") or "").lower())) if r.get("pre_verdict") else None),
    ]
    by_tag = []
    for title, hyp, fn in TAGS:
        buckets = agg(fn)
        if buckets:
            by_tag.append({"dim": title, "hypothesis": hyp, "buckets": buckets})

    return {"rows": rows[:400], "by_bucket": agg(bucket),
            "by_sector": sorted(agg(lambda r: r["sector"] or "未知"),
                                key=lambda x: -x["n"])[:15],
            "by_tag": by_tag}


@app.get("/api/history/summary")
def history_summary():
    try:
        by_bucket = q("SELECT bucket, COUNT(*) n, ROUND(SUM(pnl),0) pnl, ROUND(AVG(pnl),1) avg_pnl,"
                      " ROUND(100.0*SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)/COUNT(*),0) win_rate"
                      " FROM history_episodes GROUP BY bucket")
        by_month = q("SELECT substr(last_date,1,7) ym, ROUND(SUM(pnl),0) pnl FROM history_episodes"
                     " GROUP BY ym ORDER BY ym")
    except Exception:
        by_bucket, by_month = [], []
    return {"by_bucket": by_bucket, "by_month": by_month}
