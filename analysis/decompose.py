# -*- coding: utf-8 -*-
import pandas as pd

base = pd.read_csv(r"C:\CCWork\ib-trading\data\lots_baseline.csv")

print("=== 利润分解: 隔夜即出成分 vs 扛单成分 (baseline) ===")
d1 = base[(base["closed"]) & (base["hold"] == 1)]
d2_5 = base[(base["closed"]) & (base["hold"] > 1) & (base["hold"] <= 5)]
d5p = base[(base["closed"]) & (base["hold"] > 5)]
op = base[~base["closed"]]
for name, g in [("D1隔夜即出", d1), ("2-5天脱身", d2_5), (">5天才脱身", d5p), ("未脱身(浮亏)", op)]:
    if len(g):
        print(f"{name}: {len(g)} lots, 投入 ${g['invested'].sum():,.0f}, pnl ${g['pnl'].sum():,.0f}, 每$10k = ${g['pnl'].sum() / g['invested'].sum() * 10000:.0f}")

print("\n=== 各阶段 D1 率与市场环境 ===")


def phase(d):
    if d <= "2026-03-06":
        return "P1"
    if d <= "2026-04-02":
        return "P2"
    return "P3"


base["phase"] = base["entry"].map(phase)
for p, g in base.groupby("phase"):
    c = g[g["closed"]]
    d1r = (c["hold"] == 1).sum() / len(g) * 100
    stuck = (~g["closed"]).sum()
    print(f"{p}: lots={len(g)}, D1率={d1r:.0f}%, 未脱身={stuck}, VIX中位={g['vix'].median():.1f}, SPY当日中位={g['spy'].median():+.2f}%")

print("\n=== 按夜聚合 (baseline) ===")
nightly = base.groupby("entry").agg(pnl=("pnl", "sum"), inv=("invested", "sum"), n=("ticker", "count"))
nightly["per10k"] = (nightly["pnl"] / nightly["inv"] * 10000).round(0)
print(f"共 {len(nightly)} 夜, 盈利夜 {(nightly['pnl'] > 0).sum()}, 亏损夜 {(nightly['pnl'] <= 0).sum()}")
print("\n最差5夜:")
print(nightly.nsmallest(5, "pnl").round(0).to_string())
print("\n最好5夜:")
print(nightly.nlargest(5, "pnl").round(0).to_string())

print("\nVIX 全期间范围:", base["vix"].min(), "-", base["vix"].max())

late = base[(base["closed"]) & (base["hold"] > 1)]
n_not_d1 = len(late) + int((~base["closed"]).sum())
print(f"\nD1 没出的共 {n_not_d1} lots: 最终 +1.5% 脱身 {len(late)}, 没脱身 {int((~base['closed']).sum())}")
print(f"D1 没出 lot 的合计 pnl: ${base[~((base['closed']) & (base['hold'] == 1))]['pnl'].sum():,.0f}")

# 按夜盈亏与 spy/vix 的相关
nightly2 = base.groupby("entry").agg(pnl=("pnl", "sum"), inv=("invested", "sum"), vix=("vix", "first"), spy=("spy", "first"))
nightly2["per10k"] = nightly2["pnl"] / nightly2["inv"] * 10000
print("\n夜度 per10k 与 VIX 相关系数:", round(nightly2["per10k"].corr(nightly2["vix"]), 2))
print("夜度 per10k 与 SPY当日 相关系数:", round(nightly2["per10k"].corr(nightly2["spy"]), 2))
