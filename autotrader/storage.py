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
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        for col in ("shadow_t1", "shadow_t2"):
            try:
                self.conn.execute(f"ALTER TABLE lots ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    def lots_needing_shadow(self):
        cur = self.conn.execute(
            "SELECT lot_id, symbol, entry_date, qty, entry_price, target_price FROM lots"
            " WHERE state='CLOSED' AND shadow_t1 IS NULL AND exit_price > 0")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def set_shadow(self, lot_id, t1, t2):
        self.conn.execute("UPDATE lots SET shadow_t1=?, shadow_t2=? WHERE lot_id=?", (t1, t2, lot_id))
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
        self.conn.execute(
            "INSERT OR REPLACE INTO nightly_runs VALUES (?,?,?,?,?,?,?)",
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

    def snapshot(self, date, netliq, cash, gross, available, n_pos, realized_today):
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)",
            (date, netliq, cash, gross, available, n_pos, realized_today),
        )
        self.conn.commit()
