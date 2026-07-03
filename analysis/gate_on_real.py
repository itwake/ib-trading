# -*- coding: utf-8 -*-
"""把环境闸门 (VIX>=19 或 SPY<=-0.5%) 套在真实成交的片段上, 按入场日分组。"""
import pandas as pd
import yfinance as yf

ep = pd.read_csv(r"C:\CCWork\ib-trading\data\flex_episodes.csv")

mkt = yf.download(["SPY", "^VIX"], start="2025-07-01", end="2026-07-04", auto_adjust=True, progress=False, group_by="ticker")
spy_ret = {str(k.date()): float(v) for k, v in (mkt["SPY"]["Close"].pct_change() * 100).dropna().items()}
vix_close = {str(k.date()): float(v) for k, v in mkt["^VIX"]["Close"].dropna().items()}

ep["vix"] = ep["entry_date"].map(vix_close)
ep["spy"] = ep["entry_date"].map(spy_ret)
ep["gate"] = (ep["vix"] >= 19) | (ep["spy"] <= -0.5)

print("========== 环境闸门 × 真实已实现盈亏 ==========")
for bkt in sorted(ep["bucket"].unique()):
    g = ep[ep["bucket"] == bkt]
    print(f"\n[{bkt}]")
    for flag, gg in g.groupby("gate"):
        tag = "闸门放行" if flag else "闸门拦截"
        print(f"  {tag}: {len(gg)} 片段, 净 ${gg['pnl'].sum():,.0f}, 平均 ${gg['pnl'].mean():.1f}, 胜率 {(gg['pnl'] > 0).mean() * 100:.0f}%")

a = ep[ep["bucket"] == "A.纪律单(次日内出)"].copy()
print("\n纪律单按年代 × 闸门:")
a["era"] = a["entry_date"].apply(lambda d: "2025H2" if d < "2026-01-01" else "2026")
print(a.groupby(["era", "gate"])["pnl"].agg(["count", "sum", "mean"]).round(1).to_string())

print("\n纪律单最惨 8 笔的闸门状态:")
print(a.nsmallest(8, "pnl")[["symbol", "entry_date", "pnl", "vix", "spy", "gate"]].to_string(index=False))

# LEU 等"模拟套牢股"的真实命运
print("\n模拟中被套的股票, 真实命运:")
for s in ["LEU", "LUNR", "RDW", "WHR", "UPWK", "TTD", "BAH", "OPCH"]:
    rows = ep[ep["symbol"] == s]
    for r in rows.itertuples(index=False):
        print(f"  {s}: {r.entry_date} -> {r.last_date} ({int(r.hold_td)}天) {r.bucket} ${r.pnl:,.0f}")
