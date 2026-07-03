# -*- coding: utf-8 -*-
"""ib_async 封装: 连接管理 / 账户 / 行情 / 三种订单 (MOC 买, OVERNIGHT 限价卖, 盘前限价卖, 开盘追踪卖)。
mode=dry 时所有下单只记日志不发送。"""
import asyncio
import logging
import socket
import struct

from ib_async import IB, LimitOrder, Order, Stock

log = logging.getLogger("broker")


def round_tick(p: float) -> float:
    return round(p, 2) if p >= 1 else round(p, 4)


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
        self.dry = cfg["mode"] == "dry"
        self.ib = IB()

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

    def _send(self, contract, order, kind):
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

    async def sell_trail(self, symbol, qty, trail_pct):
        """开盘后 0.3% 追踪卖出。"""
        c = await self.qualify(symbol)
        if not c:
            return None
        o = Order(action="SELL", totalQuantity=qty, orderType="TRAIL",
                  trailingPercent=trail_pct, tif="DAY")
        return self._send(c, o, "追踪卖出")

    async def cancel_open_sells(self, symbol=None):
        if self.dry:
            log.info("[DRY] 撤销在途卖单 %s", symbol or "全部")
            return
        await self.ib.reqOpenOrdersAsync()
        for t in self.ib.openTrades():
            if t.order.action == "SELL" and (symbol is None or t.contract.symbol == symbol):
                self.ib.cancelOrder(t.order)

    async def todays_fills(self):
        from ib_async import ExecutionFilter
        fills = await self.ib.reqExecutionsAsync(ExecutionFilter())
        return fills
