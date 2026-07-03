# -*- coding: utf-8 -*-
"""逐 lot 多变体回测 + 市场环境分桶 + 阶段归因。

变体:
  baseline      : 限价 cost*1.015 挂到永远 (现行策略机制)
  next_open     : 次日开盘无条件卖出 (纯隔夜)
  next_close    : 次日收盘无条件卖出
  ts3/ts5/ts10  : 限价 +1.5%, N 个交易日未成交则收盘市价离场
  vix30 / vix25 : 入场过滤: 当日 VIX 收盘 > 阈值则当晚不买
  ts5_vix30     : 组合
简化: lot 之间独立(不做 avgCost 合并), 限价单开盘跳空按开盘价成交。
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

sp_rows = prices[(prices["Stock Splits"].notna()) & (prices["Stock Splits"] != 0)]
splits = {(r["ticker"], r["Date"]): float(r["Stock Splits"]) for _, r in sp_rows.iterrows()}

# 市场数据
mkt = yf.download(["SPY", "^VIX"], start="2026-01-20", end="2026-07-04", auto_adjust=True, progress=False, group_by="ticker")
spy = mkt["SPY"]["Close"]
vix = mkt["^VIX"]["Close"]
spy_ret = spy.pct_change() * 100
spy_ret.index = spy_ret.index.date.astype(str) if hasattr(spy_ret.index, "date") else spy_ret.index
vix.index = vix.index.date.astype(str) if hasattr(vix.index, "date") else vix.index
spy_ret = {str(k): float(v) for k, v in spy_ret.items() if pd.notna(v)}
vix_close = {str(k): float(v) for k, v in vix.items() if pd.notna(v)}


def fee(q):
    return max(1.0, 0.005 * q)


def sim_lot(ticker, entry_date, shares, mode, ts_n=None):
    """返回 (exit_date, exit_px, hold_days, closed) 或标记未平仓。"""
    dts = tdates[ticker]
    if entry_date not in dts:
        return None
    i0 = dts.index(entry_date)
    entry_px = float(px[(ticker, entry_date)].Close)
    qty = shares
    cost = entry_px
    for k, d in enumerate(dts[i0 + 1:], start=1):
        ratio = splits.get((ticker, d))
        if ratio and ratio not in (0, 1):
            qty = int(round(qty * ratio))
            cost = cost / ratio
        row = px[(ticker, d)]
        o, h, c = float(row.Open), float(row.High), float(row.Close)
        if mode == "next_open":
            return dict(exit_date=d, exit_px=o, hold=k, closed=True, qty=qty, cost=cost, entry_px=entry_px)
        if mode == "next_close":
            return dict(exit_date=d, exit_px=c, hold=k, closed=True, qty=qty, cost=cost, entry_px=entry_px)
        target = round(cost * PROFIT, 2)
        if o >= target:
            return dict(exit_date=d, exit_px=o, hold=k, closed=True, qty=qty, cost=cost, entry_px=entry_px)
        if h >= target:
            return dict(exit_date=d, exit_px=target, hold=k, closed=True, qty=qty, cost=cost, entry_px=entry_px)
        if ts_n is not None and k >= ts_n:
            return dict(exit_date=d, exit_px=c, hold=k, closed=True, qty=qty, cost=cost, entry_px=entry_px)
    # 未平仓: 用最后一个可用收盘
    last = dts[-1]
    return dict(exit_date=last, exit_px=float(px[(ticker, last)].Close), hold=len(dts) - 1 - i0, closed=False, qty=qty, cost=cost, entry_px=entry_px)


VARIANTS = {
    "baseline": dict(mode="limit", ts=None, vix_max=None),
    "next_open": dict(mode="next_open", ts=None, vix_max=None),
    "next_close": dict(mode="next_close", ts=None, vix_max=None),
    "ts3": dict(mode="limit", ts=3, vix_max=None),
    "ts5": dict(mode="limit", ts=5, vix_max=None),
    "ts10": dict(mode="limit", ts=10, vix_max=None),
    "vix25": dict(mode="limit", ts=None, vix_max=25),
    "vix30": dict(mode="limit", ts=None, vix_max=30),
    "ts5_vix30": dict(mode="limit", ts=5, vix_max=30),
    "ts5_vix25": dict(mode="limit", ts=5, vix_max=25),
}

results = {}
lot_records = {}
for name, cfg in VARIANTS.items():
    recs = []
    for b in buys.itertuples(index=False):
        v = vix_close.get(b.us_date)
        if cfg["vix_max"] is not None and v is not None and v > cfg["vix_max"]:
            continue
        r = sim_lot(b.ticker, b.us_date, b.shares, cfg["mode"], cfg["ts"])
        if r is None:
            continue
        qty, cost = r["qty"], r["cost"]
        gross = (r["exit_px"] - cost) * qty
        net = gross - fee(qty) - fee(qty)
        recs.append(
            dict(
                ticker=b.ticker, entry=b.us_date, exit=r["exit_date"], hold=r["hold"],
                closed=r["closed"], invested=round(b.shares * r["entry_px"], 2),
                pnl=round(net, 2), ret_pct=round((r["exit_px"] / cost - 1) * 100, 2),
                vix=round(v, 1) if v else None, spy=round(spy_ret.get(b.us_date, float("nan")), 2),
            )
        )
    df = pd.DataFrame(recs)
    closed = df[df["closed"]]
    open_ = df[~df["closed"]]
    results[name] = dict(
        lots=len(df), invested=df["invested"].sum(),
        realized=closed["pnl"].sum(), open_pnl=open_["pnl"].sum(), open_n=len(open_),
        net=df["pnl"].sum(),
        win=int((closed["pnl"] > 0).sum()), loss=int((closed["pnl"] <= 0).sum()),
        med_hold=closed["hold"].median() if len(closed) else None,
    )
    lot_records[name] = df

summary = pd.DataFrame(results).T
summary["net_per_10k"] = (summary["net"] / summary["invested"] * 10000).round(0)
print("=== 变体对比 (lot 独立近似) ===")
print(summary.round(0).to_string())

# ===== baseline 的 VIX / SPY 分桶 =====
base = lot_records["baseline"].copy()
base["vix_bucket"] = pd.cut(base["vix"], [0, 20, 25, 30, 100], labels=["<20", "20-25", "25-30", ">30"])
base["spy_bucket"] = pd.cut(base["spy"], [-10, -1.5, -0.5, 0.5, 10], labels=["<-1.5%", "-1.5~-0.5%", "-0.5~0.5%", ">0.5%"])

for col in ["vix_bucket", "spy_bucket"]:
    agg = base.groupby(col, observed=True).apply(
        lambda g: pd.Series(
            {
                "lots": len(g),
                "D1退出率%": round((g[g["closed"]]["hold"] == 1).sum() / len(g) * 100, 1),
                "未脱身率%": round((~g["closed"]).sum() / len(g) * 100, 1),
                "每$10k盈亏": round(g["pnl"].sum() / g["invested"].sum() * 10000, 0),
                "平均持有日": round(g["hold"].mean(), 1),
            }
        ),
        include_groups=False,
    )
    print(f"\n=== baseline 按 {col} ===")
    print(agg.to_string())

# ===== 阶段归因 (baseline) =====
def phase(d):
    if d <= "2026-03-06":
        return "P1: 1/26-3/6"
    if d <= "2026-04-02":
        return "P2: 4/1-4/2"
    return "P3: 5/29-7/2"

base["phase"] = base["entry"].map(phase)
pa = base.groupby("phase").apply(
    lambda g: pd.Series(
        {
            "lots": len(g),
            "gross投入": round(g["invested"].sum(), 0),
            "realized": round(g[g["closed"]]["pnl"].sum(), 0),
            "open_pnl": round(g[~g["closed"]]["pnl"].sum(), 0),
            "net": round(g["pnl"].sum(), 0),
            "net/10k": round(g["pnl"].sum() / g["invested"].sum() * 10000, 0),
            "未脱身数": int((~g["closed"]).sum()),
        }
    ),
    include_groups=False,
)
print("\n=== 阶段归因 (baseline) ===")
print(pa.to_string())

# 手动 run: 6/11 22:52 DAN & ORCL
manual = base[(base["entry"] == "2026-06-11") & (base["ticker"].isin(["DAN", "ORCL"]))]
print("\n=== 手动交易样本 (6/11 DAN/ORCL) ===")
print(manual.to_string(index=False))

base.to_csv(f"{DATA}\\lots_baseline.csv", index=False)
for n in ["next_close", "ts5", "ts5_vix30"]:
    lot_records[n].to_csv(f"{DATA}\\lots_{n}.csv", index=False)
print("\nsaved lot csvs")
