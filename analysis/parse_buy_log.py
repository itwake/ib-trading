# -*- coding: utf-8 -*-
"""解析 run_tonight_.log (GBK)，提取每日买入计划与实际提交的订单。

输出 buys.csv: run_ts_beijing, us_date, ticker, ref_price, change_pct, shares, invest, submitted
"""
import csv
import re
from datetime import datetime, timedelta

LOG = r"C:\Work\the-trading\run_tonight_.log"
OUT = r"C:\CCWork\ib-trading\data\buys.csv"

with open(LOG, "rb") as f:
    text = f.read().decode("gbk", errors="replace")
lines = text.splitlines()

run_re = re.compile(r"\[.*?(\d{4}/\d{2}/\d{2})\s+(\d{1,2}:\d{2}:\d{2})\.\d+\]\s+RUN .*?(\d)_.*\.py")
# 表格行: "1    FLY     25.02        -14.11      90  2251.80"
plan_re = re.compile(r"^\s*\d+\s+([A-Z][A-Z0-9.\-]*)\s+([\d.]+)\s+(-?[\d.]+)\s+(\d+)\s+([\d.]+)\s*$")
sent_re = re.compile(r"^(?:已发出|�ѷ���)\s*([A-Z][A-Z0-9.\-]*)\s")

runs = []  # list of dicts: ts, plan rows, submitted set
cur = None
cur_script = None

for ln in lines:
    m = run_re.search(ln)
    if m:
        date_s, time_s, script = m.group(1), m.group(2), m.group(3)
        ts = datetime.strptime(f"{date_s} {time_s}", "%Y/%m/%d %H:%M:%S")
        if script == "3":
            cur = {"ts": ts, "plan": [], "submitted": set()}
            runs.append(cur)
        cur_script = script
        continue
    if cur is None:
        continue
    if cur_script == "3":
        pm = plan_re.match(ln)
        if pm:
            cur["plan"].append(
                {
                    "ticker": pm.group(1),
                    "ref_price": float(pm.group(2)),
                    "change_pct": float(pm.group(3)),
                    "shares": int(pm.group(4)),
                    "invest": float(pm.group(5)),
                }
            )
    elif cur_script == "4":
        sm = sent_re.match(ln)
        if sm:
            cur["submitted"].add(sm.group(1))

import os

os.makedirs(os.path.dirname(OUT), exist_ok=True)
n_rows = 0
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["run_ts_beijing", "us_date", "ticker", "ref_price", "change_pct", "shares", "invest", "submitted"])
    for r in runs:
        # 北京时间 -> 美东时间取日期（2026 年 EDT: 3/8 ~ 11/1，偏移 -12h，否则 -13h）
        ts = r["ts"]
        edt = datetime(2026, 3, 8) <= ts < datetime(2026, 11, 2)
        us_date = (ts - timedelta(hours=12 if edt else 13)).date()
        for row in r["plan"]:
            w.writerow(
                [
                    r["ts"].isoformat(sep=" "),
                    us_date.isoformat(),
                    row["ticker"],
                    row["ref_price"],
                    row["change_pct"],
                    row["shares"],
                    row["invest"],
                    1 if row["ticker"] in r["submitted"] else 0,
                ]
            )
            n_rows += 1

print(f"runs: {len(runs)}, rows: {n_rows} -> {OUT}")
# 简单摘要
from collections import Counter

dates = [r["ts"].date().isoformat() for r in runs]
print("first run:", dates[0], "last run:", dates[-1])
dup = [d for d, c in Counter(dates).items() if c > 1]
print("days with multiple runs:", dup if dup else "none")
