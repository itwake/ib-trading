# -*- coding: utf-8 -*-
"""财报暴露复盘: 全年 1298 笔真实交易中, 有多少笔在买入当晚(AMC)或次晨(BMO)撞上财报,
盘后是大涨还是大跌居多, 收益如何。
输出: data/earnings_exposure.csv + 汇总统计
"""
import json
import time

import exchange_calendars as xcals
import pandas as pd
import yfinance as yf

DATA = r"C:\CCWork\ib-trading\data"
cal = xcals.get_calendar("XNYS")

ep = pd.read_csv(f"{DATA}\\flex_episodes.csv")
syms = sorted(ep["symbol"].unique())
print(f"{len(ep)} episodes, {len(syms)} symbols")

# ---------- 1. 抓每只票的历史财报时刻 ----------
edates = {}
fails = 0
t0 = time.time()
for i, s in enumerate(syms):
    try:
        df = yf.Ticker(s).get_earnings_dates(limit=12)
        if df is not None and len(df):
            # index 为带时区的 timestamp; 保留 (date, hour) 判断盘前/盘后
            edates[s] = [(str(ts.date()), ts.hour) for ts in df.index]
    except Exception:
        fails += 1
    if (i + 1) % 50 == 0:
        print(f"  {i + 1}/{len(syms)} ({time.time() - t0:.0f}s, fails={fails})", flush=True)

with open(f"{DATA}\\earnings_dates_cache.json", "w") as f:
    json.dump(edates, f)
print(f"earnings dates: {len(edates)}/{len(syms)} symbols ok")


def next_session(d):
    ts = pd.Timestamp(d)
    try:
        if cal.is_session(ts):
            return str(cal.next_session(ts).date())
        return str(cal.date_to_session(ts, direction="next").date())
    except Exception:
        return None


# ---------- 2. 分类每笔入场 ----------
rows = []
for r in ep.itertuples(index=False):
    ed = edates.get(r.symbol, [])
    if not ed:
        continue
    entry = r.entry_date
    nxt = next_session(entry)
    tag = None
    for d, hour in ed:
        if d == entry and hour >= 12:
            tag = "当晚AMC财报"  # 收盘买入几小时后出财报
            break
        if d == nxt and hour < 12:
            tag = "次晨BMO财报"  # 持仓过夜撞上盘前财报
            break
        if d == entry and hour < 12:
            tag = "当天盘前已出"  # 买的就是财报暴跌本身
    rows.append(dict(symbol=r.symbol, entry_date=entry, pnl=r.pnl, hold_td=r.hold_td,
                     bucket=r.bucket, tag=tag or "无财报暴露"))

df = pd.DataFrame(rows)
df.to_csv(f"{DATA}\\earnings_exposure.csv", index=False)

print("\n========== 分类统计 ==========")
agg = df.groupby("tag").apply(lambda g: pd.Series({
    "笔数": len(g),
    "盈利笔": int((g["pnl"] > 0).sum()),
    "亏损笔": int((g["pnl"] <= 0).sum()),
    "胜率%": round((g["pnl"] > 0).mean() * 100),
    "总盈亏": round(g["pnl"].sum()),
    "平均$/笔": round(g["pnl"].mean(), 1),
    "中位$/笔": round(g["pnl"].median(), 1),
}), include_groups=False)
print(agg.to_string())

exposed = df[df["tag"].isin(["当晚AMC财报", "次晨BMO财报"])]
print(f"\n持仓跨财报的 {len(exposed)} 笔明细 (按盈亏排序):")
print(exposed.sort_values("pnl").to_string(index=False))
