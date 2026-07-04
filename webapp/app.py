# -*- coding: utf-8 -*-
"""观测面板: 读取 autotrader 的 SQLite 台账, 提供 API + 静态页面。
运行: uvicorn app:app --host 0.0.0.0 --port 80  (工作目录 webapp/)
"""
import os
import sqlite3
import sys
import time
from datetime import datetime

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


@app.get("/api/overview")
def overview():
    today = now_et().date()
    target = today if cal.is_trading_day(today) else cal.next_trading_day(today)
    schedule = [{"name": n, "ts": ts.strftime("%m-%d %H:%M ET"), "iso": ts.isoformat()}
                for n, ts in cal.todays_schedule(cfg, target)]
    snap = q("SELECT * FROM snapshots ORDER BY date DESC LIMIT 1")
    run = q("SELECT * FROM nightly_runs ORDER BY date DESC LIMIT 1")
    open_lots = q("SELECT * FROM lots WHERE state NOT IN ('CLOSED','ERROR') ORDER BY lot_id DESC")
    gw = probe_handshake(cfg["ib"]["host"], cfg["ib"]["port"], timeout=8)
    return {
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


@app.get("/api/runs")
def runs(limit: int = 90):
    return q("SELECT * FROM nightly_runs ORDER BY date DESC LIMIT ?", (limit,))


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
                df = data[s] if len(syms) > 1 else data
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

    return cached("quotes_resp", 60, build)


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
    "gate.enabled": ("bool",),
    "gate.vix_min": ("num", 0, 100),
    "gate.spy_max_pct": ("num", -10, 0),
    "screener.n_stocks": ("int", 1, 20),
    "screener.skip_rank": ("int", 0, 10),
    "screener.lot_size": ("int", 1, 100),
    "budget.nightly_max_usd": ("num", 0, 1000000),
    "budget.gross_max_x_netliq": ("num", 0, 4),
    "budget.min_available_funds_usd": ("num", 0, 1000000),
    "budget.per_stock_max_pct": ("num", 1, 100),
    "exits.overnight_target_pct": ("num", 0.1, 10),
    "exits.trail_pct": ("num", 0.05, 5),
    "risk.cushion_alert_pct": ("num", 1, 50),
    "notify.heartbeat_minutes": ("int", 0, 1440),
    "notify.discord_webhook": ("str",),
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
        out["notify.discord_webhook"] = out["notify.discord_webhook"][:45] + "…(已设置)"
    return {"fields": out}


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
        if path == "notify.discord_webhook" and "…" in str(val):
            continue  # 掩码值, 未修改
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
    with open(path_cfg, "w", encoding="utf-8") as f:
        _json.dump(raw, f, ensure_ascii=False, indent=2)
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
    return {"ok": True, "result": "已保存: " + "; ".join(changed) + "。守护进程将在下一循环生效，或点「重启守护进程」立即生效。"}


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
            await eng.do_submit_moc(d)
            return "MOC 已提交"
        return await getattr(eng, "do_" + step)(d)

    return await _run_manual(STEPS[step], run)


@app.post("/api/action/sell_lot/{lot_id}/{how}")
async def action_sell_lot(lot_id: int, how: str):
    lots = q("SELECT * FROM lots WHERE lot_id=?", (lot_id,))
    if not lots:
        return {"ok": False, "error": "lot 不存在"}
    lot = lots[0]

    async def run(eng):
        await eng.broker.cancel_open_sells(lot["symbol"])
        await asyncio.sleep(0.5)
        if how == "market":
            return await eng.broker.sell_market(lot["symbol"], lot["qty"])
        if how == "trail":
            return await eng.broker.sell_trail(lot["symbol"], lot["qty"], cfg["exits"]["trail_pct"])
        return await eng.broker.sell_premarket(lot["symbol"], lot["qty"], lot["target_price"])

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
        n = 0
        for p in pos:
            await eng.broker.cancel_open_sells(p.contract.symbol)
        await asyncio.sleep(0.5)
        for p in pos:
            await eng.broker.sell_market(p.contract.symbol, int(p.position))
            n += 1
        return f"已对 {n} 只持仓提交市价清仓单"
    return await _run_manual("一键清仓 (全部市价卖出)", run)


@app.get("/api/shadow/summary")
def shadow_summary():
    """实盘出场 vs 影子 T+1/T+2 收盘的同批对比。"""
    rows = q("SELECT COUNT(*) n, ROUND(SUM(pnl),0) actual, ROUND(SUM(shadow_t1),0) t1,"
             " ROUND(SUM(shadow_t2),0) t2 FROM lots WHERE shadow_t1 IS NOT NULL")
    detail = q("SELECT lot_id, symbol, entry_date, pnl, shadow_t1, shadow_t2 FROM lots"
               " WHERE shadow_t1 IS NOT NULL ORDER BY lot_id DESC LIMIT 100")
    return {"summary": rows[0] if rows else {}, "detail": detail}


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
