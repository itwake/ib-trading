# -*- coding: utf-8 -*-
"""(1) 隔夜即出 + 环境闸门 (VIX>=19 或 SPY<=-0.5%) 回测, 悲观/乐观两个边界
(2) 闸门放行频率: 2018-2026 长历史统计 (每年放行率、连续空窗分布)
"""
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

mkt = yf.download(["SPY", "^VIX"], start="2017-12-01", end="2026-07-04", auto_adjust=True, progress=False, group_by="ticker")
spy_c = mkt["SPY"]["Close"].dropna()
vix_c = mkt["^VIX"]["Close"].dropna()
spy_ret_s = (spy_c.pct_change() * 100).dropna()
spy_ret = {str(k.date()): float(v) for k, v in spy_ret_s.items()}
vix_close = {str(k.date()): float(v) for k, v in vix_c.items()}


def gate(d):
    v, s = vix_close.get(d), spy_ret.get(d)
    return (v is not None and v >= 19) or (s is not None and s <= -0.5)


def fee(q):
    return max(1.0, 0.005 * q)


def sim_t1(b, optimistic):
    """隔夜即出: D+1 若 open>=target 按 open 出; 否则悲观=open 出(追踪0.3%近似), 乐观=日内 high>=target 按 target, 不然 close 出."""
    dts = tdates[b.ticker]
    if b.us_date not in dts:
        return None
    i0 = dts.index(b.us_date)
    if i0 + 1 >= len(dts):
        return None
    entry = float(px[(b.ticker, b.us_date)].Close)
    d1 = dts[i0 + 1]
    row = px[(b.ticker, d1)]
    o, h, c = float(row.Open), float(row.High), float(row.Close)
    target = round(entry * PROFIT, 2)
    if o >= target:
        xp = o
    elif optimistic:
        xp = target if h >= target else c
    else:
        xp = o
    pnl = (xp - entry) * b.shares - 2 * fee(b.shares)
    return dict(ticker=b.ticker, entry=b.us_date, exit=d1, exit_px=xp, entry_px=entry,
                inv=entry * b.shares, pnl=pnl, hit=(xp >= target))


for label, opt in [("悲观边界: 未达标一律次日开盘出", False), ("乐观边界: 次日日内高点可用", True)]:
    for gname, use_gate in [("无闸门", False), ("有闸门", True)]:
        recs = [sim_t1(b, opt) for b in buys.itertuples(index=False) if (not use_gate or gate(b.us_date))]
        df = pd.DataFrame([r for r in recs if r])
        wins = df[df["pnl"] > 0]
        print(f"{label} | {gname}: lots={len(df)}, net=${df['pnl'].sum():,.0f}, "
              f"每$10k=${df['pnl'].sum() / df['inv'].sum() * 10000:.0f}, "
              f"胜率={len(wins) / len(df) * 100:.0f}%, 达标率={df['hit'].mean() * 100:.0f}%")
    print()

# 按夜与按月 (悲观 + 闸门)
recs = [sim_t1(b, False) for b in buys.itertuples(index=False) if gate(b.us_date)]
df = pd.DataFrame([r for r in recs if r])
nightly = df.groupby("entry")["pnl"].sum()
print(f"悲观+闸门 按夜: {len(nightly)} 夜, 盈利 {(nightly > 0).sum()}, 亏损 {(nightly <= 0).sum()}, "
      f"最好 +${nightly.max():.0f}, 最差 -${abs(nightly.min()):.0f}")
df["month"] = df["entry"].str[:7]
print("按月:")
print(df.groupby("month")["pnl"].sum().round(0).to_string())

# ===== 闸门频率长历史 =====
print("\n========== 闸门放行频率 (NYSE 交易日) ==========")
days = [str(k.date()) for k in spy_ret_s.index]
rows = []
for yr in range(2018, 2027):
    yd = [d for d in days if d.startswith(str(yr))]
    passed = [d for d in yd if gate(d)]
    # 连续空窗
    streaks, cur = [], 0
    for d in yd:
        if gate(d):
            if cur:
                streaks.append(cur)
            cur = 0
        else:
            cur += 1
    if cur:
        streaks.append(cur)
    rows.append(dict(year=yr, 交易日=len(yd), 放行=len(passed), 放行率=f"{len(passed) / len(yd) * 100:.0f}%",
                     最长空窗=max(streaks) if streaks else 0,
                     空窗中位=int(pd.Series(streaks).median()) if streaks else 0))
print(pd.DataFrame(rows).set_index("year").to_string())

# 2026 样本期明细
print("\n2026-01-26 ~ 07-02 期间:")
sd = [d for d in days if "2026-01-26" <= d <= "2026-07-02"]
passed = [d for d in sd if gate(d)]
print(f"交易日 {len(sd)}, 放行 {len(passed)} ({len(passed) / len(sd) * 100:.0f}%)")
streaks, cur, cur_start = [], 0, None
for d in sd:
    if gate(d):
        if cur:
            streaks.append((cur_start, cur))
        cur, cur_start = 0, None
    else:
        if cur == 0:
            cur_start = d
        cur += 1
if cur:
    streaks.append((cur_start, cur))
long_gaps = sorted(streaks, key=lambda x: -x[1])[:5]
print("最长的 5 段空窗:", [(s, f"{n}天") for s, n in long_gaps])

# 用户实际 46 夜里闸门放行几夜
user_nights = sorted(buys["us_date"].unique())
un_pass = [d for d in user_nights if gate(d)]
print(f"\n你实际交易的 {len(user_nights)} 夜中, 闸门会放行 {len(un_pass)} 夜")

# 逐月放行天数 2026
print("\n2026 年逐月放行天数:")
s26 = pd.Series({d: gate(d) for d in days if d.startswith("2026") and d <= "2026-07-02"})
s26.index = s26.index.str[:7]
print(s26.groupby(level=0).agg(["sum", "count"]).rename(columns={"sum": "放行", "count": "交易日"}).to_string())
