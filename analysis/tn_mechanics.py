# -*- coding: utf-8 -*-
"""回应用户质疑:
1. gate+T+n 逐笔: 达标 vs 强制止损的拆分 (证明下跌被计入, 给最惨案例)
2. 组合级回测修正: 加佣金 + 每晚资金利用率 (回应'周二干瞪眼')
"""
import pandas as pd
import yfinance as yf
from collections import defaultdict

DATA = r"C:\CCWork\ib-trading\data"
PROFIT = 1.015
CAP = 20000.0

buys = pd.read_csv(f"{DATA}\\buys.csv")
buys = buys[(buys["submitted"] == 1) & (buys["shares"] > 0)].copy()
prices = pd.read_csv(f"{DATA}\\prices.csv")
prices["Date"] = pd.to_datetime(prices["Date"], format="ISO8601").dt.date.astype(str)
px = {}
tdates = defaultdict(list)
for r in prices.sort_values("Date").itertuples(index=False):
    px[(r.ticker, r.Date)] = r
    tdates[r.ticker].append(r.Date)

mkt = yf.download(["SPY", "^VIX"], start="2026-01-20", end="2026-07-04", auto_adjust=True, progress=False, group_by="ticker")
spy_ret = {str(k.date()): float(v) for k, v in (mkt["SPY"]["Close"].pct_change() * 100).dropna().items()}
vix_close = {str(k.date()): float(v) for k, v in mkt["^VIX"]["Close"].dropna().items()}


def gate(d):
    v, s = vix_close.get(d), spy_ret.get(d)
    return (v is not None and v >= 19) or (s is not None and s <= -0.5)


def fee(q):
    return max(1.0, 0.005 * q)


# ===== 1. 逐笔: 达标 vs 强制止损 =====
def sim(b, ts_n):
    dts = tdates[b.ticker]
    if b.us_date not in dts:
        return None
    i0 = dts.index(b.us_date)
    entry = float(px[(b.ticker, b.us_date)].Close)
    for k, d in enumerate(dts[i0 + 1:], start=1):
        row = px[(b.ticker, d)]
        o, h, c = float(row.Open), float(row.High), float(row.Close)
        target = round(entry * PROFIT, 2)
        if o >= target:
            return dict(hit=True, xp=o, xd=d, k=k)
        if h >= target:
            return dict(hit=True, xp=target, xd=d, k=k)
        if k >= ts_n:
            return dict(hit=False, xp=c, xd=d, k=k)
    return None


print("========== 1. gate + T+n 逐笔拆分 (亏损明细) ==========")
for n in [1, 2, 3]:
    recs = []
    for b in buys.itertuples(index=False):
        if not gate(b.us_date):
            continue
        r = sim(b, n)
        if not r:
            continue
        entry = float(px[(b.ticker, b.us_date)].Close)
        pnl = (r["xp"] - entry) * b.shares - 2 * fee(b.shares)
        recs.append(dict(ticker=b.ticker, entry_d=b.us_date, exit_d=r["xd"], hit=r["hit"],
                         ret=(r["xp"] / entry - 1) * 100, pnl=pnl, inv=entry * b.shares))
    df = pd.DataFrame(recs)
    hit = df[df["hit"]]
    forced = df[~df["hit"]]
    f_loss = forced[forced["pnl"] < 0]
    print(f"\nT+{n}: 共 {len(df)} 笔 | 达标 {len(hit)} 笔 (+${hit['pnl'].sum():,.0f})"
          f" | 强制离场 {len(forced)} 笔 (${forced['pnl'].sum():,.0f}, 其中亏损 {len(f_loss)} 笔 ${f_loss['pnl'].sum():,.0f})"
          f" | 净 ${df['pnl'].sum():,.0f}")
    print(f"   强制离场的收益分布: 中位 {forced['ret'].median():+.2f}%, 最惨 {forced['ret'].min():+.2f}%")
    worst = df.loc[df["pnl"].idxmin()]
    print(f"   最惨单笔: {worst['ticker']} {worst['entry_d']}买入 -> {worst['exit_d']}强平, {worst['ret']:+.2f}% (${worst['pnl']:,.0f})")

# ===== 2. 组合级修正版 =====
print("\n========== 2. $20k 组合回测 (修正: 含佣金, 记录资金利用率) ==========")
all_days = sorted({d for dts in tdates.values() for d in dts})
di = {d: i for i, d in enumerate(all_days)}
buys_night = defaultdict(list)
for b in buys.itertuples(index=False):
    if gate(b.us_date) and (b.ticker, b.us_date) in px:
        buys_night[b.us_date].append(b)


def portfolio(exit_mode):
    cash, pnl_total, fees_total = CAP, 0.0, 0.0
    positions = []
    utilization = []
    for d in all_days:
        keep = []
        for p in positions:
            row = px.get((p["ticker"], d))
            if row is None:
                keep.append(p)
                continue
            o, h, c = float(row.Open), float(row.High), float(row.Close)
            target = p["cost"] * PROFIT
            xp = None
            if exit_mode == "open_exit":
                xp = o
            else:
                if o >= target:
                    xp = o
                elif h >= target:
                    xp = target
                elif di[d] - p["di"] >= exit_mode:
                    xp = c
            if xp is not None:
                qty = p["dollars"] / p["cost"]
                f = fee(qty)
                proceeds = qty * xp - f
                fees_total += f
                pnl_total += proceeds - p["dollars"]
                cash += proceeds
            else:
                keep.append(p)
        positions = keep
        basket = buys_night.get(d)
        if basket:
            want = sum(b.invest for b in basket)
            budget = min(cash, CAP, want)
            utilization.append(budget / want)
            if budget > 100:
                scale = budget / want
                for b in basket:
                    entry = float(px[(b.ticker, d)].Close)
                    qty = b.invest * scale / entry
                    f = fee(qty)
                    fees_total += f
                    positions.append(dict(ticker=b.ticker, dollars=b.invest * scale, cost=entry, di=di[d]))
                    cash -= b.invest * scale + f
                    pnl_total -= f
    last = all_days[-1]
    open_mark = 0.0
    for p in positions:
        row = px.get((p["ticker"], last))
        c = float(row.Close) if row else p["cost"]
        open_mark += p["dollars"] * (c / p["cost"]) - p["dollars"]
    u = pd.Series(utilization)
    return pnl_total + open_mark, fees_total, u


for name, mode in [("次晨开盘即出(保守近似)", "open_exit"), ("T+1收盘", 1), ("T+2收盘", 2), ("T+3收盘", 3)]:
    pnl, ft, u = portfolio(mode)
    print(f"{name}: 净盈亏 ${pnl:,.0f} ({pnl / CAP * 100:+.1f}%), 佣金 ${ft:,.0f}, "
          f"买入夜均资金利用率 {u.mean() * 100:.0f}%, 满仓买入夜 {(u >= 0.999).sum()}/{len(u)}, 最低利用率 {u.min() * 100:.0f}%")
