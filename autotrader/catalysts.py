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
# FWP 不在内: 结构化票据营销件 (大银行每日数份), 真实股权增发必伴随 424B 主文件
DILUTION_PREFIXES = ("424B", "S-1", "S-3", "F-1", "F-3")
# 一周内 ≥ 此数量的招股类文件 = 结构化票据/中票发行程序 (如花旗一周 30 份),
# 真实股权增发一周只有 1~3 份 —— 视为噪音不打标
SHELF_PROGRAM_NOISE_N = 6


def is_dilution_form(form):
    f = (form or "").upper().strip()
    # 424B2 排除: 大金融机构的结构化票据说明书日常滚动发行 (如花旗几乎每日 424B2),
    # 不是股权稀释; 真正的股权增发用 424B5/424B4/424B3 (实测 07-14 花旗误报案例)
    if f == "424B2":
        return False
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
    hits = [(f, dt) for f, dt in zip(forms, dates)
            if lo <= dt <= hi and is_dilution_form(f)]
    if len(hits) >= SHELF_PROGRAM_NOISE_N:
        log.info("CIK %s: 窗口内 %d 份招股类文件, 判定为票据发行程序噪音, 不打标", cik, len(hits))
        return []
    return hits


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
