# -*- coding: utf-8 -*-
"""补充统计: 每日总盈亏曲线 / 逐笔命中率 / 加仓摊平效果 / SPY 对比。"""
import pandas as pd

trips = pd.read_csv(r"C:\CCWork\ib-trading\data\round_trips.csv")
opens = pd.read_csv(r"C:\CCWork\ib-trading\data\open_positions.csv")
daily = pd.read_csv(r"C:\CCWork\ib-trading\data\daily_exposure.csv")
buys = pd.read_csv(r"C:\CCWork\ib-trading\data\buys.csv")
buys = buys[(buys["submitted"] == 1) & (buys["shares"] > 0)]

# 每日累计 realized
trips["exit_date"] = trips["exit_date"].astype(str)
realized_by_day = trips.groupby("exit_date")["pnl"].sum().cumsum()
daily["realized_cum"] = daily["date"].map(realized_by_day).ffill().fillna(0)
daily["unrealized"] = daily["market_value"] - daily["cost_basis"]
daily["total_pnl"] = daily["realized_cum"] + daily["unrealized"]

peak = daily["total_pnl"].cummax()
dd = daily["total_pnl"] - peak
print("=== 总盈亏曲线 ===")
print("最终 total P&L:", round(daily["total_pnl"].iloc[-1], 2))
print("峰值 P&L:", round(daily["total_pnl"].max(), 2), "on", daily.loc[daily["total_pnl"].idxmax(), "date"])
print("最大回撤:", round(dd.min(), 2), "on", daily.loc[dd.idxmin(), "date"])
print("敞口(成本)峰值:", daily["cost_basis"].max(), "on", daily.loc[daily["cost_basis"].idxmax(), "date"])
print("敞口中位数:", round(daily["cost_basis"].median(), 2))

# 月末快照
daily["ym"] = daily["date"].str[:7]
snap = daily.groupby("ym").last()[["n_pos", "cost_basis", "realized_cum", "unrealized", "total_pnl"]]
print("\n=== 月末快照 ===")
print(snap.to_string())

# 逐笔买入 lot 命中率: 每个 lot 最终归宿
total_lots = len(buys)
lots_in_open = opens.shape[0]
n_entries_closed = trips["n_entries"].sum()
print("\n=== lot 统计 ===")
print("提交买入 lot 总数:", total_lots)
print("已平仓消耗 lot:", int(n_entries_closed))
print("留在未平仓中的 lot:", total_lots - int(n_entries_closed), f"(未平仓 position 数 {lots_in_open})")

# 平仓率与摊平
print("\nround-trips n_entries 分布:")
print(trips["n_entries"].value_counts().sort_index().to_string())
multi = trips[trips["n_entries"] > 1]
print(f"靠加仓摊平后逃出的比例: {len(multi)}/{len(trips)}")

# 持有>5天的平仓
late = trips[trips["hold_days"] > 5]
print(f"\n持有>5交易日才逃出的: {len(late)} 笔, pnl 合计 {late['pnl'].sum():.2f}")

# invested 加权收益率
tot_inv_closed = trips["invested"].sum()
print(f"\n已平仓累计投入(逐笔求和): ${tot_inv_closed:,.0f}, 实现收益率(按投入): {trips['pnl'].sum() / tot_inv_closed * 100:.2f}%")

# SPY 对比
import yfinance as yf

spy = yf.download("SPY", start="2026-01-26", end="2026-07-04", auto_adjust=True, progress=False)
if isinstance(spy.columns, pd.MultiIndex):
    spy.columns = spy.columns.get_level_values(0)
r = spy["Close"].iloc[-1] / spy["Close"].iloc[0] - 1
print(f"\nSPY 同期 (2026-01-26 ~ 2026-07-02): {r * 100:.2f}%")

daily.to_csv(r"C:\CCWork\ib-trading\data\daily_pnl.csv", index=False)
print("\nsaved daily_pnl.csv")
