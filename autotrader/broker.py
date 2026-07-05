# -*- coding: utf-8 -*-
"""ib_async 封装: 连接管理 / 账户 / 行情 / 三种订单 (MOC 买, OVERNIGHT 限价卖, 盘前限价卖, 开盘追踪卖)。
mode=dry 时所有下单只记日志不发送。"""
import asyncio
import logging
import socket
import struct

from ib_async import IB, LimitOrder, Order, Stock

log = logging.getLogger("broker")


import math

ORDER_TAG = "autotrader"


def round_tick(p: float) -> float:
    return round(p, 2) if p >= 1 else round(p, 4)


def _clean(x):
    try:
        v = float(x)
        return None if (math.isnan(v) or v <= 0) else v
    except Exception:
        return None


def smart_limit(target: float, bid, ask, last, buffer_pct: float = 0.1):
    """跟随市场: 市价已高于目标时抬高限价 (ref*(1+buffer), 不超过 ask), 否则用目标价。
    返回 (price, reason)。"""
    ref = bid or last
    limit, reason = target, "TARGET"
    if ref:
        follow = ref * (1 + buffer_pct / 100)
        if ask and follow > ask:
            follow = ask
        if follow > limit:
            limit, reason = follow, "FOLLOW"
    return round_tick(limit), reason


def probe_handshake(host, port, timeout=15) -> bool:
    """原始 socket 探测 API 是否有响应 (检测网关挂死, 不占 clientId)。"""
    try:
        s = socket.create_connection((host, port), timeout=5)
        payload = b"v100..187"
        s.sendall(b"API\0" + struct.pack(">I", len(payload)) + payload)
        s.settimeout(timeout)
        data = s.recv(4096)
        s.close()
        return len(data) > 0
    except Exception:
        return False


class Broker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ib = IB()

    @property
    def dry(self):
        return self.cfg["mode"] == "dry"

    async def connect(self, retries=3):
        c = self.cfg["ib"]
        for i in range(retries):
            if not probe_handshake(c["host"], c["port"]):
                raise ConnectionError("Gateway API 无握手响应 (可能挂死, 需重启网关)")
            try:
                await self.ib.connectAsync(c["host"], c["port"], clientId=c["client_id"], timeout=20)
                self.ib.reqMarketDataType(4)
                return
            except Exception as e:
                log.warning("连接失败 %d/%d: %s", i + 1, retries, e)
                await asyncio.sleep(5)
        raise ConnectionError("Gateway 连接失败")

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    async def account(self):
        rows = await self.ib.accountSummaryAsync()
        d = {r.tag: float(r.value) for r in rows if r.tag in
             ("NetLiquidation", "TotalCashValue", "GrossPositionValue", "AvailableFunds")}
        return d

    async def positions(self):
        return [p for p in await self.ib.reqPositionsAsync() if p.position != 0 and p.contract.secType == "STK"]

    async def qualify(self, symbol, exchange="SMART"):
        c = Stock(symbol, exchange, "USD")
        got = await self.ib.qualifyContractsAsync(c)
        return got[0] if got else None

    async def last_prices(self, symbols):
        out = {}
        for sym in symbols:
            try:
                c = await self.qualify(sym)
                if not c:
                    continue
                [t] = await self.ib.reqTickersAsync(c)
                p = t.last or t.close
                if p and p > 0:
                    out[sym] = float(p)
            except Exception as e:
                log.warning("取价失败 %s: %s", sym, e)
        return out

    async def market_ref(self, contract):
        """实时(延迟)行情参考: (bid, ask, last)。失败返回 (None, None, None)。"""
        try:
            [t] = await self.ib.reqTickersAsync(contract)
            return _clean(t.bid), _clean(t.ask), _clean(t.last) or _clean(t.close)
        except Exception as e:
            log.warning("行情获取失败 %s: %s", contract.symbol, e)
            return None, None, None

    async def open_sell_qty(self, symbol, ours_only=False):
        """在途卖单总量 (含其他客户端/手动挂单)。ours_only=True 只统计本系统的。"""
        await self.ib.reqAllOpenOrdersAsync()
        total = 0
        for t in self.ib.openTrades():
            if t.contract.symbol != symbol or t.order.action != "SELL":
                continue
            if ours_only and t.order.orderRef != ORDER_TAG:
                continue
            rem = t.orderStatus.remaining
            total += int(rem) if rem and rem > 0 else int(t.order.totalQuantity)
        return total

    async def sellable(self, symbol, want_qty):
        """防超卖: 可卖 = 实际持仓 - 全部在途卖单。返回 (可下单数量, 持仓, 在途卖量)。"""
        pos = 0
        for p in await self.positions():
            if p.contract.symbol == symbol:
                pos = int(p.position)
        pending = await self.open_sell_qty(symbol)
        return max(0, min(int(want_qty), pos - pending)), pos, pending

    def _send(self, contract, order, kind):
        order.orderRef = ORDER_TAG
        if self.dry:
            log.info("[DRY] %s %s %s x%s lmt=%s tif=%s", kind, order.action, contract.symbol,
                     order.totalQuantity, getattr(order, "lmtPrice", ""), order.tif)
            return None
        trade = self.ib.placeOrder(contract, order)
        log.info("[LIVE] %s %s %s x%s -> orderId=%s", kind, order.action, contract.symbol,
                 order.totalQuantity, trade.order.orderId)
        return trade

    async def buy_moc(self, symbol, qty):
        c = await self.qualify(symbol)
        if not c:
            return None
        o = Order(action="BUY", totalQuantity=qty, orderType="MOC", tif="DAY")
        return self._send(c, o, "MOC买入")

    async def sell_overnight(self, symbol, qty, limit_price):
        """隔夜时段 (20:00-03:50 ET): 直接路由 OVERNIGHT 场所, 仅限价, 单场次有效。"""
        c = Stock(symbol, "OVERNIGHT", "USD")
        got = await self.ib.qualifyContractsAsync(c)
        if not got:
            log.warning("%s 不支持 OVERNIGHT 场所", symbol)
            return None
        o = LimitOrder("SELL", qty, round_tick(limit_price), tif="DAY")
        return self._send(got[0], o, "隔夜限价卖")

    async def sell_premarket(self, symbol, qty, limit_price):
        """盘前 (04:00-09:30): SMART 限价 + outsideRth。"""
        c = await self.qualify(symbol)
        if not c:
            return None
        o = LimitOrder("SELL", qty, round_tick(limit_price), tif="DAY")
        o.outsideRth = True
        return self._send(c, o, "盘前限价卖")

    async def sell_market(self, symbol, qty):
        """应急市价卖出 (仅 RTH 保证成交; 盘外用限价)。"""
        c = await self.qualify(symbol)
        if not c:
            return None
        o = Order(action="SELL", totalQuantity=qty, orderType="MKT", tif="DAY")
        return self._send(c, o, "市价卖出")

    async def sell_trail(self, symbol, qty, trail_pct):
        """开盘后 0.3% 追踪卖出。"""
        c = await self.qualify(symbol)
        if not c:
            return None
        o = Order(action="SELL", totalQuantity=qty, orderType="TRAIL",
                  trailingPercent=trail_pct, tif="DAY")
        return self._send(c, o, "追踪卖出")

    async def cancel_open_sells(self, symbol=None, ours_only=True):
        """默认只撤本系统 (orderRef=autotrader) 的卖单, 不碰手动挂单。"""
        if self.dry:
            log.info("[DRY] 撤销在途卖单 %s (ours_only=%s)", symbol or "全部", ours_only)
            return
        await self.ib.reqAllOpenOrdersAsync()
        for t in self.ib.openTrades():
            if t.order.action != "SELL":
                continue
            if symbol is not None and t.contract.symbol != symbol:
                continue
            if ours_only and t.order.orderRef != ORDER_TAG:
                continue
            self.ib.cancelOrder(t.order)

    async def todays_fills(self):
        from ib_async import ExecutionFilter
        fills = await self.ib.reqExecutionsAsync(ExecutionFilter())
        return fills
