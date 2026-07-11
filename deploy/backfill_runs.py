# -*- coding: utf-8 -*-
"""一次性回填历史 nightly_runs 的 vix / spy_pct。

背景: build_plan 的 INSERT OR REPLACE 曾用 0 覆盖 gate_check 写入的真实值 (已在
2f8ce58 修为 UPSERT, 之后的运行不再丢)。本脚本从 executions 表 gate_check 步骤的
detail 文本里解析出当晚真实 VIX/SPY, 回填 vix/spy_pct 仍为 0 的历史行。

用法 (服务器上):
  cd /opt/ib-trading && .venv/bin/python deploy/backfill_runs.py           # 预览
  .venv/bin/python deploy/backfill_runs.py --apply                          # 写入
"""
import re
import sqlite3
import sys
from os.path import abspath, dirname, join

VIX_RE = re.compile(r"VIX=([0-9]+(?:\.[0-9]+)?)")
SPY_RE = re.compile(r"SPY[^=]*=([+-]?[0-9]+(?:\.[0-9]+)?)%")


def main():
    apply = "--apply" in sys.argv
    db = join(dirname(dirname(abspath(__file__))), "autotrader", "journal.db")
    conn = sqlite3.connect(db)
    # 每个交易日取最后一条 ok 的 gate_check (detail 含真实 VIX/SPY)
    rows = conn.execute(
        "SELECT run_date, detail FROM executions WHERE step='gate_check' AND status='ok'"
        " ORDER BY id").fetchall()
    parsed = {}
    for run_date, detail in rows:
        mv, ms = VIX_RE.search(detail or ""), SPY_RE.search(detail or "")
        if mv and ms:
            parsed[run_date] = (float(mv.group(1)), float(ms.group(1)))
    changed = 0
    for d, (vix, spy) in sorted(parsed.items()):
        cur = conn.execute("SELECT vix, spy_pct FROM nightly_runs WHERE date=?", (d,)).fetchone()
        if not cur:
            continue
        if (cur[0] or 0) == 0 or (cur[1] or 0) == 0:
            print(f"  {d}: vix {cur[0]} -> {vix}, spy {cur[1]} -> {spy}")
            if apply:
                conn.execute("UPDATE nightly_runs SET vix=?, spy_pct=? WHERE date=?", (vix, spy, d))
            changed += 1
    if apply:
        conn.commit()
        print(f"\n已写入 {changed} 行。")
    else:
        print(f"\n预览: {changed} 行待回填。加 --apply 执行。")


if __name__ == "__main__":
    main()
