# -*- coding: utf-8 -*-
"""按 run 聚合: 运行时刻/只数/单笔金额 -> 区分系统化运行与手动干预; 并输出活跃时间线。"""
import pandas as pd

buys = pd.read_csv(r"C:\CCWork\ib-trading\data\buys.csv")
buys = buys[buys["submitted"] == 1]
buys["run_ts_beijing"] = pd.to_datetime(buys["run_ts_beijing"])

g = buys.groupby("run_ts_beijing").agg(
    us_date=("us_date", "first"),
    n=("ticker", "count"),
    total=("invest", "sum"),
    per_stock=("invest", "median"),
)
g = g.reset_index()
g["hour"] = g["run_ts_beijing"].dt.hour + g["run_ts_beijing"].dt.minute / 60

print(g.to_string(index=False,
      formatters={"total": "{:.0f}".format, "per_stock": "{:.0f}".format, "hour": "{:.1f}".format}))

print("\n每月运行次数:")
g["ym"] = g["run_ts_beijing"].dt.strftime("%Y-%m")
print(g.groupby("ym").agg(runs=("n", "count"), invested=("total", "sum")).to_string())

# 相邻运行间隔(按美国交易日would need calendar; 用自然日)
g = g.sort_values("run_ts_beijing")
g["gap_days"] = g["run_ts_beijing"].diff().dt.days
print("\n间隔>4天的停摆期:")
for _, r in g[g["gap_days"] > 4].iterrows():
    prev = g[g["run_ts_beijing"] < r["run_ts_beijing"]]["run_ts_beijing"].max()
    print(f"  {prev.date()} -> {r['run_ts_beijing'].date()}  ({int(r['gap_days'])} 天)")
