# -*- coding: utf-8 -*-
"""autotrader 入口:
  python main.py status      # 日历/闸门/网关/账户 现状
  python main.py plan        # 只跑 选股+计划 (不下单)
  python main.py run         # 常驻运行 (mode 由 config.json 决定: dry/live)
"""
import asyncio
import sys

import calendar_util as cal
from broker import Broker, probe_handshake
from common import load_config, now_et, setup_logging
from engine import Engine
from market_gate import check_gate


async def cmd_status(cfg):
    print(f"现在 (ET): {now_et():%Y-%m-%d %H:%M %Z}")
    today = now_et().date()
    d = today if cal.is_trading_day(today) else cal.next_trading_day(today)
    print(f"今天是交易日: {cal.is_trading_day(today)}; 目标交易日: {d}")
    print(f"该日开盘 {cal.market_open_et(d):%H:%M}, 收盘 {cal.market_close_et(d):%H:%M} ET")
    print("\n事件表:")
    for name, ts in cal.todays_schedule(cfg, d):
        print(f"  {ts:%m-%d %H:%M ET}  {name}")
    passed, vix, spy, reason = check_gate(cfg)
    print(f"\n闸门 ({'启用' if cfg['gate']['enabled'] else '停用'}): {passed}  {reason}")
    c = cfg["ib"]
    alive = probe_handshake(c["host"], c["port"])
    print(f"Gateway {c['host']}:{c['port']} API 握手: {'正常' if alive else '无响应(挂死/未启动)'}")
    if alive:
        b = Broker(cfg)
        try:
            await b.connect()
            acct = await b.account()
            pos = await b.positions()
            print(f"账户: NetLiq ${acct['NetLiquidation']:,.0f} | 现金 ${acct['TotalCashValue']:,.0f} | "
                  f"持仓 ${acct['GrossPositionValue']:,.0f} | 可用 ${acct['AvailableFunds']:,.0f}")
            for p in pos:
                print(f"  持仓 {p.contract.symbol} x{int(p.position)} @ {p.avgCost:.2f}")
        finally:
            b.disconnect()
    print(f"\nmode = {cfg['mode']}  (live 前请先在 config.json 改 mode 并确认纸账户验证通过)")


async def cmd_plan(cfg):
    from screener import build_plan, fetch_finviz
    b = Broker(cfg)
    await b.connect()
    try:
        acct = await b.account()
        budget = min(cfg["budget"]["nightly_max_usd"],
                     max(0.0, cfg["budget"]["gross_max_x_netliq"] * acct["NetLiquidation"] - acct["GrossPositionValue"]))
        cands = fetch_finviz(cfg)
        print("Finviz 候选:", cands[:12])
        prices = await b.last_prices([t for t, _ in cands[:15]])
        plan = build_plan(cfg, cands, prices, budget)
        print(f"预算 ${budget:,.0f}")
        for t, s, p in plan:
            print(f"  {t} x{s} @~{p:.2f} = ${s * p:,.0f}")
    finally:
        b.disconnect()


async def cmd_seed(cfg):
    """把 IB 当前持仓导入台账 (target = avgCost * (1+目标%)), 让引擎接管已有仓位。"""
    from broker import round_tick
    from storage import DB
    db = DB(cfg["db_path"])
    tracked = {l["symbol"] for l in db.open_lots()}
    b = Broker(cfg)
    await b.connect()
    try:
        mult = 1 + cfg["exits"]["overnight_target_pct"] / 100
        for p in await b.positions():
            sym = p.contract.symbol
            if sym in tracked:
                print(f"跳过 {sym}: 台账已有")
                continue
            lot_id = db.add_lot(sym, str(now_et().date()), int(p.position), float(p.avgCost),
                                round_tick(float(p.avgCost) * mult))
            print(f"导入 {sym} x{int(p.position)} @ {p.avgCost:.2f} -> 目标 {p.avgCost * mult:.2f} (lot {lot_id})")
    finally:
        b.disconnect()


def main():
    cfg = load_config()
    setup_logging()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        asyncio.run(cmd_status(cfg))
    elif cmd == "plan":
        asyncio.run(cmd_plan(cfg))
    elif cmd == "seed":
        asyncio.run(cmd_seed(cfg))
    elif cmd == "run":
        asyncio.run(Engine(cfg).run_forever())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
