# -*- coding: utf-8 -*-
"""股票板块 (GICS sector) 解析。数据源 yfinance, 结果由调用方缓存进 sectors 表,
所以每个 symbol 全局只查一次。取不到时返回空串 (不缓存, 留待下次重试)。"""
import logging

log = logging.getLogger("sectors")

# GICS 板块 -> 代表性 SPDR ETF, 用于"个股独跌 vs 随板块跌"的对照观察
SECTOR_ETF = {
    "Healthcare": "XLV", "Technology": "XLK", "Energy": "XLE",
    "Financial Services": "XLF", "Industrials": "XLI", "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP", "Basic Materials": "XLB",
    "Communication Services": "XLC", "Real Estate": "XLRE", "Utilities": "XLU",
}


def resolve_sector(symbol):
    """返回 GICS 板块名 (如 Healthcare/Energy/Technology), 失败或未知返回 ''。"""
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
        return (info.get("sector") or "").strip()
    except Exception as e:
        log.warning("板块查询失败 %s: %s", symbol, e)
        return ""
