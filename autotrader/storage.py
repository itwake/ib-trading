# -*- coding: utf-8 -*-
"""SQLite 台账: lots 生命周期 / 订单 / 事件 / 每日快照。"""
import sqlite3
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
  lot_id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT, entry_date TEXT, qty INTEGER, entry_price REAL,
  target_price REAL, state TEXT,           -- PLANNED/FILLED/OVERNIGHT/PREMARKET/TRAILING/CLOSED/ERROR
  exit_date TEXT, exit_price REAL, exit_how TEXT, pnl REAL,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
  order_id INTEGER, lot_id INTEGER, kind TEXT, symbol TEXT, qty INTEGER,
  limit_price REAL, status TEXT, placed_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS events (
  ts TEXT, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  date TEXT PRIMARY KEY, netliq REAL, cash REAL, gross REAL,
  available REAL, n_pos INTEGER, realized_today REAL
);
CREATE TABLE IF NOT EXISTS nightly_runs (
  date TEXT PRIMARY KEY, gate_pass INTEGER, vix REAL, spy_pct REAL,
  n_planned INTEGER, budget REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS processed_execs (
  exec_id TEXT PRIMARY KEY, ts TEXT
);
CREATE TABLE IF NOT EXISTS control (
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_date TEXT, step TEXT, status TEXT, detail TEXT,
  started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS fills (
  exec_id TEXT PRIMARY KEY,                -- 按 execId 跨会话累积, 规避 reqExecutions 的当日窗口
  ts TEXT, symbol TEXT, side TEXT, qty REAL, price REAL
);
CREATE TABLE IF NOT EXISTS watchlist (
  date TEXT, rank INTEGER, symbol TEXT, sector TEXT, change_pct REAL, bought INTEGER,
  entry_close REAL, next_open REAL, next_high REAL, next_close REAL,
  target_hit INTEGER, shadow_ret_pct REAL,  -- 次日结果由 daily_report 回填
  PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS minute_bars (
  date TEXT, symbol TEXT, bars TEXT,        -- 当日 RTH 1分钟K线 JSON [[HH:MM,o,h,l,c],...]
  PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS sectors (
  symbol TEXT PRIMARY KEY, sector TEXT
);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def event(self, kind, detail):
        self.conn.execute("INSERT INTO events VALUES (?,?,?)", (datetime.now().isoformat(), kind, str(detail)))
        self.conn.commit()

    def add_lot(self, symbol, entry_date, qty, entry_price, target_price, state="FILLED"):
        cur = self.conn.execute(
            "INSERT INTO lots (symbol, entry_date, qty, entry_price, target_price, state, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (symbol, entry_date, qty, entry_price, target_price, state, datetime.now().isoformat()),
        )
        self.conn.commit()
        return cur.lastrowid

    def open_lots(self):
        cur = self.conn.execute(
            "SELECT lot_id, symbol, entry_date, qty, entry_price, target_price, state FROM lots"
            " WHERE state NOT IN ('CLOSED','ERROR')")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_lot_state(self, lot_id, state):
        self.conn.execute("UPDATE lots SET state=? WHERE lot_id=?", (state, lot_id))
        self.conn.commit()

    def close_lot(self, lot_id, exit_date, exit_price, exit_how, pnl):
        self.conn.execute(
            "UPDATE lots SET state='CLOSED', exit_date=?, exit_price=?, exit_how=?, pnl=? WHERE lot_id=?",
            (exit_date, exit_price, exit_how, pnl, lot_id),
        )
        self.conn.commit()

    def record_order(self, order_id, lot_id, kind, symbol, qty, limit_price, status, note=""):
        self.conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)",
            (order_id, lot_id, kind, symbol, qty, limit_price, status, datetime.now().isoformat(), note),
        )
        self.conn.commit()

    def record_run(self, date, gate_pass, vix, spy_pct, n_planned, budget, note=""):
        # UPSERT: 同一晚 gate_check 与 build_plan 各写一次, 后写的 0 值不得覆盖先写的真实值
        self.conn.execute(
            "INSERT INTO nightly_runs VALUES (?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET"
            " gate_pass=excluded.gate_pass, note=excluded.note,"
            " vix=CASE WHEN excluded.vix<>0 THEN excluded.vix ELSE nightly_runs.vix END,"
            " spy_pct=CASE WHEN excluded.spy_pct<>0 THEN excluded.spy_pct ELSE nightly_runs.spy_pct END,"
            " n_planned=CASE WHEN excluded.n_planned<>0 THEN excluded.n_planned ELSE nightly_runs.n_planned END,"
            " budget=CASE WHEN excluded.budget<>0 THEN excluded.budget ELSE nightly_runs.budget END",
            (date, int(gate_pass), vix, spy_pct, n_planned, budget, note),
        )
        self.conn.commit()

    def record_exec(self, run_date, step, status, detail, started_at):
        self.conn.execute(
            "INSERT INTO executions (run_date, step, status, detail, started_at, finished_at)"
            " VALUES (?,?,?,?,?,?)",
            (str(run_date), step, status, detail, started_at,
             datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def get_control(self, key, default=""):
        cur = self.conn.execute("SELECT value FROM control WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    def set_control(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO control VALUES (?,?,?)",
                          (key, str(value), datetime.now().isoformat()))
        self.conn.commit()

    def exec_seen(self, exec_id) -> bool:
        cur = self.conn.execute("SELECT 1 FROM processed_execs WHERE exec_id=?", (exec_id,))
        return cur.fetchone() is not None

    def mark_exec(self, exec_id):
        self.conn.execute("INSERT OR IGNORE INTO processed_execs VALUES (?,?)",
                          (exec_id, datetime.now().isoformat()))
        self.conn.commit()

    def realized_on(self, date) -> float:
        cur = self.conn.execute("SELECT COALESCE(SUM(pnl),0) FROM lots WHERE exit_date=?", (date,))
        return cur.fetchone()[0]

    def lot_count_on(self, entry_date) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM lots WHERE entry_date=?", (entry_date,))
        return cur.fetchone()[0]

    def planned_on(self, date) -> int:
        cur = self.conn.execute("SELECT n_planned FROM nightly_runs WHERE date=?", (date,))
        row = cur.fetchone()
        return int(row[0]) if row and row[0] else 0

    def snapshot(self, date, netliq, cash, gross, available, n_pos, realized_today):
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
            (date, netliq, cash, gross, available, n_pos, realized_today),
        )
        self.conn.commit()

    # ---------- 成交固化 (对账数据源) ----------
    def record_fill(self, exec_id, ts, symbol, side, qty, price):
        self.conn.execute("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?)",
                          (exec_id, ts, symbol, side, qty, price))
        self.conn.commit()

    def recent_sells(self, symbol, need_qty, since_ts=""):
        """该票最近的卖出成交, 从最新往回取到覆盖 need_qty 股为止: 返回 (覆盖股数, 金额)。"""
        cur = self.conn.execute(
            "SELECT qty, price FROM fills WHERE symbol=? AND side='SLD' AND ts>=?"
            " ORDER BY ts DESC, rowid DESC", (symbol, since_ts))
        q = cash = 0.0
        for fq, fp in cur:
            take = min(fq, need_qty - q)
            q += take
            cash += take * fp
            if q >= need_qty:
                break
        return q, cash

    # ---------- 候选追踪 (只观察不买入) ----------
    def add_watch(self, rows):
        """rows: [(date, rank, symbol, sector, change_pct, bought)]"""
        self.conn.executemany(
            "INSERT OR REPLACE INTO watchlist (date, rank, symbol, sector, change_pct, bought)"
            " VALUES (?,?,?,?,?,?)", rows)
        self.conn.commit()

    def watch_pending(self, before_date, limit=300):
        cur = self.conn.execute(
            "SELECT date, symbol FROM watchlist WHERE date<? AND next_close IS NULL"
            " ORDER BY date DESC LIMIT ?", (before_date, limit))
        return [{"date": r[0], "symbol": r[1]} for r in cur.fetchall()]

    def set_watch_outcome(self, date, symbol, entry_close, next_open, next_high, next_close,
                          target_hit, shadow_ret_pct):
        self.conn.execute(
            "UPDATE watchlist SET entry_close=?, next_open=?, next_high=?, next_close=?,"
            " target_hit=?, shadow_ret_pct=? WHERE date=? AND symbol=?",
            (entry_close, next_open, next_high, next_close, target_hit, shadow_ret_pct, date, symbol))
        self.conn.commit()

    def get_sector(self, symbol):
        row = self.conn.execute("SELECT sector FROM sectors WHERE symbol=?", (symbol,)).fetchone()
        return row[0] if row else None

    def set_sector(self, symbol, sector):
        self.conn.execute("INSERT OR REPLACE INTO sectors VALUES (?,?)", (symbol, sector))
        self.conn.commit()

    # ---------- 分钟线 (开盘卖出时机观测) ----------
    def save_minute_bars(self, date, symbol, bars_json):
        self.conn.execute("INSERT OR REPLACE INTO minute_bars VALUES (?,?,?)",
                          (date, symbol, bars_json))
        self.conn.commit()

    def symbols_for_bars(self, date):
        cur = self.conn.execute(
            "SELECT DISTINCT symbol FROM lots WHERE exit_date=? OR state NOT IN ('CLOSED','ERROR')",
            (date,))
        return [r[0] for r in cur.fetchall()]
