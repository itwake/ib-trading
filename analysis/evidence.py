# -*- coding: utf-8 -*-
"""策略规则书的证据计算:
A. VIX/SPY 阈值敏感性 (稳健性: 是平台还是尖峰)
B. 分段样本检验 (P1 vs P3 内部 VIX 规则是否都成立)
C. 扛单生存分析 (持有到第 k 天还没脱身的 lot, 继续持有的期望)
D. 止盈目标敏感性 (1% / 1.5% / 2% / 3%)
E. 入场跌幅深度分桶
F. 套牢 lot 的按夜聚集度 (行业集中的代理证据)
G. 推荐组合 vs 现行 逐月对比
"""
import pandas as pd
import yfinance as yf
from collections import defaultdict

DATA = r"C:\CCWork\ib-trading\data"

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

mkt = yf.download(["SPY", "^VIX"], start="2026-01-20", end="2026-07-04", auto_adjust=True, progress=False, group_by="ticker")
spy_ret = {str(k.date()): float(v) for k, v in (mkt["SPY"]["Close"].pct_change() * 100).dropna().items()}
vix_close = {str(k.date()): float(v) for k, v in mkt["^VIX"]["Close"].dropna().items()}


def fee(q):
    return max(1.0, 0.005 * q)


def sim_lot(ticker, entry_date, shares, profit=1.015, ts_n=None):
    dts = tdates[ticker]
    if entry_date not in dts:
        return None
    i0 = dts.index(entry_date)
    entry_px = float(px[(ticker, entry_date)].Close)
    qty, cost = shares, entry_px
    obs = len(dts) - 1 - i0  # 可观察天数
    for k, d in enumerate(dts[i0 + 1:], start=1):
        ratio = splits.get((ticker, d))
        if ratio and ratio not in (0, 1):
            qty = int(round(qty * ratio))
            cost /= ratio
        row = px[(ticker, d)]
        o, h, c = float(row.Open), float(row.High), float(row.Close)
        target = round(cost * profit, 2)
        if o >= target:
            return dict(hold=k, closed=True, pnl=(o - cost) * qty - 2 * fee(qty), inv=shares * entry_px, obs=obs)
        if h >= target:
            return dict(hold=k, closed=True, pnl=(target - cost) * qty - 2 * fee(qty), inv=shares * entry_px, obs=obs)
        if ts_n is not None and k >= ts_n:
            return dict(hold=k, closed=True, pnl=(c - cost) * qty - 2 * fee(qty), inv=shares * entry_px, obs=obs)
    last = dts[-1]
    c = float(px[(ticker, last)].Close)
    return dict(hold=obs, closed=False, pnl=(c - cost) * qty - 2 * fee(qty), inv=shares * entry_px, obs=obs)


def run(profit=1.015, ts_n=None, vix_min=None, spy_max=None, entry_range=None, or_filter=False):
    recs = []
    for b in buys.itertuples(index=False):
        if entry_range and not (entry_range[0] <= b.us_date <= entry_range[1]):
            continue
        v, s = vix_close.get(b.us_date), spy_ret.get(b.us_date)
        if or_filter:  # VIX>=vix_min 或 SPY<=spy_max 满足其一
            ok = (v is not None and v >= vix_min) or (s is not None and s <= spy_max)
            if not ok:
                continue
        else:
            if vix_min is not None and (v is None or v < vix_min):
                continue
            if spy_max is not None and (s is None or s > spy_max):
                continue
        r = sim_lot(b.ticker, b.us_date, b.shares, profit, ts_n)
        if r:
            r.update(ticker=b.ticker, entry=b.us_date, chg=b.change_pct, vix=v, spy=s)
            recs.append(r)
    return pd.DataFrame(recs)


def summ(df):
    if not len(df):
        return dict(lots=0)
    cl = df[df["closed"]]
    return dict(
        lots=len(df), per10k=round(df["pnl"].sum() / df["inv"].sum() * 10000),
        net=round(df["pnl"].sum()), stuck=int((~df["closed"]).sum()),
        stuck_pct=round((~df["closed"]).sum() / len(df) * 100, 1),
        losers=int((cl["pnl"] <= 0).sum()),
    )


print("========== A. 阈值敏感性 ==========")
rows = []
for th in [17, 18, 19, 20, 21, 22]:
    rows.append(dict(rule=f"VIX>={th}", **summ(run(vix_min=th))))
for th in [0.0, -0.25, -0.5, -0.75, -1.0]:
    rows.append(dict(rule=f"SPY<={th}%", **summ(run(spy_max=th))))
print(pd.DataFrame(rows).set_index("rule").to_string())

print("\n========== B. 分段样本 (规则在两段独立成立吗) ==========")
rows = []
for tag, rng in [("P1(1/26-3/6)", ("2026-01-01", "2026-03-06")), ("P3(5/29-7/2)", ("2026-05-29", "2026-12-31"))]:
    df_all = run(entry_range=rng)
    hi = df_all[df_all["vix"] >= 19]
    lo = df_all[df_all["vix"] < 19]
    rows.append(dict(seg=tag, grp="VIX>=19", **summ(hi)))
    rows.append(dict(seg=tag, grp="VIX<19", **summ(lo)))
print(pd.DataFrame(rows).set_index(["seg", "grp"]).to_string())

print("\n========== C. 扛单生存分析 (baseline 无止损) ==========")
base = run()
rows = []
for k in [1, 2, 3, 4, 5, 7, 10, 15]:
    alive = base[(base["hold"] >= k) & (base["obs"] >= k)]  # 第k天开盘时仍未脱身
    if not len(alive):
        continue
    esc = alive[alive["closed"]]
    rows.append(
        dict(
            day=k, 仍被套lot=len(alive),
            最终脱身率=f"{len(esc) / len(alive) * 100:.0f}%",
            继续持有的每10k期望=round(alive["pnl"].sum() / alive["inv"].sum() * 10000),
        )
    )
print(pd.DataFrame(rows).set_index("day").to_string())
print("(censoring: 观察不足k天的新lot已剔除)")

print("\n========== D. 止盈目标敏感性 ==========")
rows = []
for p in [1.01, 1.015, 1.02, 1.03]:
    for ts in [None, 3]:
        df = run(profit=p, ts_n=ts)
        d1 = (df[df["closed"]]["hold"] == 1).sum() / len(df) * 100
        rows.append(dict(target=f"+{(p - 1) * 100:.1f}%", stop="无" if ts is None else f"T+{ts}", D1退出=f"{d1:.0f}%", **summ(df)))
print(pd.DataFrame(rows).set_index(["target", "stop"]).to_string())

print("\n========== E. 入场跌幅深度 ==========")
base["depth"] = pd.cut(base["chg"], [-100, -12, -8, -5, 0], labels=["<-12%", "-12~-8%", "-8~-5%", ">-5%"])
agg = base.groupby("depth", observed=True).apply(
    lambda g: pd.Series(summ(g)), include_groups=False)
print(agg.to_string())

print("\n========== F. 套牢 lot 的按夜聚集 ==========")
stuck = base[~base["closed"]]
by_night = stuck.groupby("entry").size().sort_values(ascending=False)
print(f"套牢 {len(stuck)} lots 分布在 {len(by_night)} 个夜; 前5夜占 {by_night.head(5).sum()} 个:")
for d, n in by_night.head(5).items():
    tk = ",".join(stuck[stuck["entry"] == d]["ticker"])
    print(f"  {d}: {n} lots (VIX={vix_close.get(d, float('nan')):.1f}, SPY={spy_ret.get(d, float('nan')):+.2f}%) {tk}")

print("\n========== G. 推荐组合 vs 现行 ==========")
variants = {
    "现行(=+1.5%扛到底)": run(),
    "推荐: VIX>=19或SPY<=-0.5 + T+3止损": run(ts_n=3, vix_min=19, spy_max=-0.5, or_filter=True),
    "推荐(不带止损): 仅环境闸门(OR)": run(vix_min=19, spy_max=-0.5, or_filter=True),
}
rows = []
for name, df in variants.items():
    s = summ(df)
    df["month"] = df["entry"].str[:7]
    monthly = df.groupby("month")["pnl"].sum()
    s["最差月"] = round(monthly.min())
    s["盈利月/总月"] = f"{(monthly > 0).sum()}/{len(monthly)}"
    rows.append(dict(variant=name, **s))
print(pd.DataFrame(rows).set_index("variant").to_string())

df_rec = variants["推荐: VIX>=19或SPY<=-0.5 + T+3止损"]
df_rec["month"] = df_rec["entry"].str[:7]
b2 = variants["现行(=+1.5%扛到底)"]
b2["month"] = b2["entry"].str[:7]
cmp = pd.DataFrame({"现行": b2.groupby("month")["pnl"].sum().round(0), "推荐": df_rec.groupby("month")["pnl"].sum().round(0)})
print("\n按月净盈亏对比:")
print(cmp.to_string())
