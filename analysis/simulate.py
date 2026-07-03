# -*- coding: utf-8 -*-
"""重演策略: 收盘 MOC 买入 -> 每日以 avgCost*1.015 限价卖出(全部), 直至成交。

假设:
- MOC 成交价 = 当日官方收盘价
- 卖出: 从买入次日起(含隔夜, 用日内 High 近似), High >= target 即以 target 成交
- 同票再买 -> 加权平均成本(与 IB avgCost 一致), 目标价随之变化
- 拆股: qty *= ratio, avg_cost /= ratio
- 佣金: 每边 max($1, $0.005/股)
"""
import pandas as pd
from collections import defaultdict

PROFIT = 1.015
BUYS = r"C:\CCWork\ib-trading\data\buys.csv"
PRICES = r"C:\CCWork\ib-trading\data\prices.csv"
OUT_TRIPS = r"C:\CCWork\ib-trading\data\round_trips.csv"
OUT_OPEN = r"C:\CCWork\ib-trading\data\open_positions.csv"
OUT_DAILY = r"C:\CCWork\ib-trading\data\daily_exposure.csv"


def commission(qty):
    return max(1.0, 0.005 * qty)


buys = pd.read_csv(BUYS)
buys = buys[(buys["submitted"] == 1) & (buys["shares"] > 0)].copy()
prices = pd.read_csv(PRICES)
prices["Date"] = pd.to_datetime(prices["Date"], format="ISO8601").dt.date.astype(str)

px = {}  # (ticker, date) -> row
by_ticker_dates = defaultdict(list)
for r in prices.itertuples(index=False):
    px[(r.ticker, r.Date)] = r
    by_ticker_dates[r.ticker].append(r.Date)

all_days = sorted(prices["Date"].unique())
buys_by_date = defaultdict(list)
skipped = []
for r in buys.itertuples(index=False):
    if (r.ticker, r.us_date) in px:
        buys_by_date[r.us_date].append(r)
    else:
        skipped.append((r.us_date, r.ticker, r.shares, r.ref_price))

splits_by_td = {}
if "Stock Splits" in prices.columns:
    sp = prices[(prices["Stock Splits"].notna()) & (prices["Stock Splits"] != 0)]
    for r in sp.itertuples(index=False):
        splits_by_td[(r.ticker, r.Date)] = float(r._7 if hasattr(r, "_7") else r[-1])
# 直接重新取, 保险
sp_rows = prices[(prices.get("Stock Splits").notna()) & (prices["Stock Splits"] != 0)] if "Stock Splits" in prices.columns else pd.DataFrame()
splits_by_td = {(r["ticker"], r["Date"]): float(r["Stock Splits"]) for _, r in sp_rows.iterrows()}

positions = {}  # ticker -> dict(qty, avg, first_date, entries=[(date,qty,price)])
trips = []
daily_rows = []

for day in all_days:
    # 1) 拆股调整
    for t, p in positions.items():
        ratio = splits_by_td.get((t, day))
        if ratio and ratio > 0 and ratio != 1:
            p["qty"] = int(round(p["qty"] * ratio))
            p["avg"] = p["avg"] / ratio

    # 2) 卖出检查 (仅对今天之前建立的持仓)
    for t in list(positions.keys()):
        p = positions[t]
        if p["first_date"] == day and p["last_buy_date"] == day:
            pass
        row = px.get((t, day))
        if row is None:
            continue
        if p["last_buy_date"] >= day:
            continue  # 当日(收盘)才买入的仓位, 次日才开始卖
        target = round(p["avg"] * PROFIT, 2)
        if float(row.High) >= target:
            qty = p["qty"]
            fee = commission(qty)
            pnl = (target - p["avg"]) * qty - fee - p["buy_fees"]
            trips.append(
                {
                    "ticker": t,
                    "first_entry": p["first_date"],
                    "last_entry": p["last_buy_date"],
                    "exit_date": day,
                    "qty": qty,
                    "avg_cost": round(p["avg"], 4),
                    "exit_price": target,
                    "invested": round(p["avg"] * qty, 2),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round((target / p["avg"] - 1) * 100, 3),
                    "n_entries": len(p["entries"]),
                }
            )
            del positions[t]

    # 3) 收盘 MOC 买入
    for r in buys_by_date.get(day, []):
        row = px[(r.ticker, day)]
        fill = float(row.Close)
        fee = commission(r.shares)
        if r.ticker in positions:
            p = positions[r.ticker]
            total_cost = p["avg"] * p["qty"] + fill * r.shares
            p["qty"] += r.shares
            p["avg"] = total_cost / p["qty"]
            p["last_buy_date"] = day
            p["entries"].append((day, r.shares, fill))
            p["buy_fees"] += fee
        else:
            positions[r.ticker] = {
                "qty": r.shares,
                "avg": fill,
                "first_date": day,
                "last_buy_date": day,
                "entries": [(day, r.shares, fill)],
                "buy_fees": fee,
            }

    # 4) 日终敞口
    mv = 0.0
    cost_basis = 0.0
    for t, p in positions.items():
        row = px.get((t, day))
        close = float(row.Close) if row else p["avg"]
        mv += close * p["qty"]
        cost_basis += p["avg"] * p["qty"]
    daily_rows.append({"date": day, "n_pos": len(positions), "cost_basis": round(cost_basis, 2), "market_value": round(mv, 2)})

# 期末未平仓
open_rows = []
last_day = all_days[-1]
for t, p in positions.items():
    row = px.get((t, last_day))
    # 找该票最后一个有数据的日子
    if row is None:
        dts = [d for d in by_ticker_dates[t] if d <= last_day]
        row = px[(t, dts[-1])] if dts else None
    close = float(row.Close) if row else p["avg"]
    open_rows.append(
        {
            "ticker": t,
            "first_entry": p["first_date"],
            "last_entry": p["last_buy_date"],
            "qty": p["qty"],
            "avg_cost": round(p["avg"], 4),
            "last_close": close,
            "invested": round(p["avg"] * p["qty"], 2),
            "market_value": round(close * p["qty"], 2),
            "unrealized_pnl": round((close - p["avg"]) * p["qty"], 2),
            "unrealized_pct": round((close / p["avg"] - 1) * 100, 2),
            "target": round(p["avg"] * PROFIT, 2),
        }
    )

trips_df = pd.DataFrame(trips).sort_values("exit_date")
open_df = pd.DataFrame(open_rows).sort_values("unrealized_pnl")
daily_df = pd.DataFrame(daily_rows)
trips_df.to_csv(OUT_TRIPS, index=False)
open_df.to_csv(OUT_OPEN, index=False)
daily_df.to_csv(OUT_DAILY, index=False)

# ===== 摘要 =====
print("=== 无价格数据被跳过的买入 ===")
for s in skipped:
    print("  ", s)

n = len(trips_df)
print(f"\n=== 已平仓 round-trips: {n} ===")
print(f"realized P&L 合计: ${trips_df['pnl'].sum():,.2f}")
print(f"平均每笔: ${trips_df['pnl'].mean():,.2f}  中位持有天数与分布见下")

# 持有交易日数
day_index = {d: i for i, d in enumerate(all_days)}
trips_df["hold_days"] = trips_df.apply(lambda r: day_index[r["exit_date"]] - day_index[r["first_entry"]], axis=1)
print("\n持有交易日分布 (从首次买入到卖出):")
print(trips_df["hold_days"].describe().to_string())
dist = trips_df["hold_days"].value_counts().sort_index()
print(dist.head(15).to_string())

print("\n按月 realized P&L:")
trips_df["month"] = trips_df["exit_date"].str[:7]
print(trips_df.groupby("month")["pnl"].agg(["count", "sum"]).to_string())

print(f"\n=== 未平仓: {len(open_df)} ===")
print(open_df.to_string(index=False))
print(f"\n未实现盈亏合计: ${open_df['unrealized_pnl'].sum():,.2f}")
print(f"未平仓投入成本: ${open_df['invested'].sum():,.2f}")

print(f"\n=== 总计 ===")
total = trips_df["pnl"].sum() + open_df["unrealized_pnl"].sum()
print(f"realized + unrealized: ${total:,.2f}")
print(f"敞口峰值(成本): ${daily_df['cost_basis'].max():,.2f} on {daily_df.loc[daily_df['cost_basis'].idxmax(), 'date']}")

trips_df.to_csv(OUT_TRIPS, index=False)
print(f"\nsaved: {OUT_TRIPS}, {OUT_OPEN}, {OUT_DAILY}")
