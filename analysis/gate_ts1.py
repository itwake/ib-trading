# -*- coding: utf-8 -*-
"""闸门 + T+1/T+2 收盘强制离场: '隔夜即出'的改良版 (让均值回归多走一个日内时段)."""
import pandas as pd
import yfinance as yf
from collections import defaultdict

DATA = r"C:\CCWork\ib-trading\data"
PROFIT = 1.015

buys = pd.read_csv(f"{DATA}\\buys.csv")
buys = buys[(buys["submitted"] == 1) & (buys["shares"] > 0)].copy()
prices = pd.read_csv(f"{DATA}\\prices.csv")
prices["Date"] = pd.to_datetime(prices["Date"], format="ISO8601").dt.date.astype(str)
px = {}
tdates = defaultdict(list)
for r in prices.sort_values("Date").itertuples(index=False):
    px[(r.ticker, r.Date)] = r
    tdates[r.ticker].append(r.Date)
sp_rows = prices[(prices["Stock Splits"].notna()) & (prices["Stock Splits"] != 0)]
splits = {(r["ticker"], r["Date"]): float(r["Stock Splits"]) for _, r in sp_rows.iterrows()}

mkt = yf.download(["SPY", "^VIX"], start="2026-01-20", end="2026-07-04", auto_adjust=True, progress=False, group_by="ticker")
spy_ret = {str(k.date()): float(v) for k, v in (mkt["SPY"]["Close"].pct_change() * 100).dropna().items()}
vix_close = {str(k.date()): float(v) for k, v in mkt["^VIX"]["Close"].dropna().items()}


def gate(d):
    v, s = vix_close.get(d), spy_ret.get(d)
    return (v is not None and v >= 19) or (s is not None and s <= -0.5)


def fee(q):
    return max(1.0, 0.005 * q)


def sim(b, ts_n):
    dts = tdates[b.ticker]
    if b.us_date not in dts:
        return None
    i0 = dts.index(b.us_date)
    entry = float(px[(b.ticker, b.us_date)].Close)
    qty, cost = b.shares, entry
    for k, d in enumerate(dts[i0 + 1:], start=1):
        ratio = splits.get((b.ticker, d))
        if ratio and ratio not in (0, 1):
            qty = int(round(qty * ratio))
            cost /= ratio
        row = px[(b.ticker, d)]
        o, h, c = float(row.Open), float(row.High), float(row.Close)
        target = round(cost * PROFIT, 2)
        if o >= target:
            xp, hit = o, True
        elif h >= target:
            xp, hit = target, True
        elif k >= ts_n:
            xp, hit = c, False
        else:
            continue
        return dict(entry=b.us_date, pnl=(xp - cost) * qty - 2 * fee(qty), inv=entry * b.shares, hit=hit, hold=k)
    return None


for ts in [1, 2]:
    recs = [sim(b, ts) for b in buys.itertuples(index=False) if gate(b.us_date)]
    df = pd.DataFrame([r for r in recs if r])
    nightly = df.groupby("entry")["pnl"].sum()
    print(f"闸门 + T+{ts}收盘强制离场: lots={len(df)}, net=${df['pnl'].sum():,.0f}, "
          f"每$10k=${df['pnl'].sum() / df['inv'].sum() * 10000:.0f}, 达标率={df['hit'].mean() * 100:.0f}%, "
          f"盈利夜 {(nightly > 0).sum()}/{len(nightly)}, 最差夜 -${abs(nightly.min()):.0f}")
    if ts == 1:
        df["month"] = df["entry"].str[:7]
        print("  T+1 按月:", {k: round(v) for k, v in df.groupby("month")["pnl"].sum().items()})
