# -*- coding: utf-8 -*-
"""三个检验:
A. 高开统计: 全部 441 lots 的 D+1 开盘 vs 入场价 (含闸门夜子集)
B. 15分钟级实证 (2026-05-05 后子集, 含盘前): 用户流程 = 盘前限价1.5% -> 开盘后0.3%追踪
   对比 T+1收盘规则。15m 粒度会高估追踪成交价(bar内回撤不可见), 结论偏乐观。
C. 固定资金 $20k 组合级回测: 追踪流 vs T+1/T+2/T+3 收盘, 检验"资金周转损失"论。
"""
import pandas as pd
import yfinance as yf
from collections import defaultdict

DATA = r"C:\CCWork\ib-trading\data"
PROFIT = 1.015
TRAIL = 0.003

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


def next_day(t, d):
    dts = tdates[t]
    if d not in dts:
        return None
    i = dts.index(d)
    return dts[i + 1] if i + 1 < len(dts) else None


# ========== A. 高开统计 ==========
gaps, gaps_gate = [], []
for b in buys.itertuples(index=False):
    d1 = next_day(b.ticker, b.us_date)
    if not d1:
        continue
    entry = float(px[(b.ticker, b.us_date)].Close)
    o = float(px[(b.ticker, d1)].Open)
    g = (o / entry - 1) * 100
    gaps.append(g)
    if gate(b.us_date):
        gaps_gate.append(g)
s = pd.Series(gaps)
sg = pd.Series(gaps_gate)
print("========== A. 次日开盘 vs 入场价 (你的'高开'经验) ==========")
print(f"全部 {len(s)} lots: 高开比例 {(s > 0).mean() * 100:.0f}%, 中位 {s.median():+.2f}%, 均值 {s.mean():+.2f}%, "
      f"开盘即>=+1.5% 的 {(s >= 1.5).mean() * 100:.0f}%")
print(f"闸门夜 {len(sg)} lots: 高开比例 {(sg > 0).mean() * 100:.0f}%, 中位 {sg.median():+.2f}%, 开盘>=+1.5%: {(sg >= 1.5).mean() * 100:.0f}%")
print(f"低开超过-1.5%的比例: 全部 {(s <= -1.5).mean() * 100:.0f}%, 闸门夜 {(sg <= -1.5).mean() * 100:.0f}%")

# ========== B. 15m 实证 ==========
subset = buys[buys["us_date"] >= "2026-05-05"].copy()
tk = sorted(subset["ticker"].unique())
print(f"\n========== B. 15分钟实证 (5/5 后 {len(subset)} lots, {len(tk)} tickers, 含盘前) ==========")
intra = {}
for i in range(0, len(tk), 25):
    chunk = tk[i:i + 25]
    data = yf.download(chunk, start="2026-05-05", end="2026-07-04", interval="15m", prepost=True,
                       auto_adjust=False, group_by="ticker", progress=False, threads=True)
    for t in chunk:
        try:
            df = data[t].dropna(subset=["Close"])
            if len(df):
                intra[t] = df
        except KeyError:
            pass
print(f"拿到 15m 数据: {len(intra)}/{len(tk)} tickers")


def user_flow_15m(t, d1, entry):
    """盘前: 限价 target; RTH: 0.3% 追踪; 收不了 -> 收盘。返回 (exit_px, how)"""
    if t not in intra:
        return None
    df = intra[t]
    day = df[df.index.strftime("%Y-%m-%d") == d1]
    if not len(day):
        return None
    target = round(entry * PROFIT, 2)
    pre = day[day.index.strftime("%H:%M") < "09:30"]
    rth = day[(day.index.strftime("%H:%M") >= "09:30") & (day.index.strftime("%H:%M") < "16:00")]
    if len(pre) and float(pre["High"].max()) >= target:
        return target, "盘前限价"
    if not len(rth):
        return None
    runmax = None
    for bar in rth.itertuples(index=False):
        h, low = float(bar.High), float(bar.Low)
        runmax = h if runmax is None else max(runmax, h)
        stop = runmax * (1 - TRAIL)
        if low <= stop:
            return stop, "追踪触发"
    return float(rth["Close"].iloc[-1]), "收盘"


def fee(q):
    return max(1.0, 0.005 * q)


rows = []
for b in subset.itertuples(index=False):
    d1 = next_day(b.ticker, b.us_date)
    if not d1:
        continue
    entry = float(px[(b.ticker, b.us_date)].Close)
    r = user_flow_15m(b.ticker, d1, entry)
    if r is None:
        continue
    xp, how = r
    drow = px[(b.ticker, d1)]
    o, h, c = float(drow.Open), float(drow.High), float(drow.Close)
    target = round(entry * PROFIT, 2)
    t1_xp = o if o >= target else (target if h >= target else c)
    rows.append(dict(entry_d=b.us_date, ticker=b.ticker, how=how, gated=gate(b.us_date),
                     user_pnl=(xp - entry) * b.shares - 2 * fee(b.shares),
                     t1_pnl=(t1_xp - entry) * b.shares - 2 * fee(b.shares),
                     open_pnl=(o - entry) * b.shares - 2 * fee(b.shares),
                     inv=entry * b.shares,
                     trail_vs_open=(xp / o - 1) * 100))
df = pd.DataFrame(rows)
print(f"可测 lots: {len(df)}")
for tag, g in [("全部", df), ("仅闸门夜", df[df["gated"]])]:
    if not len(g):
        continue
    print(f"\n[{tag}] n={len(g)}")
    print(f"  你的流程(15m上界): net=${g['user_pnl'].sum():,.0f}, 每$10k=${g['user_pnl'].sum() / g['inv'].sum() * 10000:.0f}")
    print(f"  T+1收盘规则:       net=${g['t1_pnl'].sum():,.0f}, 每$10k=${g['t1_pnl'].sum() / g['inv'].sum() * 10000:.0f}")
    print(f"  纯开盘价卖:        net=${g['open_pnl'].sum():,.0f}, 每$10k=${g['open_pnl'].sum() / g['inv'].sum() * 10000:.0f}")
    print(f"  出场方式分布: {g['how'].value_counts().to_dict()}")
    print(f"  追踪价相对开盘价: 中位 {g['trail_vs_open'].median():+.2f}%")

# ========== C. 固定资金 $20k 组合级 ==========
print("\n========== C. 固定资金 $20,000 组合回测 (闸门夜, 全周期) ==========")
CAP = 20000.0
all_days = sorted({d for dts in tdates.values() for d in dts})
buys_night = defaultdict(list)
for b in buys.itertuples(index=False):
    if gate(b.us_date) and (b.ticker, b.us_date) in px:
        buys_night[b.us_date].append(b)


def portfolio(exit_mode):
    """exit_mode: 'trail_open'(次晨开盘价近似) 或 1/2/3 (T+n收盘)"""
    cash, pnl_total = CAP, 0.0
    positions = []  # dict(ticker, qty$, entry, cost, entry_d, deadline_idx)
    skipped_budget = 0
    di = {d: i for i, d in enumerate(all_days)}
    for d in all_days:
        # 先处理离场 (当日资金可用于当晚买入)
        keep = []
        for p in positions:
            row = px.get((p["ticker"], d))
            if row is None:
                keep.append(p)
                continue
            o, h, c = float(row.Open), float(row.High), float(row.Low),
            c = float(row.Close)
            target = p["cost"] * PROFIT
            xp = None
            if exit_mode == "trail_open":
                xp = o if o >= target else o  # 次晨即出(开盘近似)
            else:
                if o >= target:
                    xp = o
                elif float(row.High) >= target:
                    xp = target
                elif di[d] - p["di"] >= exit_mode:
                    xp = c
            if xp is not None:
                proceeds = p["dollars"] * (xp / p["cost"])
                pnl_total += proceeds - p["dollars"]
                cash += proceeds
            else:
                keep.append(p)
        positions = keep
        # 收盘买入
        basket = buys_night.get(d)
        if basket:
            want = sum(b.invest for b in basket)
            budget = min(cash, CAP, want)
            if budget < want:
                skipped_budget += 1
            if budget > 100:
                scale = budget / want
                for b in basket:
                    entry = float(px[(b.ticker, d)].Close)
                    positions.append(dict(ticker=b.ticker, dollars=b.invest * scale, cost=entry, di=di[d]))
                cash -= budget
    # 期末清算
    last = all_days[-1]
    for p in positions:
        row = px.get((p["ticker"], last))
        c = float(row.Close) if row else p["cost"]
        pnl_total += p["dollars"] * (c / p["cost"]) - p["dollars"]
    return pnl_total, skipped_budget


for name, mode in [("次晨开盘即出(你的流程近似)", "trail_open"), ("T+1收盘", 1), ("T+2收盘", 2), ("T+3收盘", 3)]:
    pnl, sk = portfolio(mode)
    print(f"  {name}: 总盈亏 ${pnl:,.0f} (= {pnl / CAP * 100:+.1f}% on $20k, 5个月), 预算受限的夜数 {sk}")
