# -*- coding: utf-8 -*-
"""每日状态机: 按日历时刻表驱动 买入->隔夜卖->盘前卖->开盘追踪->日报。
lot 生命周期: FILLED -> OVERNIGHT -> PREMARKET -> TRAILING -> CLOSED
"""
import asyncio
import logging
from datetime import timedelta

import calendar_util as cal
from broker import Broker, round_tick
from common import now_et
from market_gate import check_gate
from notify import Notifier
from screener import build_plan, fetch_finviz
from storage import DB

log = logging.getLogger("engine")


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db = DB(cfg["db_path"])
        self.notify = Notifier(cfg)
        self.broker = Broker(cfg)
        self.plan = []

    # ---------- 各阶段动作 ----------
    async def do_gate_check(self, d):
        if self.db.get_control("pause_buys") == "1":
            self.notify.send(f"[{d}] 开仓已被手动暂停 (面板开关), 今晚不买入。卖出链正常。", "warn")
            self.db.record_run(str(d), False, 0, 0, 0, 0, "手动暂停")
            return False
        passed, vix, spy, reason = check_gate(self.cfg)
        if passed is None:
            self.notify.send(f"闸门数据失败, 今晚默认不开仓。{reason}", "warn")
            passed = False
        tag = "放行 ✅" if passed else "拦截 ⛔"
        self.notify.send(f"[{d}] 环境闸门: {tag}  {reason}")
        self.db.record_run(str(d), passed, vix or 0, spy or 0, 0, 0, reason)
        # 影子判定单独落列 (闸门停用时也记), 供"如果开着闸门"的实盘对比
        if vix is not None and spy is not None:
            g = self.cfg["gate"]
            self.db.set_gate_shadow(str(d), 1 if (vix >= g["vix_min"] or spy <= g["spy_max_pct"]) else 0)
        return passed

    async def do_build_plan(self, d):
        acct = await self.broker.account()
        netliq, gross = acct["NetLiquidation"], acct["GrossPositionValue"]
        avail = acct["AvailableFunds"]
        b = self.cfg["budget"]
        budget = min(b["nightly_max_usd"], max(0.0, b["gross_max_x_netliq"] * netliq - gross))
        if avail < b["min_available_funds_usd"]:
            self.notify.send(f"可用资金 ${avail:,.0f} 低于阈值, 今晚不开仓", "warn")
            return False
        try:
            candidates, cand_details = fetch_finviz(self.cfg)
            src = "Finviz"
        except Exception as e:
            self.notify.send(f"Finviz 失败({str(e)[:80]}), 切换 IB 扫描器后备", "warn")
            from screener import fetch_ib_scanner
            candidates = await fetch_ib_scanner(self.broker, self.cfg)
            cand_details = {}
            src = "IB扫描器"
        sc = self.cfg["screener"]
        depth = sc["skip_rank"] + sc["n_stocks"] + 15  # 递补池深度: 主窗口 + 15 个候补
        try:  # 只买普通股: 排除 ETF/ETN/基金 (Finviz 偶发混入 + 2026-07-15 脏码撞出合法 ETF 的教训)
            candidates = await self._exclude_non_common(candidates, limit=depth)
        except Exception as e:
            log.warning("标的类型过滤失败(不阻断): %s", e)
        prices = await self.broker.last_prices([t for t, _ in candidates[:depth]])
        self.plan = build_plan(self.cfg, candidates, prices, budget)
        # 失效防护: 候选窗口是满的、却几乎全部无法在 IB 定价 => 选股数据疑似损坏
        # (2026-07-15 事故: Finviz 改版致代码解析出错, 唯一"撞上真名"的 EELV 被误买)。
        # 宁可空仓一晚, 不买垃圾数据选出的票。
        window = candidates[sc["skip_rank"]: sc["skip_rank"] + sc["n_stocks"]]
        if len(window) >= sc["n_stocks"] and len(self.plan) <= max(2, sc["n_stocks"] // 3):
            self.notify.send(f"🚨 [{d}] 候选 {len(window)} 只但仅 {len(self.plan)} 只可在 IB 定价 — "
                             f"选股数据疑似异常 (解析/行情故障), 今晚放弃买入", "critical")
            self.db.record_run(str(d), True, 0, 0, 0, budget, "选股数据异常, 放弃买入")
            self.plan = []
            return False
        rank = {t: i + 1 for i, (t, _) in enumerate(candidates)}
        main_end = sc["skip_rank"] + sc["n_stocks"]
        lines = [f"{t} x{s} @~{p:.2f} (${s * p:,.0f})"
                 + (f" [递补·第{rank[t]}名]" if rank.get(t, 0) > main_end else "")
                 for t, s, p in self.plan]
        if len(self.plan) < sc["n_stocks"]:
            lines.append(f"⚠️ 候选耗尽, 仅凑到 {len(self.plan)}/{sc['n_stocks']} 只")
        self.notify.send(f"[{d}] 买入计划({src}) 预算 ${budget:,.0f} (NetLiq ${netliq:,.0f}):\n" + "\n".join(lines))
        self._earnings_watch(d)  # 仅观察不过滤 (2026-07-06 决定: 样本 21 笔不足以立规则)
        self.db.record_run(str(d), True, 0, 0, len(self.plan), budget, "plan built")
        # 候选追踪的登记 (板块/特征打标, 网络慢) 移出买入关键路径, 由日报执行
        self._watch_candidates = list(candidates)
        self._watch_details = dict(cand_details)
        return bool(self.plan)

    async def _record_watchlist(self, d, candidates, details=None):
        n = int(self.cfg["screener"].get("watch_n", 20))  # 0=关闭
        if n <= 0:
            return
        from sectors import resolve_sector
        bought = {t for t, _s, _p in self.plan}
        head = candidates[:n]
        rows = []
        for i, (t, chg) in enumerate(head, start=1):
            sec = self.db.get_sector(t)
            if not sec:  # None(未查过) 或 ''(上次没取到): 重试, 只缓存非空结果
                got = resolve_sector(t)
                if got:
                    self.db.set_sector(t, got)
                sec = got
            rows.append((str(d), i, t, sec or "", chg, 1 if t in bought else 0))
        if rows:
            self.db.add_watch(rows)
            log.info("[%s] 候选追踪登记 %d 名 (其中买入 %d)", d, len(rows), sum(r[5] for r in rows))
        # ---- 观察特征打标 (每步独立降级, 任何一步失败不影响其他) ----
        import time as _time
        log.info("[%s] 特征打标开始: %d 候选, finviz明细 %d 条", d, len(head), len(details or {}))
        for step, fn in (("finviz特征", lambda: self._tag_finviz(d, head, details or {})),
                         ("趋势形态", lambda: self._tag_trend_shape(d, head)),
                         ("板块对照", lambda: self._tag_sector_rel(d, head)),
                         ("财报标签", lambda: self._tag_earnings(d, head)),
                         ("做空比例", lambda: self._tag_short_interest(d, head)),
                         ("增发检索", lambda: self._tag_dilution(d, head)),
                         ("停牌检索", lambda: self._tag_halts(d, head)),
                         ("夜间环境", lambda: self._tag_night_env(d, candidates)),
                         ("异动归因", lambda: self._tag_news_pulse(d, head))):
            t0 = _time.time()
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
                log.info("特征打标[%s] 完成 %.1fs", step, _time.time() - t0)
            except Exception as e:
                log.warning("候选特征[%s]失败: %s", step, e)

    def _tag_finviz(self, d, head, details):
        """Finviz 页面自带特征 (零额外请求): Perf 系列/周波动/量比/价格/市值/归一化跌幅。"""
        for t, _ in head:
            ex = details.get(t)
            if ex:
                self.db.set_watch_features(str(d), t, **ex)

    def _tag_trend_shape(self, d, head):
        """趋势位置 (200日均线/52周高) 与当日K线形态 (跳空占比/收盘位置 CLV)。
        依据: George-Hwang 2004 (趋势中回调优于破位); End-of-Day Reversal 2024
        (收在最低附近的下跌反弹更强)。一次批量下载 1 年日线。"""
        import yfinance as yf
        syms = [t for t, _ in head]
        df = yf.download(syms, period="1y", interval="1d", progress=False,
                         auto_adjust=False, group_by="ticker", threads=True)
        for t, _ in head:
            try:
                sub = df[t] if df.columns.nlevels > 1 else df
                sub = sub.dropna(subset=["Close"])
                dates = [str(x.date()) for x in sub.index]
                if str(d) not in dates:
                    continue  # 当日日线还没出, 留待次日
                i = dates.index(str(d))
                if i < 1:
                    continue
                o, h, l, c = (float(sub[k].iloc[i]) for k in ("Open", "High", "Low", "Close"))
                prev_c = float(sub["Close"].iloc[i - 1])
                closes = sub["Close"].iloc[:i + 1]
                feats = {
                    "gap_pct": round((o / prev_c - 1) * 100, 2),
                    "clv": round((c - l) / (h - l), 3) if h > l else None,
                    "vs_52w_high": round((c / float(sub["High"].iloc[:i + 1].max()) - 1) * 100, 1),
                }
                if len(closes) >= 200:
                    feats["vs_200dma"] = round((c / float(closes.iloc[-200:].mean()) - 1) * 100, 1)
                if len(closes) >= 50:  # FOMO 态判定用 (>50日线30%+, 与回测口径一致)
                    feats["vs_50dma"] = round((c / float(closes.iloc[-50:].mean()) - 1) * 100, 1)
                self.db.set_watch_features(str(d), t, **feats)
            except Exception as e:
                log.warning("趋势形态失败 %s: %s", t, e)

    def _tag_sector_rel(self, d, head):
        """个股独跌 vs 随板块跌: 当日跌幅 − 所属板块 ETF 当日涨跌。
        依据: 板块性事件的相关性风险 (IONS 传染案例), 独跌更可能是个股信息。"""
        import yfinance as yf
        from sectors import SECTOR_ETF
        secs = {t: self.db.get_sector(t) for t, _ in head}
        etfs = sorted({SECTOR_ETF[s] for s in secs.values() if s in SECTOR_ETF})
        if not etfs:
            return
        df = yf.download(etfs, period="1mo", interval="1d", progress=False,
                         auto_adjust=False, group_by="ticker", threads=True)
        etf_chg = {}
        for e in etfs:
            try:
                sub = (df[e] if df.columns.nlevels > 1 else df).dropna(subset=["Close"])
                dates = [str(x.date()) for x in sub.index]
                i = dates.index(str(d))
                if i >= 1:
                    etf_chg[e] = round((float(sub["Close"].iloc[i]) / float(sub["Close"].iloc[i - 1]) - 1) * 100, 2)
            except Exception:
                pass
        for t, chg in head:
            e = SECTOR_ETF.get(secs.get(t) or "")
            if e in etf_chg:
                self.db.set_watch_features(str(d), t, sector_chg=etf_chg[e],
                                           rel_drop=round(chg - etf_chg[e], 2))

    def _tag_earnings(self, d, head):
        """财报关联标签: earn_recent=当日/前一交易日出过财报 (下跌大概率信息性, PEAD 续跌);
        earn_next=次日财报 (持仓期内二元事件)。依据: 全年审计财报单净 -$833 + PEAD 文献。"""
        import yfinance as yf
        prev = d
        for _ in range(4):  # 找前一交易日
            prev = prev - timedelta(days=1)
            if cal.is_trading_day(prev):
                break
        nxt = cal.next_trading_day(d)
        for t, _ in head:
            try:
                edates = yf.Ticker(t).get_earnings_dates(limit=8)
                if edates is None or edates.empty:
                    continue
                ds = {str(x.date()) for x in edates.index}
                self.db.set_watch_features(str(d), t,
                                           earn_recent=1 if {str(d), str(prev)} & ds else 0,
                                           earn_next=1 if str(nxt) in ds else 0)
            except Exception as e:
                log.warning("财报标签失败 %s: %s", t, e)

    def _tag_short_interest(self, d, head):
        """做空比例 (% of float)。依据: Boehmer 2008 高SI大跌更可能是知情做空, 续跌风险高;
        观察阈值 15%。"""
        import yfinance as yf
        for t, _ in head:
            try:
                v = (yf.Ticker(t).info or {}).get("shortPercentOfFloat")
                if v is not None:
                    self.db.set_watch_features(str(d), t, si_pct=round(float(v) * 100, 1))
            except Exception as e:
                log.warning("做空比例失败 %s: %s", t, e)

    _SEC_UA = "ibtrading-observer xiaoj313@gmail.com"

    def _tag_dilution(self, d, head):
        """增发/货架标签: d 前 7 日内提交过 424B*/S-1/S-3/F-1/F-3/FWP = 稀释压制风险
        (增发折价定价 + ATM 卖单墙, MDA 案例)。数据: SEC EDGAR (免费)。"""
        import time
        import catalysts
        today = str(now_et().date())
        if getattr(self, "_cik_day", None) != today:
            self._cik_map = catalysts.load_cik_map(self._SEC_UA)
            self._cik_day = today
        for t, _ in head:
            cik = self._cik_map.get(t.upper())
            if not cik:
                continue
            try:
                hits = catalysts.dilution_filings(cik, d, self._SEC_UA)
                self.db.set_watch_features(str(d), t, dilution=1 if hits else 0)
                if hits:
                    log.info("增发标签 %s: %s", t, hits[:3])
            except Exception as e:
                log.warning("增发检索失败 %s: %s", t, e)
            time.sleep(0.15)  # SEC 限速礼貌间隔

    def _tag_halts(self, d, head):
        """停牌/LULD 标签: 当日出现在 Nasdaq 停牌名单 = 价格发现被延迟, 次日续跌风险
        (Kim-Rhee 1997)。RSS 只含当日, 无法回填历史。"""
        import catalysts
        proxy = self.cfg["screener"].get("proxy") or None
        proxies = {"http": proxy, "https": proxy} if proxy else None
        halted = catalysts.todays_halts(d, self._SEC_UA, proxies)
        for t, _ in head:
            self.db.set_watch_features(str(d), t, halted=1 if t.upper() in halted else 0)
        if halted:
            log.info("[%s] 当日停牌名单命中候选: %s", d, sorted(halted & {t for t, _ in head}) or "无")

    NEWS_PROMPT = (
        '{sym} (US stock) fell {chg}% on {d} during regular US trading. Determine why, using web '
        'search with at least 2 different queries. Classify: 1=company-specific hard event that day '
        'or the prior evening (subtype one of: earnings/guidance/downgrade/short-report/clinical/'
        'offering/legal/contract/other), 2=sector or market-wide selloff with NO company-specific '
        'news, 3=no news found. Discipline: never invent a reason; if searches surface nothing '
        'company-specific for that date, answer 2 or 3. Reply ONLY a JSON object: '
        '{{"class":1|2|3,"type":"...","confidence":"A|B|C","reason":"one sentence"}}')

    async def _tag_news_pulse(self, d, head):
        """异动归因 (news-pulse, 维度13): 用服务器上已授权的 codex CLI + 网络搜索,
        判定每只候选的大跌是 ①个股硬事件 ②板块联动 ③查无消息。
        依据: Chan 2003 (有消息大跌→续跌, 无消息→反弹) —— 证据链最强的分类器;
        与硬标签 (财报/增发/停牌) 的一致性可反查归因质量。观察不拦截, 逐票降级。"""
        import json as _json
        import re as _re
        sem = asyncio.Semaphore(3)
        conf_map = {"A": 3, "B": 2, "C": 1}

        async def one(t, chg):
            prompt = self.NEWS_PROMPT.format(sym=t, chg=chg, d=d)
            async with sem:
                try:
                    p = await asyncio.create_subprocess_exec(
                        "codex", "exec", "--skip-git-repo-check", "-s", "read-only",
                        "-c", "tools.web_search=true", prompt,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        cwd="/tmp")
                    out, _ = await asyncio.wait_for(p.communicate(), timeout=150)
                    m = _re.findall(r'\{[^{}]*"class"[^{}]*\}', out.decode("utf-8", "replace"))
                    if not m:
                        log.warning("异动归因无输出 %s", t)
                        return
                    j = _json.loads(m[-1])
                    self.db.set_watch_features(
                        str(d), t, news_class=int(j["class"]),
                        news_conf=conf_map.get(str(j.get("confidence", "C")).upper(), 1),
                        news_type=str(j.get("type", ""))[:24],
                        news_reason=str(j.get("reason", ""))[:200])
                except Exception as e:
                    log.warning("异动归因失败 %s: %s", t, e)

        await asyncio.gather(*(one(t, chg) for t, chg in head))

    def _tag_night_env(self, d, candidates):
        """夜间环境: VIX3M 期限结构 (VIX/VIX3M>1=近端恐慌, Nagel 2012 反转收益随之上升)
        + 当晚榜单宽度 (榜单又短又浅=平静市, 历史弱环境)。"""
        import yfinance as yf
        vix3m = None
        try:
            s = yf.download("^VIX3M", period="7d", interval="1d", progress=False,
                            auto_adjust=False)["Close"].squeeze().dropna()
            vix3m = round(float(s.iloc[-1]), 2)
        except Exception as e:
            log.warning("VIX3M 获取失败: %s", e)
        drops = [chg for _, chg in candidates if chg is not None]
        self.db.set_night_env(str(d), vix3m=vix3m, cand_n=len(candidates),
                              cand_avg_drop=round(sum(drops) / len(drops), 2) if drops else None)

    def _ensure_sectors(self, symbols):
        """给一批 symbol 补全板块 (缓存进 sectors 表), 供台账/历史/持仓表显示。
        在 daily_report 里对当日持有/平仓标的调用, 兜住 watch_n=0 时买入票没板块的情况。"""
        from sectors import resolve_sector
        for s in symbols:
            if not self.db.get_sector(s):
                got = resolve_sector(s)
                if got:
                    self.db.set_sector(s, got)

    async def _exclude_non_common(self, candidates, limit=15):
        """从将被定价的前 limit 个候选里排除非普通股 (ETF/ETN/FUND), 并行查询。
        IB 扫描器后备路径已有同款过滤, 此处补齐 Finviz 主路径。"""
        head = candidates[:limit]
        types = await asyncio.gather(*(self.broker.stock_type(t) for t, _ in head))
        out, dropped = [], []
        for (t, chg), st in zip(head, types):
            if st and any(x in st for x in ("ETF", "ETN", "FUND")):
                dropped.append(f"{t}({st})")
                continue
            out.append((t, chg))
        if dropped:
            self.notify.send("已排除非普通股: " + ", ".join(dropped), "warn")
        return out + list(candidates[limit:])

    def _earnings_watch(self, d):
        """观察性标记: 计划里哪些票在持仓期内(今晚AMC/明晨BMO)发布财报。只播报留痕, 不拦截。"""
        try:
            import yfinance as yf
            nxt = str(cal.next_trading_day(d))
            hits = []
            for t, _s, _p in self.plan:
                try:
                    c = yf.Ticker(t).calendar
                    eds = c.get("Earnings Date") if isinstance(c, dict) else None
                    if eds and str(eds[0]) in (str(d), nxt):
                        hits.append(f"{t}({eds[0]})")
                except Exception:
                    pass
            if hits:
                self.notify.send("👁 财报暴露观察(未过滤): " + ", ".join(hits) +
                                 " 将在持仓期内发布财报 — 历史上此类 21 笔净 -$833", "warn")
                self.db.event("earnings_watch", ", ".join(hits))
        except Exception as e:
            log.warning("财报观察失败: %s", e)

    async def do_submit_moc(self, d):
        ok, fails = 0, []
        for t, shares, ref in self.plan:
            trade = await self.broker.buy_moc(t, shares)
            self.db.record_order(getattr(getattr(trade, "order", None), "orderId", -1) if trade else -1,
                                 0, "MOC_BUY", t, shares, 0, "submitted")
            if trade is not None or self.broker.dry:  # dry 模式 _send 恒返 None, 视为成功
                ok += 1
            else:
                fails.append(t)
        msg = f"[{d}] 已提交 {ok}/{len(self.plan)} 个 MOC 买单"
        if fails:
            msg += "；提交失败: " + ", ".join(fails)
        self.notify.send(msg, "warn" if fails else "info")

    def _persist_fills(self, fills):
        """所有成交按 execId 固化进 fills 表 (跨会话累积)。reqExecutions 只返回网关时区
        '当日午夜以来'的成交, 上海时区网关的午夜=12:00 ET, 上午的成交过午即不可见——
        固化后对账不再依赖单次查询窗口。"""
        for f in fills:
            e = f.execution
            try:
                ts = e.time.astimezone(now_et().tzinfo).isoformat(timespec="seconds")
            except Exception:
                ts = now_et().isoformat(timespec="seconds")
            self.db.record_fill(e.execId, ts, f.contract.symbol, e.side,
                                float(e.shares), float(e.price))

    async def do_confirm_fills(self, d):
        fills = await self.broker.todays_fills()
        self._persist_fills(fills)
        target_mult = 1 + self.cfg["exits"]["overnight_target_pct"] / 100
        # 同一 MOC 单可能分多笔部分成交回报 (2026-07-15: EELV 一单 15 笔), 按 symbol
        # 聚合成一个 lot (vwap), 否则台账碎片化且卖出链会挂一堆小单白付佣金。
        agg = {}
        for f in fills:
            e, c = f.execution, f.contract
            if e.side != "BOT" or self.db.exec_seen(e.execId):
                continue
            a = agg.setdefault(c.symbol, [0.0, 0.0, []])
            a[0] += float(e.shares)
            a[1] += float(e.shares) * float(e.price)
            a[2].append(e.execId)
        n_execs = 0
        for sym, (qty, cash, exec_ids) in sorted(agg.items()):
            vwap = cash / qty
            self.db.add_lot(sym, str(d), int(qty), round(vwap, 4),
                            round_tick(vwap * target_mult))
            for eid in exec_ids:
                self.db.mark_exec(eid)
            n_execs += len(exec_ids)
        self.notify.send(f"[{d}] 成交确认: {len(agg)} 个新买入 lot 已入台账 ({n_execs} 笔成交)")
        # 计划 vs 成交缺口告警: 拒单/停牌导致 MOC 未成交时, 这里是唯一能发现的地方。
        # 只在本次有新成交、或计划>0 却一笔未成时告警 (16:2x 的重跑不重复报)。
        planned, filled = self.db.planned_on(str(d)), self.db.lot_count_on(str(d))
        if planned and filled < planned and (n_execs > 0 or filled == 0):
            self.notify.send(f"⚠️ [{d}] 计划 {planned} 只、实际成交 {filled} 只 — "
                             f"缺 {planned - filled} 只, 检查拒单/停牌/竞价未成交", "warn")

    async def _sell_price_for(self, lot):
        """智能挂价: 市价已超目标则跟随抬价 (不超过 ask), 否则用目标价。"""
        target = lot["target_price"]
        ex = self.cfg["exits"]
        if not ex.get("follow_market", True):
            return target, "TARGET"
        c = await self.broker.qualify(lot["symbol"])
        if not c:
            return target, "TARGET"
        bid, ask, last = await self.broker.market_ref(c)
        from broker import smart_limit
        price, reason = smart_limit(target, bid, ask, last, ex.get("follow_buffer_pct", 0.1))
        return price, reason

    async def _staged_sell(self, lot, place_fn, kind, ctx, skips=None):
        """基于共享快照的防超卖检查 + 智能定价。返回描述行或 None(跳过)。"""
        qty, pos, pending = self.broker.sellable_from(ctx, lot["symbol"], lot["qty"])
        if qty <= 0:
            if skips is not None:
                skips.append(f"{lot['symbol']}(持仓{pos}/在途{pending})")
            return None
        price, reason = await self._sell_price_for(lot)
        t = await place_fn(lot["symbol"], qty, price)
        self.broker.ctx_add_pending(ctx, lot["symbol"], qty)
        self.db.record_order(getattr(getattr(t, "order", None), "orderId", -1) if t else -1,
                             lot["lot_id"], kind, lot["symbol"], qty, price, "submitted", reason)
        shrink = f" (缩量, 持仓{pos}/在途{pending})" if qty < lot["qty"] else ""
        return f"{lot['symbol']} x{qty} @{price} [{reason}]{shrink}"

    async def do_overnight_sells(self, d):
        lines, skips = [], []
        ctx = await self.broker.sell_context()
        # 含 TRAILING: 当日追踪单未成交 (tif=DAY 收盘作废) 的 lot 由隔夜单重新接管, 防孤儿仓
        for lot in self.db.open_lots():
            if lot["state"] not in ("FILLED", "OVERNIGHT", "TRAILING"):
                continue
            line = await self._staged_sell(lot, self.broker.sell_overnight, "OVERNIGHT_SELL", ctx, skips)
            if line:
                self.db.set_lot_state(lot["lot_id"], "OVERNIGHT")
                lines.append(line)
        if lines:
            self.notify.send("隔夜限价卖单:\n" + "\n".join(lines))
        if skips:
            self.notify.send("防超卖跳过(已有在途卖单/手动单): " + ", ".join(skips), "warn")

    async def _cancel_sells_and_wait(self, symbols, rounds=8):
        """撤系统自己的卖单, 并轮询直到这些票的在途卖量清零 (或超时) 后返回快照。
        固定短等待不可靠: 隔夜僵尸单撤销传播慢 (2026-07-20 盘前 6 只被跳过)、跨客户端
        撤单 (面板引擎 clientId+2 的单) 更慢 (同日 10:00 又 4 只被跳过)。
        手动 (非 autotrader) 卖单不会被撤、也不该清零 -> 超时后按防超卖正常缩量/跳过。"""
        syms = sorted(set(symbols))
        for s in syms:
            await self.broker.cancel_open_sells(s)
        ctx = None
        for i in range(rounds):
            await asyncio.sleep(1 if i == 0 else 2)
            ctx = await self.broker.sell_context()
            if not any(ctx[1].get(s, 0) for s in syms):
                return ctx
        left = {s: ctx[1].get(s) for s in syms if ctx[1].get(s)}
        log.warning("撤单等待超时, 仍有在途 (可能为手动单, 将按防超卖处理): %s", left)
        return ctx

    async def do_premarket_sells(self, d):
        await self._resync_lots_with_positions("盘前")
        lines, skips = [], []
        lots = [l for l in self.db.open_lots() if l["state"] in ("FILLED", "OVERNIGHT", "TRAILING")]
        ctx = await self._cancel_sells_and_wait([l["symbol"] for l in lots]) \
            if lots else await self.broker.sell_context()
        for lot in lots:
            line = await self._staged_sell(lot, self.broker.sell_premarket, "PREMARKET_SELL", ctx, skips)
            if line:
                self.db.set_lot_state(lot["lot_id"], "PREMARKET")
                lines.append(line)
        if lines:
            self.notify.send("盘前限价卖单:\n" + "\n".join(lines))
        if skips:
            self.notify.send("防超卖跳过(已有在途卖单/手动单): " + ", ".join(skips), "warn")

    async def do_open_trail(self, d):
        await self._resync_lots_with_positions("开盘")
        lines, skips = [], []
        lots = [l for l in self.db.open_lots() if l["state"] in ("FILLED", "OVERNIGHT", "PREMARKET")]
        ctx = await self._cancel_sells_and_wait([l["symbol"] for l in lots]) \
            if lots else await self.broker.sell_context()
        for lot in lots:
            qty, pos, pending = self.broker.sellable_from(ctx, lot["symbol"], lot["qty"])
            if qty <= 0:
                skips.append(f"{lot['symbol']}(持仓{pos}/在途{pending})")
                continue
            t = await self.broker.sell_trail(lot["symbol"], qty, self.cfg["exits"]["trail_pct"])
            self.broker.ctx_add_pending(ctx, lot["symbol"], qty)
            self.db.set_lot_state(lot["lot_id"], "TRAILING")
            self.db.record_order(getattr(getattr(t, "order", None), "orderId", -1) if t else -1,
                                 lot["lot_id"], "TRAIL_SELL", lot["symbol"], qty, 0,
                                 "submitted", f"trail {self.cfg['exits']['trail_pct']}%")
            lines.append(f"{lot['symbol']} x{qty}" + (f" (缩量)" if qty < lot["qty"] else ""))
        if lines:
            self.notify.send(f"开盘追踪卖出已挂 ({self.cfg['exits']['trail_pct']}%):\n" + "\n".join(lines))
        if skips:
            self.notify.send("防超卖跳过(已有在途卖单/手动单): " + ", ".join(skips), "warn")

    async def do_daily_report(self, d):
        await self._resync_lots_with_positions("日报")
        acct = await self.broker.account()
        pos = await self.broker.positions()
        left = self.db.open_lots()
        realized = self.db.realized_on(str(d))
        msg = (f"[{d}] 日报: 当日已实现 ${realized:+,.0f} | NetLiq ${acct['NetLiquidation']:,.0f} | "
               f"现金 ${acct['TotalCashValue']:,.0f} | 持仓 ${acct['GrossPositionValue']:,.0f} ({len(pos)} 只) | "
               f"台账未平 lot {len(left)}")
        if left and any(l["state"] == "TRAILING" for l in left):
            msg += "\n⚠️ 有 TRAILING 状态 lot 未平 — 检查是否停牌/未成交"
        self.notify.send(msg)
        self.db.snapshot(str(d), acct["NetLiquidation"], acct["TotalCashValue"],
                         acct["GrossPositionValue"], acct["AvailableFunds"], len(pos), realized)
        try:  # 板块补全: 当日持有/平仓标的, 兜住 watch_n=0 的买入票
            self._ensure_sectors(self.db.symbols_for_bars(str(d)))
        except Exception as e:
            log.warning("板块补全失败: %s", e)
        watch_syms = []
        try:  # 观测1: 登记今晚候选 + 特征打标 (从买入关键路径移来; add_watch 幂等)
            cands = getattr(self, "_watch_candidates", None)
            if cands:
                await self._record_watchlist(d, cands, getattr(self, "_watch_details", {}))
                watch_syms = [t for t, _ in cands[:int(self.cfg["screener"].get("watch_n", 20))]]
                self._watch_candidates = None
        except Exception as e:
            log.warning("候选追踪登记失败: %s", e)
        try:  # 观测2: 1 分钟K线 (持仓/平仓标的 + 今晚候选), 候选顺带算尾盘形态特征
            await self._capture_minute_bars(d, extra_syms=watch_syms)
        except Exception as e:
            log.warning("分钟线抓取失败: %s", e)
        try:  # 观测3: 回填历史候选 (含未买入) 的次日结果
            self._eval_watchlist(d)
        except Exception as e:
            log.warning("候选追踪回填失败: %s", e)

    async def _capture_minute_bars(self, d, extra_syms=()):
        """当日 RTH 1分钟K线存档: 持仓/平仓标的 (卖出时机复盘) + 候选 (尾盘形态观察)。
        候选顺带计算 last30_pct (最后30分钟涨跌): 尾盘加速下跌的反弹证据最强
        (End-of-Day Reversal, SSRN 2024)。"""
        import json
        extra = set(extra_syms)
        syms = list(dict.fromkeys(self.db.symbols_for_bars(str(d)) + list(extra_syms)))
        n = 0
        for sym in syms:
            try:
                bars = await self.broker.minute_bars(sym)
                if bars:
                    self.db.save_minute_bars(str(d), sym, json.dumps(bars, separators=(",", ":")))
                    n += 1
                    if sym in extra and len(bars) >= 31:
                        c30, clast = bars[-31][4], bars[-1][4]
                        if c30:
                            self.db.set_watch_features(str(d), sym,
                                                       last30_pct=round((clast / c30 - 1) * 100, 2))
            except Exception as e:
                log.warning("分钟线失败 %s: %s", sym, e)
        if n:
            log.info("[%s] 分钟线已存 %d/%d 只", d, n, len(syms))

    def _eval_watchlist(self, d):
        """用日线回填候选的次日结果。口径与实盘近似: 次日最高触及 收盘×(1+目标%) 记为
        止盈命中(+目标%), 否则按次日收盘价计算收益。"""
        rows = self.db.watch_pending(str(d))
        if not rows:
            return
        import yfinance as yf
        syms = sorted({r["symbol"] for r in rows})
        start = min(r["date"] for r in rows)
        df = yf.download(syms, start=start, progress=False, auto_adjust=False, group_by="ticker")
        tgt_pct = self.cfg["exits"]["overnight_target_pct"]
        done = 0
        for r in rows:
            try:
                # group_by="ticker" 对单一 symbol 也返回两层列, 按列层级判断
                sub = df[r["symbol"]] if df.columns.nlevels > 1 else df
                sub = sub.dropna(subset=["Close"])
                dates = [str(x.date()) for x in sub.index]
                if r["date"] not in dates:
                    continue
                i = dates.index(r["date"])
                if i + 1 >= len(sub):
                    continue  # 次日数据未出, 下次再算
                entry_close = float(sub["Close"].iloc[i])
                nxt = sub.iloc[i + 1]
                hit = 1 if float(nxt["High"]) >= entry_close * (1 + tgt_pct / 100) else 0
                ret = tgt_pct if hit else (float(nxt["Close"]) / entry_close - 1) * 100
                self.db.set_watch_outcome(r["date"], r["symbol"], round(entry_close, 4),
                                          float(nxt["Open"]), float(nxt["High"]),
                                          float(nxt["Close"]), hit, round(ret, 2))
                done += 1
            except Exception as e:
                log.warning("候选回填失败 %s %s: %s", r["date"], r["symbol"], e)
        if done:
            log.info("[%s] 候选追踪回填 %d 笔", d, done)

    async def _resync_lots_with_positions(self, tag):
        """以 IB 实际持仓为准核对台账。成交先固化进 fills 表, 平仓价优先用累积成交回填;
        查不到成交时用行情价估算 (exit_how=resync-est, 待 Flex 对账修正), 绝不以 0 价记账。
        同票多 lot 且持仓少于台账时按 FIFO 关旧 lot (幽灵仓自愈)。"""
        try:
            pos = {p.contract.symbol: p.position for p in await self.broker.positions()}
            fills = await self.broker.todays_fills()
        except Exception as e:
            log.warning("对账失败(%s): %s", tag, e)
            return
        self._persist_fills(fills)
        open_ = self.db.open_lots()
        # 持仓快照空且当次查询无成交 = 可能是 reqPositions 竞态返回空。此时仍允许用
        # fills 表里已固化的真实成交回填 (如午间抓到、日报窗口翻页的场景), 但禁用行情估价,
        # 防止把其实还在的持仓整批按估价关掉。
        race_suspect = not pos and not fills and bool(open_)
        if race_suspect:
            log.warning("对账(%s): 持仓快照为空且无成交, 疑似数据竞态, 仅按已固化成交回填", tag)
        by_sym = {}
        for lot in open_:
            by_sym.setdefault(lot["symbol"], []).append(lot)
        closed_msgs, today = [], str(now_et().date())
        for sym, lots in sorted(by_sym.items()):
            deficit = sum(l["qty"] for l in lots) - int(pos.get(sym, 0))
            if deficit <= 0:
                continue
            # 卖出价 = 覆盖缺口股数的最近卖出成交 vwap (只取最早未平 lot 入场收盘之后的)
            since = min(l["entry_date"] for l in lots) + "T16:00"
            q, cash = self.db.recent_sells(sym, deficit, since)
            vwap = cash / q if q else None
            for lot in sorted(lots, key=lambda l: l["lot_id"]):  # FIFO: 旧 lot 先出
                if deficit < lot["qty"]:
                    if deficit > 0:
                        self.notify.send(f"对账({tag}): {sym} 持仓比台账少 {deficit} 股, "
                                         f"不足 lot{lot['lot_id']} 整手, 请人工核对", "warn")
                    break
                if vwap:
                    exit_px, how = vwap, f"fill@{tag}"
                elif race_suspect:
                    break  # 无成交佐证 + 持仓快照可疑: 不动, 等下次对账
                else:
                    c = await self.broker.qualify(sym)
                    _, _, last = await self.broker.market_ref(c) if c else (None, None, None)
                    if not last:
                        self.notify.send(f"对账({tag}): {sym} lot{lot['lot_id']} 持仓已消失但查无成交"
                                         f"且无行情可估价, 保留待查", "critical")
                        break
                    exit_px, how = last, f"resync-est@{tag}"
                pnl = (exit_px - lot["entry_price"]) * lot["qty"] - 2.0
                self.db.close_lot(lot["lot_id"], today, round(exit_px, 4), how, round(pnl, 2))
                pct = (exit_px / lot["entry_price"] - 1) * 100
                est = " ⚠️估价" if how.startswith("resync-est") else ""
                closed_msgs.append(f"{sym} x{lot['qty']} @{exit_px:.2f} ({pct:+.2f}%, ${pnl:+,.0f}){est}")
                deficit -= lot["qty"]
        if closed_msgs:
            self.notify.send("已平仓:\n" + "\n".join(closed_msgs))

    async def do_midday_reconcile(self, d):
        """午间对账: 赶在网关时区(上海)的午夜=12:00 ET 之前, 把上午的追踪单成交固化进
        fills 表并回填平仓, 否则 16:20 日报的 reqExecutions 已看不到这些成交。"""
        await self._resync_lots_with_positions("午间")

    # ---------- 主循环 ----------
    ACTIONS = {
        "gate_check": None,  # 特殊处理
        "build_plan": "do_build_plan",
        "submit_moc": "do_submit_moc",
        "confirm_fills": "do_confirm_fills",
        "overnight_sells": "do_overnight_sells",
        "premarket_sells": "do_premarket_sells",
        "open_trail": "do_open_trail",
        "midday_reconcile": "do_midday_reconcile",
        "daily_report": "do_daily_report",
    }

    # 事件错过后仍值得补执行的时间窗 (秒)。买入链严格准时 (MOC 有截止), 卖出链宽容自愈。
    GRACE = {"overnight_sells": 7 * 3600, "premarket_sells": 5 * 3600, "open_trail": 6 * 3600,
             "midday_reconcile": 2 * 3600, "confirm_fills": 4 * 3600, "daily_report": 12 * 3600,
             "submit_moc": 120}

    async def run_day(self, d, from_now_only=True):
        """执行交易日 d 的事件表。买入链在闸门拦截时跳过, 卖出链始终执行 (照顾已有持仓)。"""
        sched = cal.todays_schedule(self.cfg, d)
        gate_passed = None
        for name, ts in sched:
            wait = (ts - now_et()).total_seconds()
            if from_now_only and wait < -self.GRACE.get(name, 300):
                continue  # 超出补执行窗口的过期事件跳过
            if wait < -60:
                self.notify.send(f"补执行 {name} (迟到 {-wait / 60:.0f} 分钟)", "warn")
            if wait > 0:
                log.info("等待 %s @ %s (%.0f 分钟)", name, ts.strftime("%H:%M ET"), wait / 60)
                await asyncio.sleep(wait)
            t0 = now_et().isoformat(timespec="seconds")
            self.notify.buffer = []
            status = "ok"
            try:
                await self.broker.connect()
                if name == "gate_check":
                    gate_passed = await self.do_gate_check(d)
                elif name in ("build_plan", "submit_moc"):
                    if gate_passed is False:
                        log.info("闸门拦截, 跳过 %s", name)
                        status = "skipped"
                        self.notify.buffer.append("闸门拦截/暂停/无计划, 未执行")
                    else:
                        ok = await getattr(self, self.ACTIONS[name])(d)
                        if name == "build_plan" and ok is False:
                            gate_passed = False
                else:
                    await getattr(self, self.ACTIONS[name])(d)
            except Exception as e:
                log.exception("%s 失败", name)
                status = f"error: {e}"
                self.notify.send(f"[{d}] 步骤 {name} 失败: {e}", "critical")
            finally:
                detail = "\n".join(self.notify.buffer or []) or "(无输出)"
                self.notify.buffer = None
                try:
                    self.db.record_exec(d, name, status, detail, t0)
                except Exception:
                    log.exception("执行流水写入失败")
                self.broker.disconnect()

    @staticmethod
    def check_order_coverage(lots, positions, pending_sells, trail_orders, now_t, trail_hhmm):
        """订单哨兵纯逻辑: 返回 issues 列表 (空=健康)。
        不变量 (出场时段内): ① IB 每股持仓都有在途卖单覆盖 (不裸奔);
        ② 台账 lot 数量与 IB 持仓一致; ③ 追踪时点+10分钟后, 覆盖单应为 TRAIL (软提示)。
        时段边界 ±5 分钟不判 (换单过渡期)。lots: 未平 lot 列表; positions: {sym: qty};
        pending_sells: {sym: 在途卖量}; trail_orders: {sym: 追踪单卖量}。"""
        issues = []
        sell_windows = [("04:00", "16:00"), ("20:00", "23:59"), ("00:00", "03:50")]
        in_window = any(a <= now_t <= b for a, b in sell_windows)
        boundaries = ["04:00", "09:30", trail_hhmm, "16:00", "20:00", "03:50"]

        def near_boundary(t):
            tm = int(t[:2]) * 60 + int(t[3:5])
            for b in boundaries:
                bm = int(b[:2]) * 60 + int(b[3:5])
                if abs(tm - bm) <= 5:
                    return True
            return False

        exp = {}
        for l in lots:
            exp[l["symbol"]] = exp.get(l["symbol"], 0) + l["qty"]
        # ② 台账 vs IB 持仓 (任何时候都查)
        for sym, q in sorted(exp.items()):
            have = int(positions.get(sym, 0))
            if have != q and not near_boundary(now_t):
                issues.append(f"{sym}: 台账 {q} 股 vs IB 持仓 {have} 股 (待对账)")
        if not in_window or near_boundary(now_t):
            return issues
        # ① 裸奔检测: 持仓 > 在途卖量
        for sym in sorted(set(positions) & set(exp)):
            have, pend = int(positions.get(sym, 0)), int(pending_sells.get(sym, 0))
            if have > 0 and pend < have:
                issues.append(f"🚨 {sym}: 持仓 {have} 股仅 {pend} 股有卖单在途 (裸奔 {have - pend} 股)")
        # ③ 追踪时段的订单类型 (软提示, 手动限价单属合法状态)
        trail_min = int(trail_hhmm[:2]) * 60 + int(trail_hhmm[3:5])
        now_min = int(now_t[:2]) * 60 + int(now_t[3:5])
        if trail_min + 10 <= now_min and now_t <= "16:00":
            for sym in sorted(set(positions) & set(exp)):
                if int(positions.get(sym, 0)) > 0 and not trail_orders.get(sym):
                    issues.append(f"{sym}: 已过追踪时点但覆盖单非 TRAIL (若为手动单可忽略)")
        return issues

    async def order_watchdog(self):
        """订单哨兵: 取 IB 实时持仓/挂单 + 台账, 跑一致性校验, 结果写 control 表供面板显示;
        出现裸奔立即 critical 告警 (去重: 同一问题集合 30 分钟内不重复推)。"""
        import json as _json
        c = self.cfg["ib"]
        rb = Broker({**self.cfg, "ib": {**c, "client_id": c["client_id"] + 1}})
        try:
            await rb.connect(retries=1)
            pos = {p.contract.symbol: int(p.position) for p in await rb.positions()}
            await rb.ib.reqAllOpenOrdersAsync()
            pend, trails = {}, {}
            for t in rb.ib.openTrades():
                if t.order.action != "SELL":
                    continue
                rem = t.orderStatus.remaining
                q = int(rem) if rem and rem > 0 else int(t.order.totalQuantity)
                pend[t.contract.symbol] = pend.get(t.contract.symbol, 0) + q
                if t.order.orderType == "TRAIL":
                    trails[t.contract.symbol] = trails.get(t.contract.symbol, 0) + q
        finally:
            rb.disconnect()
        now = now_et()
        tr_off = float(self.cfg["schedule_et"].get("open_trail_offset_min", 1))
        trail_hhmm = f"{9 + int((30 + tr_off) // 60):02d}:{int((30 + tr_off) % 60):02d}"
        issues = self.check_order_coverage(self.db.open_lots(), pos, pend, trails,
                                           now.strftime("%H:%M"), trail_hhmm)
        verdict = {"ts": now.isoformat(timespec="minutes"), "ok": not issues, "issues": issues}
        self.db.set_control("watchdog", _json.dumps(verdict, ensure_ascii=False))
        key = "|".join(issues)
        naked = [i for i in issues if "裸奔" in i]
        if naked and (key != getattr(self, "_wd_last", "") or
                      (now - getattr(self, "_wd_last_t", now)).total_seconds() > 1800):
            self.notify.send("订单哨兵:\n" + "\n".join(issues), "critical")
            self._wd_last, self._wd_last_t = key, now
        elif not issues:
            self._wd_last = ""
        return issues

    async def heartbeat_loop(self):
        """每 10 分钟探测网关/隧道; 状态变化即告警; 交易时段整点报平安 + 保证金缓冲监控。"""
        from broker import Broker, probe_handshake
        c = self.cfg["ib"]
        last_ok, last_beat, last_margin_alert = None, None, None
        n = 0
        while True:
            ok = probe_handshake(c["host"], c["port"], timeout=10)
            if last_ok is not None and ok != last_ok:
                if ok:
                    self.notify.send("网关/隧道恢复正常 ✅")
                else:
                    self.notify.send("网关/隧道无响应！检查 Windows 隧道任务与 Gateway", "critical")
            last_ok = ok
            t = now_et()
            hb_min = self.cfg["notify"].get("heartbeat_minutes", 60)
            in_session = cal.is_trading_day(t.date()) and 4 <= t.hour < 20
            if in_session and hb_min and (last_beat is None or (t - last_beat).total_seconds() >= hb_min * 60):
                open_n = len(self.db.open_lots())
                self.notify.send(f"心跳: 网关{'正常' if ok else '异常'} | 未平 lot {open_n} | {t:%H:%M ET}")
                last_beat = t
            # 订单哨兵: 出场时段内每个心跳周期核一次持仓-挂单-台账一致性
            if ok and cal.is_trading_day(t.date()) and self.db.open_lots():
                try:
                    await self.order_watchdog()
                except Exception as e:
                    log.warning("订单哨兵失败: %s", e)
            # 保证金缓冲: RTH 内每 30 分钟查一次
            n += 1
            rth = cal.is_trading_day(t.date()) and (9, 30) <= (t.hour, t.minute) < (16, 0)
            if ok and rth and n % 3 == 0:
                try:
                    rb = Broker({**self.cfg, "ib": {**c, "client_id": c["client_id"] + 1}})
                    await rb.connect(retries=1)
                    acct = await rb.account()
                    rb.disconnect()
                    netliq = acct["NetLiquidation"]
                    cushion = acct["AvailableFunds"] / netliq * 100 if netliq else 0
                    limit = self.cfg.get("risk", {}).get("cushion_alert_pct", 12)
                    if cushion < limit and (last_margin_alert is None or
                                            (t - last_margin_alert).total_seconds() > 7200):
                        self.notify.send(
                            f"保证金缓冲仅 {cushion:.1f}% (可用 ${acct['AvailableFunds']:,.0f} / "
                            f"净值 ${netliq:,.0f})，接近强平风险，考虑减仓", "critical")
                        last_margin_alert = t
                except Exception as e:
                    log.warning("保证金检查失败: %s", e)
            await asyncio.sleep(600)

    async def run_forever(self):
        try:  # 启动横幅带运行版本 (update.sh 写入 VERSION), 一眼可知在跑哪个 commit
            import os
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "VERSION"), encoding="ascii") as f:
                ver = f.read().strip()
        except Exception:
            ver = "unknown"
        self.notify.send(f"autotrader 启动 (mode={self.cfg['mode']}, 版本 {ver})")
        asyncio.create_task(self.heartbeat_loop())
        while True:
            try:  # 热加载配置 (面板改动即生效; 原地更新保持引用)
                from common import load_config
                self.cfg.clear()
                self.cfg.update(load_config())
            except Exception as e:
                log.warning("配置热加载失败: %s", e)
            today = now_et().date()
            if cal.is_trading_day(today) and now_et() < cal.market_close_et(today) + timedelta(minutes=30):
                d = today
            else:
                d = cal.next_trading_day(today)
            log.info("目标交易日: %s", d)
            await self.run_day(d)
            await asyncio.sleep(60)
