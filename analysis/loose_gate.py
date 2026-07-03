# -*- coding: utf-8 -*-
"""放松闸门候选: VIX>=18 或 SPY<=-0.25% —— 放行率与收益对比。"""
import pandas as pd
import yfinance as yf

ep = pd.read_csv(r"C:\CCWork\ib-trading\data\flex_episodes.csv")
mkt = yf.download(["SPY", "^VIX"], start="2017-12-01", end="2026-07-04", auto_adjust=True, progress=False, group_by="ticker")
spy_ret_s = (mkt["SPY"]["Close"].pct_change() * 100).dropna()
vix_s = mkt["^VIX"]["Close"].dropna()
spy_ret = {str(k.date()): float(v) for k, v in spy_ret_s.items()}
vix_close = {str(k.date()): float(v) for k, v in vix_s.items()}


def make_gate(vmin, smax):
    def g(d):
        v, s = vix_close.get(d), spy_ret.get(d)
        return (v is not None and v >= vmin) or (s is not None and s <= smax)
    return g


days = [str(k.date()) for k in spy_ret_s.index]
print("历史放行率对比 (NYSE交易日):")
for name, g in [("严格(19/-0.5)", make_gate(19, -0.5)), ("放松(18/-0.25)", make_gate(18, -0.25)), ("更松(17/-0.25)", make_gate(17, -0.25))]:
    line = []
    for yr in [2019, 2021, 2024, 2025, 2026]:
        yd = [d for d in days if d.startswith(str(yr))]
        line.append(f"{yr}:{sum(g(d) for d in yd) / len(yd) * 100:.0f}%")
    print(f"  {name}: " + "  ".join(line))

a = ep[ep["bucket"] == "A.纪律单(次日内出)"].copy()
a = a[a["entry_date"] >= "2026-01-01"]
a["vix"] = a["entry_date"].map(vix_close)
a["spy"] = a["entry_date"].map(spy_ret)
print("\n2026 纪律单真实盈亏 (901 笔) 在不同闸门下:")
for name, vmin, smax in [("严格(19/-0.5)", 19, -0.5), ("放松(18/-0.25)", 18, -0.25), ("更松(17/-0.25)", 17, -0.25)]:
    m = (a["vix"] >= vmin) | (a["spy"] <= smax)
    g = a[m]
    print(f"  {name}: 放行 {len(g)}/{len(a)} 笔, 保留盈利 ${g['pnl'].sum():,.0f}/{a['pnl'].sum():,.0f}, 平均 ${g['pnl'].mean():.1f}/笔")
