# -*- coding: utf-8 -*-
"""选股与买入计划: Finviz 筛选 -> IB 校验价格 -> 平均分配。
移植自 the-trading 1/2/3 号脚本, 合并为一个函数。"""
import logging

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("screener")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}


def fetch_finviz(cfg):
    """返回 [(ticker, change_pct)] 按跌幅升序 (Finviz o=change 已排序)。"""
    url = cfg["screener"]["finviz_url"].format(0)
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="screener_table")
    if table is None:
        raise RuntimeError("Finviz 页面结构变化或被拦截")
    header = [th.get_text(strip=True) for th in table.find("tr").find_all("th")]
    t_i = next(i for i, h in enumerate(header) if h.lower() == "ticker")
    c_i = next(i for i, h in enumerate(header) if h.lower() == "change")
    rows = []
    for tr in table.find_all("tr")[1:]:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if not tds or not tds[0].isdigit():
            continue
        try:
            chg = float(tds[c_i].replace("%", ""))
        except ValueError:
            continue
        rows.append((tds[t_i], chg))
    return rows


async def fetch_ib_scanner(broker, cfg):
    """Finviz 被拒时的后备: IB 市场扫描器 TOP_PERC_LOSE。
    条件对齐 Finviz 筛选 (中大盘 >$2B, 价格 >$15, 高成交), 排序同为跌幅最大在前。
    change_pct 扫描器不直接给出, 置 0 (仅用于展示, 选股只依赖排序)。"""
    from ib_async import ScannerSubscription
    sub = ScannerSubscription(
        instrument="STK", locationCode="STK.US.MAJOR", scanCode="TOP_PERC_LOSE",
        abovePrice=15, aboveVolume=1_000_000, marketCapAbove=2_000_000_000,
        numberOfRows=30)
    rows = await broker.ib.reqScannerDataAsync(sub)
    out = [(r.contractDetails.contract.symbol, 0.0) for r in rows]
    log.info("IB 扫描器候选: %s", [t for t, _ in out[:12]])
    return out


def build_plan(cfg, candidates, prices, budget):
    """candidates: [(ticker, chg)], prices: {ticker: last_price}
    返回 [(ticker, shares, ref_price)] 平均分配, 手数取整。"""
    sc = cfg["screener"]
    picks = candidates[sc["skip_rank"]: sc["skip_rank"] + sc["n_stocks"]]
    picks = [(t, c) for t, c in picks if prices.get(t)]
    if not picks:
        return []
    per_cap_max = budget * cfg["budget"]["per_stock_max_pct"] / 100
    target = min(budget / len(picks), per_cap_max)
    lot = sc["lot_size"]
    plan = []
    spent = 0.0
    for t, _ in picks:
        p = prices[t]
        shares = int(target / p // lot * lot)
        if shares <= 0:
            log.info("跳过 %s: 单价 %.2f 超出单股预算", t, p)
            continue
        plan.append((t, shares, p))
        spent += shares * p
    # 用剩余预算给花费最少的加仓
    remaining = budget - spent
    while plan:
        affordable = [i for i, (t, s, p) in enumerate(plan) if remaining >= p * lot and (s + lot) * p <= per_cap_max]
        if not affordable:
            break
        i = min(affordable, key=lambda i: plan[i][1] * plan[i][2])
        t, s, p = plan[i]
        plan[i] = (t, s + lot, p)
        remaining -= p * lot
    return plan
