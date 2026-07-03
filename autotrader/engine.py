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
        candidates = fetch_finviz(self.cfg)
        prices = await self.broker.last_prices([t for t, _ in candidates[:15]])
        self.plan = build_plan(self.cfg, candidates, prices, budget)
        lines = [f"{t} x{s} @~{p:.2f} (${s * p:,.0f})" for t, s, p in self.plan]
        self.notify.send(f"[{d}] 买入计划 预算 ${budget:,.0f} (NetLiq ${netliq:,.0f}):\n" + "\n".join(lines))
        self.db.record_run(str(d), True, 0, 0, len(self.plan), budget, "plan built")
        return bool(self.plan)

    async def do_submit_moc(self, d):
        ok = 0
        for t, shares, ref in self.plan:
            trade = await self.broker.buy_moc(t, shares)
            self.db.record_order(getattr(getattr(trade, "order", None), "orderId", -1) if trade else -1,
                                 0, "MOC_BUY", t, shares, 0, "submitted")
            ok += 1
        self.notify.send(f"[{d}] 已提交 {ok}/{len(self.plan)} 个 MOC 买单")

    async def do_confirm_fills(self, d):
        fills = await self.broker.todays_fills()
        target_mult = 1 + self.cfg["exits"]["overnight_target_pct"] / 100
        n = 0
        for f in fills:
            e, c = f.execution, f.contract
            if e.side != "BOT" or self.db.exec_seen(e.execId):
                continue
            self.db.add_lot(c.symbol, str(d), int(e.shares), float(e.avgPrice),
                            round_tick(float(e.avgPrice) * target_mult))
            self.db.mark_exec(e.execId)
            n += 1
        self.notify.send(f"[{d}] 成交确认: {n} 笔新买入已入台账")

    async def do_overnight_sells(self, d):
        lots = self.db.open_lots()
        n = 0
        for lot in lots:
            if lot["state"] not in ("FILLED", "OVERNIGHT"):
                continue
            t = await self.broker.sell_overnight(lot["symbol"], lot["qty"], lot["target_price"])
            self.db.set_lot_state(lot["lot_id"], "OVERNIGHT")
            self.db.record_order(getattr(getattr(t, "order", None), "orderId", -1) if t else -1,
                                 lot["lot_id"], "OVERNIGHT_SELL", lot["symbol"], lot["qty"],
                                 lot["target_price"], "submitted")
            n += 1
        if n:
            self.notify.send(f"隔夜限价卖单已挂 {n} 笔 (+{self.cfg['exits']['overnight_target_pct']}%)")

    async def do_premarket_sells(self, d):
        await self._resync_lots_with_positions("盘前")
        lots = [l for l in self.db.open_lots() if l["state"] in ("FILLED", "OVERNIGHT")]
        for lot in lots:
            await self.broker.sell_premarket(lot["symbol"], lot["qty"], lot["target_price"])
            self.db.set_lot_state(lot["lot_id"], "PREMARKET")
        if lots:
            self.notify.send(f"盘前限价卖单已挂 {len(lots)} 笔")

    async def do_open_trail(self, d):
        await self._resync_lots_with_positions("开盘")
        lots = [l for l in self.db.open_lots() if l["state"] in ("FILLED", "OVERNIGHT", "PREMARKET")]
        for lot in lots:
            await self.broker.cancel_open_sells(lot["symbol"])
            await asyncio.sleep(0.5)
            await self.broker.sell_trail(lot["symbol"], lot["qty"], self.cfg["exits"]["trail_pct"])
            self.db.set_lot_state(lot["lot_id"], "TRAILING")
        if lots:
            self.notify.send(f"开盘追踪卖出已挂 {len(lots)} 笔 ({self.cfg['exits']['trail_pct']}%)")

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

    async def _resync_lots_with_positions(self, tag):
        """以 IB 实际持仓为准核对台账: 已清仓的 lot 用当日卖出成交回填真实出场价与盈亏。"""
        try:
            pos = {p.contract.symbol: p.position for p in await self.broker.positions()}
            fills = await self.broker.todays_fills()
        except Exception as e:
            log.warning("对账失败(%s): %s", tag, e)
            return
        sells = {}
        for f in fills:
            e, c = f.execution, f.contract
            if e.side == "SLD":
                q0, cash0 = sells.get(c.symbol, (0.0, 0.0))
                sells[c.symbol] = (q0 + e.shares, cash0 + e.shares * e.price)
        closed_msgs = []
        for lot in self.db.open_lots():
            if pos.get(lot["symbol"], 0) > 0:
                continue
            q, cash = sells.get(lot["symbol"], (0.0, 0.0))
            exit_px = cash / q if q else 0.0
            pnl = (exit_px - lot["entry_price"]) * lot["qty"] - 2.0 if exit_px else 0.0
            self.db.close_lot(lot["lot_id"], str(now_et().date()), round(exit_px, 4),
                              f"fill@{tag}" if exit_px else f"resync@{tag}", round(pnl, 2))
            if exit_px:
                pct = (exit_px / lot["entry_price"] - 1) * 100
                closed_msgs.append(f"{lot['symbol']} x{lot['qty']} @{exit_px:.2f} ({pct:+.2f}%, ${pnl:+,.0f})")
        if closed_msgs:
            self.notify.send("已平仓:\n" + "\n".join(closed_msgs))

    # ---------- 主循环 ----------
    ACTIONS = {
        "gate_check": None,  # 特殊处理
        "build_plan": "do_build_plan",
        "submit_moc": "do_submit_moc",
        "confirm_fills": "do_confirm_fills",
        "overnight_sells": "do_overnight_sells",
        "premarket_sells": "do_premarket_sells",
        "open_trail": "do_open_trail",
        "daily_report": "do_daily_report",
    }

    async def run_day(self, d, from_now_only=True):
        """执行交易日 d 的事件表。买入链在闸门拦截时跳过, 卖出链始终执行 (照顾已有持仓)。"""
        sched = cal.todays_schedule(self.cfg, d)
        gate_passed = None
        for name, ts in sched:
            wait = (ts - now_et()).total_seconds()
            if from_now_only and wait < -300:
                continue  # 已过时的事件跳过
            if wait > 0:
                log.info("等待 %s @ %s (%.0f 分钟)", name, ts.strftime("%H:%M ET"), wait / 60)
                await asyncio.sleep(wait)
            try:
                await self.broker.connect()
                if name == "gate_check":
                    gate_passed = await self.do_gate_check(d)
                elif name in ("build_plan", "submit_moc"):
                    if gate_passed is False:
                        log.info("闸门拦截, 跳过 %s", name)
                    else:
                        ok = await getattr(self, self.ACTIONS[name])(d)
                        if name == "build_plan" and ok is False:
                            gate_passed = False
                else:
                    await getattr(self, self.ACTIONS[name])(d)
            except Exception as e:
                log.exception("%s 失败", name)
                self.notify.send(f"[{d}] 步骤 {name} 失败: {e}", "critical")
            finally:
                self.broker.disconnect()

    async def heartbeat_loop(self):
        """每 10 分钟探测网关/隧道; 状态变化即告警; 交易时段整点报平安。"""
        from broker import probe_handshake
        c = self.cfg["ib"]
        last_ok, last_beat = None, None
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
            await asyncio.sleep(600)

    async def run_forever(self):
        self.notify.send(f"autotrader 启动 (mode={self.cfg['mode']})")
        asyncio.create_task(self.heartbeat_loop())
        while True:
            today = now_et().date()
            if cal.is_trading_day(today) and now_et() < cal.market_close_et(today) + timedelta(minutes=30):
                d = today
            else:
                d = cal.next_trading_day(today)
            log.info("目标交易日: %s", d)
            await self.run_day(d)
            await asyncio.sleep(60)
