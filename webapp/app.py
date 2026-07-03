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


def cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


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
    schedule = [{"name": n, "ts": ts.strftime("%m-%d %H:%M ET")} for n, ts in cal.todays_schedule(cfg, target)]
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
    return q("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))


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
        return {"passed": passed, "vix": vix, "spy_pct": spy, "reason": reason}
    return cached("gate", 300, probe)


@app.get("/api/quotes")
def quotes():
    """未平 lot 的延迟报价与浮盈估算 (yfinance, 60s 缓存)。"""
    lots = q("SELECT symbol, qty, entry_price, target_price FROM lots WHERE state NOT IN ('CLOSED','ERROR')")
    if not lots:
        return {"quotes": [], "unrealized": 0}

    def fetch():
        import yfinance as yf
        syms = sorted({l["symbol"] for l in lots})
        data = yf.download(syms, period="2d", interval="1d", progress=False,
                           auto_adjust=False, group_by="ticker", threads=True)
        px = {}
        for s in syms:
            try:
                df = data[s] if len(syms) > 1 else data
                px[s] = float(df["Close"].dropna().iloc[-1])
            except Exception:
                px[s] = None
        return px

    px = cached("quotes", 60, fetch)
    out, unreal = [], 0.0
    for l in lots:
        p = px.get(l["symbol"])
        u = (p - l["entry_price"]) * l["qty"] if p else None
        if u:
            unreal += u
        out.append({**l, "last": p, "unrealized": round(u, 2) if u is not None else None,
                    "pct": round((p / l["entry_price"] - 1) * 100, 2) if p else None})
    return {"quotes": out, "unrealized": round(unreal, 2)}


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
