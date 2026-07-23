# -*- coding: utf-8 -*-
"""mood_daily 历史回填 (一次性): yfinance 系 ~15 个月 + CNN Fear&Greed 历史曲线。
Put/Call 与 NAAIM 无免费历史源, 只从上线日起前向积累。
用法: 在服务器仓库根目录  .venv/bin/python deploy/backfill_mood.py"""
import json
import sys
import urllib.request

sys.path.insert(0, "autotrader")
from storage import DB  # noqa: E402
from common import load_config  # noqa: E402

import yfinance as yf  # noqa: E402

HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",  # 缺这项会被 CNN 拒 (418), 与引擎 _MOOD_HDRS 保持一致
        "Referer": "https://edition.cnn.com/markets/fear-and-greed"}


def main():
    db = DB(load_config()["db_path"])
    print("== yfinance 批量 (15mo) ==")
    df = yf.download(["^VIX", "^VIX3M", "^VVIX", "^SKEW", "HYG", "IEF", "RSP", "SPY"],
                     period="15mo", interval="1d", progress=False, auto_adjust=False)
    close, opn = df["Close"], df["Open"]
    dates = [d.strftime("%Y-%m-%d") for d in close.index]
    prev_spy_close = None
    n = 0
    for i, d in enumerate(dates):
        row = {}
        g = lambda s: (round(float(close[s].iloc[i]), 4)
                       if close[s].iloc[i] == close[s].iloc[i] else None)
        row["vix"], row["vix3m"] = g("^VIX"), g("^VIX3M")
        row["vvix"], row["skew"] = g("^VVIX"), g("^SKEW")
        if g("HYG") and g("IEF"):
            row["hyg_ief"] = round(g("HYG") / g("IEF"), 4)
        if g("RSP") and g("SPY"):
            row["rsp_spy"] = round(g("RSP") / g("SPY"), 4)
        spy_o = opn["SPY"].iloc[i]
        spy_c = close["SPY"].iloc[i]
        if prev_spy_close and spy_o == spy_o and spy_c == spy_c:
            row["spy_on_pct"] = round((float(spy_o) / prev_spy_close - 1) * 100, 3)
            row["spy_id_pct"] = round((float(spy_c) / float(spy_o) - 1) * 100, 3)
        if spy_c == spy_c:
            prev_spy_close = float(spy_c)
        row = {k: v for k, v in row.items() if v is not None}
        if row:
            db.set_mood(d, **row)
            n += 1
    print(f"  写入 {n} 天")
    print("== CNN Fear&Greed 历史 ==")
    try:
        req = urllib.request.Request(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata", headers=HDRS)
        data = json.load(urllib.request.urlopen(req, timeout=20))
        hist = data.get("fear_and_greed_historical", {}).get("data", [])
        m = 0
        for p in hist:
            from datetime import datetime, timezone
            d = datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            db.set_mood(d, fear_greed=round(float(p["y"]), 1))
            m += 1
        print(f"  写入 {m} 点")
    except Exception as e:
        print("  失败(不阻断):", e)
    total = db.conn.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM mood_daily").fetchone()
    print(f"== 完成: mood_daily 共 {total[0]} 天 ({total[1]} ~ {total[2]}) ==")


if __name__ == "__main__":
    main()
