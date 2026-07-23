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
CREATE TABLE IF NOT EXISTS briefs (
  kind TEXT, date TEXT, text TEXT,          -- LLM 简报存档 (weekend=周末风险简报)
  PRIMARY KEY (kind, date)
);
CREATE TABLE IF NOT EXISTS mood_daily (
  date TEXT PRIMARY KEY,                    -- 市场氛围日度序列 (2026-07-22 起, 全部免费源, 仅观察)
  vix REAL, vix3m REAL, vvix REAL, skew REAL,
  hyg_ief REAL, rsp_spy REAL,               -- 信用比价 / 等权广度比价
  spy_on_pct REAL, spy_id_pct REAL,         -- SPY 当日隔夜/日内收益分解 (%)
  pc_ratio REAL, pc_src TEXT,               -- Put/Call: cboe=官方总比值, spy_opt=期权链自算
  fear_greed REAL,                          -- CNN Fear&Greed 0-100 (低=恐慌)
  naaim REAL                                -- NAAIM 经理人仓位 (周频, 记录当日快照)
);
"""


class DB:
    # 观察特征列 (2026-07-16 选股质量观测, 全部只记录不影响交易):
    # Finviz 免费层 / 趋势与形态 / 事件与风险标签, 语义见面板悬停说明
    WATCH_FEATURES = (
        "perf_w", "perf_m", "perf_ytd", "perf_y", "vol_w", "relvol", "price", "cap_b",
        "zscore", "gap_pct", "clv", "vs_200dma", "vs_50dma", "vs_52w_high", "last30_pct",
        "rvol90", "fomo_score",  # 当日量/90日均量 (yf口径, 可回补) 与 FOMO 数值分 0-100
        "sector_chg", "rel_drop", "earn_recent", "earn_next", "si_pct",
        "dilution", "halted",
        "news_class", "news_conf", "news_type", "news_reason",  # 异动归因 (codex news-pulse)
        "binevent", "binevent_desc",  # 持仓窗口内已排期二元事件 (归因顺路检查)
        "pre_class", "pre_verdict", "pre_risk", "pre_reason", "pre_pv",  # 预买裁决 (15:39)
        "pre_vetoed",  # 1=被确定性否决规则剔除并递补 (2026-07-22 起赋权, 影子收益照记)
    )

    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        for col in self.WATCH_FEATURES:
            try:
                self.conn.execute(f"ALTER TABLE watchlist ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass
        # gate_shadow: 闸门影子判定 (停用时也记录, 供"如果开着闸门"对比);
        # vix3m/cand_*: 夜间环境观察
        for col in ("vix3m", "cand_n", "cand_avg_drop", "gate_shadow"):
            try:
                self.conn.execute(f"ALTER TABLE nightly_runs ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass
        try:  # fees: Flex 对账回填的真实佣金 (记账时按 -$2/手估, 对账后修正)
            self.conn.execute("ALTER TABLE lots ADD COLUMN fees REAL")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def set_gate_shadow(self, date, val):
        self.conn.execute("UPDATE nightly_runs SET gate_shadow=? WHERE date=?", (val, date))
        self.conn.commit()

    def set_watch_features(self, date, symbol, **cols):
        """按白名单更新候选的观察特征列 (None 值跳过)。"""
        cols = {k: v for k, v in cols.items() if k in self.WATCH_FEATURES and v is not None}
        if not cols:
            return
        sql = "UPDATE watchlist SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE date=? AND symbol=?"
        self.conn.execute(sql, (*cols.values(), date, symbol))
        self.conn.commit()

    MOOD_COLS = ("vix", "vix3m", "vvix", "skew", "hyg_ief", "rsp_spy",
                 "spy_on_pct", "spy_id_pct", "pc_ratio", "pc_src", "fear_greed", "naaim")

    def set_mood(self, date, **cols):
        """UPSERT 当日氛围行 (None 跳过, 不覆盖已有值)。"""
        cols = {k: v for k, v in cols.items() if k in self.MOOD_COLS and v is not None}
        self.conn.execute("INSERT OR IGNORE INTO mood_daily (date) VALUES (?)", (date,))
        if cols:
            sql = "UPDATE mood_daily SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE date=?"
            self.conn.execute(sql, (*cols.values(), date))
        self.conn.commit()

    def get_mood_prev(self, date):
        """date 之前最近一行 (翻转告警的比较基准), 无则 None。"""
        row = self.conn.execute(
            "SELECT * FROM mood_daily WHERE date < ? ORDER BY date DESC LIMIT 1", (date,)).fetchone()
        if not row:
            return None
        keys = [c[0] for c in self.conn.execute("SELECT * FROM mood_daily LIMIT 0").description]
        return dict(zip(keys, row))

    def set_night_env(self, date, vix3m=None, cand_n=None, cand_avg_drop=None):
        self.conn.execute(
            "UPDATE nightly_runs SET vix3m=COALESCE(?,vix3m), cand_n=COALESCE(?,cand_n),"
            " cand_avg_drop=COALESCE(?,cand_avg_drop) WHERE date=?",
            (vix3m, cand_n, cand_avg_drop, date))
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
            "INSERT INTO nightly_runs (date, gate_pass, vix, spy_pct, n_planned, budget, note)"
            " VALUES (?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET"
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
