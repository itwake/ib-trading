# -*- coding: utf-8 -*-
"""Flex 审计 v2: 片段盈亏 = sum(NetCash) (含佣金, 拆股免疫)。
脏链(期初已有持仓/仓位为负)单独隔离。"""
import pandas as pd

PATH = r"C:\Users\dvnuo\Downloads\U16971869_U16971869_20250703_20260702_AF_NA_33e97a06b5c8a2e20d9e3a47458d1392.csv"

df = pd.read_csv(PATH, dtype=str)
df = df[(df["LevelOfDetail"] == "EXECUTION") & (df["AssetClass"] == "STK")].copy()
for c in ["Quantity", "TradePrice", "IBCommission", "NetCash"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df["dt"] = pd.to_datetime(df["DateTime"], format="%Y%m%d;%H%M%S")
df["TradeDate"] = pd.to_datetime(df["TradeDate"], format="%Y%m%d").dt.date.astype(str)
df = df.sort_values(["Symbol", "dt", "TradeID"]).reset_index(drop=True)

cal = sorted(df["TradeDate"].unique())
cal_idx = {d: i for i, d in enumerate(cal)}

episodes, dirty = [], {}
for sym, g in df.groupby("Symbol"):
    pos, cur, bad = 0.0, None, False
    for r in g.itertuples(index=False):
        q = r.Quantity
        if cur is None:
            if q < 0:  # 期初已有仓位, 整个 symbol 标脏
                bad = True
                break
            cur = dict(symbol=sym, entry_date=r.TradeDate, entry_types=set(), entry_api=set(),
                       exit_types=set(), cash=0.0, buys=0.0, last_date=r.TradeDate)
        cur["cash"] += r.NetCash
        cur["last_date"] = r.TradeDate
        if q > 0:
            cur["entry_types"].add(str(r.OrderType))
            cur["entry_api"].add(str(r.IsAPIOrder))
            cur["buys"] += q * r.TradePrice
        else:
            cur["exit_types"].add(str(r.OrderType))
        pos += q
        if pos < -1e-6:
            bad = True
            break
        if abs(pos) < 1e-6:
            cur["hold_td"] = cal_idx[cur["last_date"]] - cal_idx[cur["entry_date"]]
            episodes.append(cur)
            cur = None
    if bad:
        dirty[sym] = round(g["NetCash"].sum())
        episodes = [e for e in episodes if e["symbol"] != sym]
    elif cur is not None:
        cur["hold_td"] = None
        episodes.append(cur)

ep = pd.DataFrame(episodes)
open_ep = ep[ep["hold_td"].isna()]
ep = ep[ep["hold_td"].notna()].copy()
ep["pnl"] = ep["cash"].round(2)
ep["api_entry"] = ep["entry_api"].apply(lambda s: s == {"Y"})

def bucket(r):
    if not r["api_entry"]:
        return "C.手动入场"
    return "A.纪律单(次日内出)" if r["hold_td"] <= 1 else "B.策略单被扛(>1天)"

ep["bucket"] = ep.apply(bucket, axis=1)
ep["era"] = ep["entry_date"].apply(lambda d: "2025下半年" if d < "2026-01-01" else "2026")

print(f"可信片段: 已平 {len(ep)}, 未平 {len(open_ep)} ({','.join(sorted(open_ep['symbol']))})")
print(f"脏链 symbol (期初持仓/拆股, 单独计): {dirty}")
print(f"脏链现金流合计: ${sum(dirty.values()):,}")

print("\n========== 分账 (全期间) ==========")
agg = ep.groupby("bucket").apply(lambda g: pd.Series({
    "片段数": len(g), "盈利": int((g["pnl"] > 0).sum()), "亏损": int((g["pnl"] <= 0).sum()),
    "胜率%": round((g["pnl"] > 0).mean() * 100), "净盈亏$": round(g["pnl"].sum()),
    "平均$": round(g["pnl"].mean(), 1), "最惨$": round(g["pnl"].min()),
}), include_groups=False)
print(agg.to_string())

print("\n========== 分账 × 时代 ==========")
agg2 = ep.groupby(["era", "bucket"]).apply(lambda g: pd.Series({
    "片段数": len(g), "胜率%": round((g["pnl"] > 0).mean() * 100),
    "净盈亏$": round(g["pnl"].sum()), "平均$": round(g["pnl"].mean(), 1),
}), include_groups=False)
print(agg2.to_string())

print("\nC 桶(手动入场)按持有时间:")
c = ep[ep["bucket"] == "C.手动入场"].copy()
c["hold_bin"] = pd.cut(c["hold_td"], [-1, 1, 3, 7, 15, 10000], labels=["<=1天", "2-3天", "4-7天", "8-15天", ">15天"])
print(c.groupby("hold_bin", observed=True)["pnl"].agg(["count", "sum"]).round(0).to_string())

print("\nB 桶(策略单被扛):")
b = ep[ep["bucket"] == "B.策略单被扛(>1天)"]
print(b[["symbol", "entry_date", "last_date", "hold_td", "pnl"]].to_string(index=False))

print("\n最惨 15 个片段:")
print(ep.nsmallest(15, "pnl")[["symbol", "entry_date", "last_date", "hold_td", "bucket", "pnl"]].to_string(index=False))

print("\n盈亏集中度: 亏损最大的 10 个片段合计 $%.0f, 占全部净亏损绝对值的比例见下" % ep.nsmallest(10, "pnl")["pnl"].sum())
tot = ep["pnl"].sum()
losses = ep[ep["pnl"] < 0]["pnl"].sum()
wins = ep[ep["pnl"] > 0]["pnl"].sum()
print(f"总: ${tot:,.0f} = 盈利片段 +${wins:,.0f} + 亏损片段 ${losses:,.0f}")
print(f"最惨10个片段 = {ep.nsmallest(10, 'pnl')['pnl'].sum() / losses * 100:.0f}% 的总亏损")

print("\n========== 按月分账 ==========")
ep["month"] = ep["last_date"].str[:7]
pv = ep.pivot_table(index="month", columns="bucket", values="pnl", aggfunc="sum").round(0)
pv["月合计"] = pv.sum(axis=1)
pv["累计"] = pv["月合计"].cumsum()
print(pv.to_string())

ep.to_csv(r"C:\CCWork\ib-trading\data\flex_episodes.csv", index=False)
print("\nsaved")
