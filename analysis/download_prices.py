# -*- coding: utf-8 -*-
"""下载 buys.csv 涉及的所有 ticker 的日线 OHLC（未复权），存 parquet/csv。"""
import pandas as pd
import yfinance as yf

BUYS = r"C:\CCWork\ib-trading\data\buys.csv"
OUT = r"C:\CCWork\ib-trading\data\prices.csv"

buys = pd.read_csv(BUYS)
tickers = sorted(buys["ticker"].unique())
print(f"{len(tickers)} tickers")

frames = []
failed = []
CHUNK = 50
for i in range(0, len(tickers), CHUNK):
    chunk = tickers[i : i + CHUNK]
    data = yf.download(
        chunk,
        start="2026-01-20",
        end="2026-07-04",
        auto_adjust=False,
        actions=True,
        group_by="ticker",
        progress=False,
        threads=True,
    )
    for t in chunk:
        try:
            df = data[t].dropna(subset=["Close"])
        except KeyError:
            failed.append(t)
            continue
        if df.empty:
            failed.append(t)
            continue
        df = df.reset_index()
        df["ticker"] = t
        frames.append(df[["Date", "ticker", "Open", "High", "Low", "Close", "Volume"] + (["Stock Splits"] if "Stock Splits" in df.columns else [])])
    print(f"chunk {i // CHUNK + 1}: done, cumulative failed={len(failed)}")

out = pd.concat(frames, ignore_index=True)
out.to_csv(OUT, index=False)
print(f"saved {len(out)} rows for {out['ticker'].nunique()} tickers -> {OUT}")
print("FAILED:", failed)

# 分割检测
if "Stock Splits" in out.columns:
    sp = out[(out["Stock Splits"].notna()) & (out["Stock Splits"] != 0)]
    if not sp.empty:
        print("SPLITS DETECTED:")
        print(sp[["Date", "ticker", "Stock Splits"]].to_string())
    else:
        print("no splits in window")
