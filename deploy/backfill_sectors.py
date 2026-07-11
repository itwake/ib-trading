# -*- coding: utf-8 -*-
"""一次性补全 sectors 表: 给 lots / history_episodes / watchlist 里出现过的所有股票
补 GICS 板块 (yfinance)。可反复运行, 只补缺失或空的。守护进程之后会自动维护新标的。

用法 (服务器上):
  cd /opt/ib-trading && .venv/bin/python deploy/backfill_sectors.py
  .venv/bin/python deploy/backfill_sectors.py --limit 50   # 分批, 防 yfinance 限流
"""
import sqlite3
import sys
import time
from os.path import abspath, dirname, join

sys.path.insert(0, join(dirname(dirname(abspath(__file__))), "autotrader"))
from sectors import resolve_sector  # noqa: E402


def main():
    db = join(dirname(dirname(abspath(__file__))), "autotrader", "journal.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS sectors (symbol TEXT PRIMARY KEY, sector TEXT)")
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    syms = set()
    for t in ("lots", "history_episodes", "watchlist"):
        if t in tabs:
            for (s,) in conn.execute(f"SELECT DISTINCT symbol FROM {t} WHERE symbol IS NOT NULL AND symbol<>''"):
                syms.add(s)
    have = {r[0] for r in conn.execute("SELECT symbol FROM sectors WHERE sector IS NOT NULL AND sector<>''")}
    todo = sorted(syms - have)
    if "--limit" in sys.argv:
        todo = todo[:int(sys.argv[sys.argv.index("--limit") + 1])]
    print(f"出现过 {len(syms)} 只, 已有板块 {len(have)}, 本次待补 {len(todo)}")
    n = 0
    for s in todo:
        sec = resolve_sector(s)
        if sec:
            conn.execute("INSERT OR REPLACE INTO sectors VALUES (?,?)", (s, sec))
            conn.commit()
            n += 1
        print(f"  {s:<6} -> {sec or '(未取到, 下次重试)'}")
        time.sleep(0.3)  # 温和限速
    print(f"\n本次补全 {n}/{len(todo)} 只。")


if __name__ == "__main__":
    main()
