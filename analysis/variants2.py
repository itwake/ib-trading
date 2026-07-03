# -*- coding: utf-8 -*-
"""入场过滤变体: 只在大盘下跌日/VIX 抬升时入场, 搭配时间止损。"""
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
spy = mkt["SPY"]["Close"]
vix = mkt["^VIX"]["Close"]
spy_ret = (spy.pct_change() * 100).dropna()
spy_ret = {str(k.date()): float(v) for k, v in spy_ret.items()}
vix_close = {str(k.date()): float(v) for k, v in vix.dropna().items()}


def fee(q):
    return max(1.0, 0.005 * q)


def sim_lot(ticker, entry_date, shares, ts_n):
    dts = tdates[ticker]
    if entry_date not in dts:
        return None
    i0 = dts.index(entry_date)
    entry_px = float(px[(ticker, entry_date)].Close)
    qty, cost = shares, entry_px
    for k, d in enumerate(dts[i0 + 1:], start=1):
        ratio = splits.get((ticker, d))
        if ratio and ratio not in (0, 1):
            qty = int(round(qty * ratio))
            cost /= ratio
        row = px[(ticker, d)]
        o, h, c = float(row.Open), float(row.High), float(row.Close)
        target = round(cost * PROFIT, 2)
        if o >= target:
            return d, o, k, True, qty, cost, entry_px
        if h >= target:
            return d, target, k, True, qty, cost, entry_px
        if ts_n is not None and k >= ts_n:
            return d, c, k, True, qty, cost, entry_px
    last = dts[-1]
    return last, float(px[(ticker, last)].Close), len(dts) - 1 - i0, False, qty, cost, entry_px


def run(name, ts_n=None, vix_min=None, spy_max=None):
    recs = []
    for b in buys.itertuples(index=False):
        v = vix_close.get(b.us_date)
        s = spy_ret.get(b.us_date)
        if vix_min is not None and (v is None or v < vix_min):
            continue
        if spy_max is not None and (s is None or s > spy_max):
            continue
        r = sim_lot(b.ticker, b.us_date, b.shares, ts_n)
        if r is None:
            continue
        d, xp, k, closed, qty, cost, ep = r
        net = (xp - cost) * qty - 2 * fee(qty)
        recs.append(dict(closed=closed, invested=b.shares * ep, pnl=net, hold=k))
    df = pd.DataFrame(recs)
    cl = df[df["closed"]]
    op = df[~df["closed"]]
    return dict(
        variant=name, lots=len(df), invested=round(df["invested"].sum()),
        net=round(df["pnl"].sum()), realized=round(cl["pnl"].sum()), open_pnl=round(op["pnl"].sum()),
        open_n=len(op), losers=int((cl["pnl"] <= 0).sum()),
        net_per_10k=round(df["pnl"].sum() / df["invested"].sum() * 10000),
    )


rows = [
    run("baseline(重算)"),
    run("只在SPY当日<-0.5%买", spy_max=-0.5),
    run("只在SPY当日<-1.0%买", spy_max=-1.0),
    run("只在VIX>=19买", vix_min=19),
    run("只在VIX>=21买", vix_min=21),
    run("ts3", ts_n=3),
    run("ts3+VIX>=19", ts_n=3, vix_min=19),
    run("ts5+VIX>=19", ts_n=5, vix_min=19),
    run("ts3+SPY<-0.5%", ts_n=3, spy_max=-0.5),
    run("ts5+SPY<-0.5%", ts_n=5, spy_max=-0.5),
]
out = pd.DataFrame(rows).set_index("variant")
print(out.to_string())
