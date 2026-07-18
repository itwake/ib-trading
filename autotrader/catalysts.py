# -*- coding: utf-8 -*-
"""事件硬标签数据源 (T3 观察指标, 全部免费):
- SEC EDGAR: 近 N 日增发/货架类文件 (424B*/S-1/S-3/F-1/F-3/FWP) => 稀释压制风险
  依据: 增发折价定价 + ATM 卖单墙压制反弹 (Bradley-Yuan; 实盘 MDA 案例)
- Nasdaq Trader: 当日停牌/LULD 列表 => 价格发现被延迟, 次日续跌风险 (Kim-Rhee 1997)
SEC 要求请求带可识别 User-Agent; data.sec.gov 限速 ~10 req/s, 调用方自行节流。"""
import logging
import re
from datetime import date, timedelta

import requests

log = logging.getLogger("catalysts")

CIK_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
HALTS_RSS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
DILUTION_PREFIXES = ("424B", "S-1", "S-3", "F-1", "F-3", "FWP")


def is_dilution_form(form):
    f = (form or "").upper().strip()
    return any(f.startswith(p) for p in DILUTION_PREFIXES)


def load_cik_map(ua):
    """{ticker: cik}。约 800KB, 调用方按日缓存。"""
    r = requests.get(CIK_MAP_URL, headers={"User-Agent": ua}, timeout=20)
    r.raise_for_status()
    return {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values()}


def dilution_filings(cik, d, ua, window_days=7):
    """返回 [(form, filing_date)]: d 之前 window_days 内的增发/货架类文件。
    窗口相对 d 计算, 支持历史回填。"""
    r = requests.get(SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": ua}, timeout=15)
    r.raise_for_status()
    recent = (r.json().get("filings") or {}).get("recent") or {}
    forms, dates = recent.get("form") or [], recent.get("filingDate") or []
    lo, hi = str(d - timedelta(days=window_days)), str(d)
    return [(f, dt) for f, dt in zip(forms, dates)
            if lo <= dt <= hi and is_dilution_form(f)]


def parse_halt_symbols(xml_text, d):
    """从 Nasdaq Trader RSS 解析停牌代码集合 (按 HaltDate == d 过滤; 兼容带/不带命名空间)。"""
    us = str(d)
    mdy = f"{d.month:02d}/{d.day:02d}/{d.year}"
    out = set()
    items = re.split(r"<item[ >]", xml_text)[1:]
    for it in items:
        m = re.search(r"<(?:ndaq:)?IssueSymbol>\s*([A-Z.\-]+)\s*</(?:ndaq:)?IssueSymbol>", it)
        dm = re.search(r"<(?:ndaq:)?HaltDate>\s*([^<]+?)\s*</(?:ndaq:)?HaltDate>", it)
        if m and dm and (us in dm.group(1) or mdy in dm.group(1)):
            out.add(m.group(1))
    return out


def todays_halts(d, ua, proxies=None):
    """当日停牌代码集合。RSS 只含当前滚动名单, 无法历史回填。"""
    try:
        r = requests.get(HALTS_RSS_URL, headers={"User-Agent": ua}, timeout=15)
        r.raise_for_status()
        return parse_halt_symbols(r.text, d)
    except Exception as e:
        if proxies:  # 直连被拒时借道代理 (与 Finviz 同口)
            r = requests.get(HALTS_RSS_URL, headers={"User-Agent": ua}, timeout=15, proxies=proxies)
            r.raise_for_status()
            return parse_halt_symbols(r.text, d)
        raise
