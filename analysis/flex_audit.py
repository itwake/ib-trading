# -*- coding: utf-8 -*-
"""Flex 全年成交审计: 按'仓位片段(flat->flat)'分组, 分类:
  A. 纪律单: MOC/API 入场 且 次一交易日内清仓
  B. 策略单被扛: MOC/API 入场 但持有 >1 交易日
  C. 手动入场单: 非 API 入场
用 IB 官方 FifoPnlRealized 计盈亏。
"""
import pandas as pd

PATH = r"C:\Users\dvnuo\Downloads\U16971869_U16971869_20250703_20260702_AF_NA_33e97a06b5c8a2e20d9e3a47458d1392.csv"

df = pd.read_csv(PATH, dtype=str)
print("rows:", len(df), "cols:", len(df.columns))
print("LevelOfDetail:", df["LevelOfDetail"].value_counts().to_dict())
print("AssetClass:", df["AssetClass"].value_counts().to_dict())

df = df[(df["LevelOfDetail"] == "EXECUTION") & (df["AssetClass"] == "STK")].copy()
for c in ["Quantity", "TradePrice", "IBCommission", "FifoPnlRealized", "NetCash"]:
    df[c] = pd.to_numeric(df[c], errors="coerce")
df["dt"] = pd.to_datetime(df["DateTime"], format="%Y%m%d;%H%M%S")
df["TradeDate"] = pd.to_datetime(df["TradeDate"], format="%Y%m%d").dt.date.astype(str)
df = df.sort_values(["Symbol", "dt", "TradeID"]).reset_index(drop=True)

print("\n期间:", df["TradeDate"].min(), "->", df["TradeDate"].max())
print("总成交笔数:", len(df), " 总佣金: $%.0f" % df["IBCommission"].sum())
print("全部 FifoPnlRealized 合计: $%.0f" % df["FifoPnlRealized"].sum())
print("OrderType 分布:", df["OrderType"].value_counts().to_dict())
print("IsAPIOrder:", df["IsAPIOrder"].value_counts().to_dict())

cal = sorted(df["TradeDate"].unique())
cal_idx = {d: i for i, d in enumerate(cal)}

# ===== flat->flat 仓位片段 =====
episodes = []
for sym, g in df.groupby("Symbol"):
    pos = 0.0
    cur = None
    for r in g.itertuples(index=False):
        q = r.Quantity
        if cur is None:
            cur = dict(symbol=sym, entry_dt=r.dt, entry_date=r.TradeDate, entry_types=set(), entry_api=set(),
                       exit_types=set(), buys=0.0, pnl=0.0, comm=0.0, last_date=r.TradeDate, max_cost=0.0)
        cur["comm"] += r.IBCommission
        cur["pnl"] += r.FifoPnlRealized if pd.notna(r.FifoPnlRealized) else 0.0
        cur["last_date"] = r.TradeDate
        if q > 0:
            cur["entry_types"].add(str(r.OrderType))
            cur["entry_api"].add(str(r.IsAPIOrder))
            cur["buys"] += q * r.TradePrice
            cur["max_cost"] = max(cur["max_cost"], cur["buys"])
        else:
            cur["exit_types"].add(str(r.OrderType))
        pos += q
        if abs(pos) < 1e-6:
            cur["hold_td"] = cal_idx[cur["last_date"]] - cal_idx[cur["entry_date"]]
            episodes.append(cur)
            cur = None
            pos = 0.0
    if cur is not None:
        cur["hold_td"] = None  # 仍未平仓
        episodes.append(cur)

ep = pd.DataFrame(episodes)
ep["api_entry"] = ep["entry_api"].apply(lambda s: s == {"Y"})
ep["moc_entry"] = ep["entry_types"].apply(lambda s: "MOC" in s and len(s) == 1)
open_ep = ep[ep["hold_td"].isna()]
ep = ep[ep["hold_td"].notna()].copy()

def bucket(r):
    if not r["api_entry"]:
        return "C.手动入场"
    if r["hold_td"] <= 1:
        return "A.纪律单(次日内出)"
    return "B.策略单被扛(>1天)"

ep["bucket"] = ep.apply(bucket, axis=1)
print(f"\n仓位片段: 已平 {len(ep)}, 未平 {len(open_ep)} ({','.join(open_ep['symbol'])})")

print("\n========== 分账结果 ==========")
agg = ep.groupby("bucket").apply(lambda g: pd.Series({
    "片段数": len(g),
    "盈利片段": int((g["pnl"] > 0).sum()),
    "亏损片段": int((g["pnl"] <= 0).sum()),
    "净盈亏$": round(g["pnl"].sum()),
    "平均$": round(g["pnl"].mean(), 1),
    "最惨一笔$": round(g["pnl"].min()),
    "投入合计$": round(g["max_cost"].sum()),
}), include_groups=False)
print(agg.to_string())

print("\nB 桶按持有天数细分:")
b = ep[ep["bucket"] == "B.策略单被扛(>1天)"].copy()
b["hold_bin"] = pd.cut(b["hold_td"], [1, 3, 7, 15, 1000], labels=["2-3天", "4-7天", "8-15天", ">15天"])
print(b.groupby("hold_bin", observed=True)["pnl"].agg(["count", "sum"]).round(0).to_string())

print("\n最惨 12 个片段:")
worst = ep.nsmallest(12, "pnl")[["symbol", "entry_date", "last_date", "hold_td", "bucket", "pnl", "max_cost"]]
print(worst.to_string(index=False))

print("\n最赚 5 个片段:")
print(ep.nlargest(5, "pnl")[["symbol", "entry_date", "last_date", "hold_td", "bucket", "pnl"]].to_string(index=False))

# ===== 按月 & 按桶 =====
ep["month"] = ep["last_date"].str[:7]
pv = ep.pivot_table(index="month", columns="bucket", values="pnl", aggfunc="sum").round(0)
pv["合计"] = pv.sum(axis=1)
print("\n========== 按月分账 (盈亏记在平仓月) ==========")
print(pv.to_string())

ep.to_csv(r"C:\CCWork\ib-trading\data\flex_episodes.csv", index=False)
print("\nsaved flex_episodes.csv")
