# -*- coding: utf-8 -*-
"""选股与买入计划: Finviz 筛选 -> IB 校验价格 -> 平均分配。
移植自 the-trading 1/2/3 号脚本, 合并为一个函数。"""
import logging
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("screener")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"}


def fetch_finviz(cfg, pages=2):
    """返回 ([(ticker, change_pct)], {ticker: extras}) 按跌幅升序 (Finviz o=change 已排序)。
    抓 pages 页 (每页 20 行) 作为递补池; 第 1 页失败抛异常 (触发 IB 扫描器后备),
    后续页失败只降级不阻断。服务器直连 IP 被 Finviz 拒 (403), 配置 screener.proxy
    借道 Windows 上的 Clash 出口。"""
    proxy = cfg["screener"].get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    rows, details, seen = [], {}, set()
    for pg in range(pages):
        url = cfg["screener"]["finviz_url"].format(1 + pg * 20)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20, proxies=proxies)
            resp.raise_for_status()
            page_rows = _parse_page(resp.text)
        except Exception as e:
            if pg == 0:
                raise
            log.warning("Finviz 第 %d 页失败(忽略, 用前 %d 行): %s", pg + 1, len(rows), e)
            break
        if not page_rows:
            break  # 榜单到头
        for t, chg, extras in page_rows:
            if t not in seen:
                seen.add(t)
                rows.append((t, chg))
                details[t] = extras
    return rows, details


def _num(s):
    """Finviz 数值解析: '3.85%'->3.85, '2.5M'->2.5e6, '1.2B'->1.2e9, '-'->None。"""
    s = (s or "").strip().replace(",", "").replace("%", "")
    if not s or s == "-":
        return None
    mult = 1.0
    if s[-1] in "KMB":
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[s[-1]]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


# v=141 表头 -> 观察特征列 (选股质量观测, 2026-07-16)
_EXTRA_COLS = {"Perf Week": "perf_w", "Perf Month": "perf_m", "Perf YTD": "perf_ytd",
               "Perf Year": "perf_y", "Volatility W": "vol_w", "Rel Volume": "relvol",
               "Price": "price"}


def _parse_page(html):
    """返回 [(ticker, chg, extras_dict)]。extras 含 Perf/波动/量比/市值等观察特征,
    全部来自本来就要抓的页面, 零额外请求。"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="screener_table")
    if table is None:
        raise RuntimeError("Finviz 页面结构变化或被拦截")
    header = [th.get_text(strip=True) for th in table.find("tr").find_all("th")]
    t_i = next(i for i, h in enumerate(header) if h.lower() == "ticker")
    c_i = next(i for i, h in enumerate(header) if h.lower() == "change")
    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        tds = [td.get_text(strip=True) for td in cells]
        if not tds or not tds[0].isdigit():
            continue
        try:
            chg = float(tds[c_i].replace("%", ""))
        except ValueError:
            continue
        extras = {}
        for col, key in _EXTRA_COLS.items():
            if col in header:
                v = _num(tds[header.index(col)])
                if v is not None:
                    extras[key] = round(v, 4)
        cap = _num(cells[t_i].get("data-boxover-value") or "")
        if cap:
            extras["cap_b"] = round(cap / 1e9, 2)
        if extras.get("vol_w"):  # 归一化跌幅: 当日跌幅 / 自身周均波动
            extras["zscore"] = round(abs(chg) / extras["vol_w"], 2)
        rows.append((_cell_ticker(cells[t_i]), chg, extras))
    return rows


def _cell_ticker(cell):
    """从 ticker 单元格取干净代码。2026-07 Finviz 改版后单元格含 logo/首字母元素,
    get_text 会串出脏代码 (如 CELC -> 'CCELC', 曾导致误买撞名标的 EELV)。
    优先 data-boxover-ticker 属性, 其次锚点 href 的 t= 参数, 文本仅兜底。"""
    tick = (cell.get("data-boxover-ticker") or "").strip()
    if tick:
        return tick
    a = cell.find("a", href=True)
    if a:
        m = re.search(r"[?&]t=([A-Za-z0-9.\-]+)", a["href"])
        if m:
            return m.group(1)
    return cell.get_text(strip=True)


async def fetch_ib_scanner(broker, cfg):
    """Finviz 被拒时的后备: IB 市场扫描器 TOP_PERC_LOSE。
    条件对齐 Finviz 筛选 (中大盘 >$2B, 价格 >$15, 高成交), 排序同为跌幅最大在前。
    市值须走 filter options (旧字段 marketCapAbove 会被静默取消订阅);
    stockTypeFilter 已被 IB 禁用, 改用返回的 contractDetails.stockType 后置排除 ETF/基金。
    change_pct 扫描器不直接给出, 置 0 (选股只依赖排序)。"""
    from ib_async import ScannerSubscription, TagValue
    sub = ScannerSubscription(
        instrument="STK", locationCode="STK.US.MAJOR", scanCode="TOP_PERC_LOSE",
        abovePrice=15, aboveVolume=1_000_000, numberOfRows=30)
    filt = [TagValue("marketCapAbove1e6", "2000")]
    rows = await broker.ib.reqScannerDataAsync(sub, scannerSubscriptionFilterOptions=filt)
    # 扫描结果的 contractDetails 元数据为空, 需逐个补查才能拿到 stockType 排除 ETF/基金
    from ib_async import Stock
    out = []
    for r in rows[:18]:
        sym = r.contractDetails.contract.symbol
        try:
            cds = await broker.ib.reqContractDetailsAsync(Stock(sym, "SMART", "USD"))
            st = (cds[0].stockType or "").upper() if cds else ""
        except Exception:
            st = ""
        if any(x in st for x in ("ETF", "ETN", "FUND")):
            log.info("排除 %s (stockType=%s)", sym, st)
            continue
        out.append((sym, 0.0))
    log.info("IB 扫描器候选: %s", [t for t, _ in out[:12]])
    return out


def build_plan(cfg, candidates, prices, budget):
    """candidates: [(ticker, chg)] 已按跌幅排序, prices: {ticker: last_price}
    返回 [(ticker, shares, ref_price)]。
    从 skip_rank 之后按名次顺序遴选: 无价 / 配不进单股配额的跳过, 由后位递补,
    凑满 n_stocks 或候选耗尽为止 (2026-07-15 用户决定: 补齐到 N, 总资金不变)。
    单股配额固定 = min(预算/n_stocks, 单股上限) — 递补不改变每只的资金配置。"""
    sc = cfg["screener"]
    n = sc["n_stocks"]
    if n <= 0:
        return []
    per_cap_max = budget * cfg["budget"]["per_stock_max_pct"] / 100
    target = min(budget / n, per_cap_max)
    lot = sc["lot_size"]
    plan = []
    spent = 0.0
    for t, _ in candidates[sc["skip_rank"]:]:
        if len(plan) >= n:
            break
        p = prices.get(t)
        if not p:
            continue
        shares = int(target / p // lot * lot)
        if shares <= 0:
            log.info("跳过 %s: 单价 %.2f 超出单股配额, 由后位递补", t, p)
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
