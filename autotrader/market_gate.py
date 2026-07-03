# -*- coding: utf-8 -*-
"""环境闸门: 收盘前用 VIX 水平 + SPY 当日涨跌判定今晚是否开仓。
数据源: yfinance (15分钟延迟对阈值判定足够)。失败时返回 (None, ...) 由引擎决定降级策略。"""
import logging

import yfinance as yf

log = logging.getLogger("gate")


def check_gate(cfg):
    """返回 (passed: bool|None, vix: float, spy_pct: float, reason: str)"""
    g = cfg["gate"]
    if not g.get("enabled", True):
        return True, None, None, "闸门未启用"
    try:
        vix_df = yf.download("^VIX", period="5d", interval="1d", progress=False, auto_adjust=False)
        spy_df = yf.download("SPY", period="5d", interval="1d", progress=False, auto_adjust=True)
        vix_close = vix_df["Close"].squeeze()
        spy_close = spy_df["Close"].squeeze()
        vix = float(vix_close.iloc[-1])
        spy_pct = (float(spy_close.iloc[-1]) / float(spy_close.iloc[-2]) - 1) * 100
    except Exception as e:
        log.error("闸门数据获取失败: %s", e)
        return None, None, None, f"数据获取失败: {e}"

    passed = (vix >= g["vix_min"]) or (spy_pct <= g["spy_max_pct"])
    reason = f"VIX={vix:.1f} (阈值>={g['vix_min']}), SPY当日={spy_pct:+.2f}% (阈值<={g['spy_max_pct']}%)"
    return passed, vix, spy_pct, reason
