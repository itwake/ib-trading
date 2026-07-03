# -*- coding: utf-8 -*-
"""把全年 Flex 审计的仓位片段 CSV 导入台账 (history_episodes 表), 供面板"全年历史"页展示。
用法: .venv/bin/python deploy/import_history.py <flex_episodes.csv>
"""
import csv
import sqlite3
import sys

sys.path.insert(0, "/opt/ib-trading/autotrader")
from common import load_config  # noqa: E402

cfg = load_config()
conn = sqlite3.connect(cfg["db_path"])
conn.execute("""CREATE TABLE IF NOT EXISTS history_episodes (
  symbol TEXT, entry_date TEXT, last_date TEXT, hold_td REAL,
  bucket TEXT, pnl REAL, era TEXT)""")
conn.execute("DELETE FROM history_episodes")

path = sys.argv[1]
n = 0
with open(path, encoding="utf-8") as f:
    for r in csv.DictReader(f):
        conn.execute("INSERT INTO history_episodes VALUES (?,?,?,?,?,?,?)",
                     (r["symbol"], r["entry_date"], r["last_date"], r["hold_td"],
                      r["bucket"], float(r["pnl"]), r.get("era", "")))
        n += 1
conn.commit()
print(f"imported {n} episodes -> {cfg['db_path']}")
