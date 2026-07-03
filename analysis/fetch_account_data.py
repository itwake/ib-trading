# -*- coding: utf-8 -*-
"""只读：连接本地 IB Gateway，拉取账户摘要 / 持仓 / 成交 / 已完成订单，存为 CSV/JSON。"""
import asyncio
import json
import sys

from ib_async import IB, ExecutionFilter, util

HOST = "127.0.0.1"
PORT = 4001
CLIENT_ID = 17

OUT_DIR = r"C:\CCWork\ib-trading\data"


async def main():
    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    print("connected:", ib.isConnected())
    print("accounts:", ib.managedAccounts())

    # 账户摘要
    summary = await ib.accountSummaryAsync()
    summary_rows = [
        {"account": s.account, "tag": s.tag, "value": s.value, "currency": s.currency}
        for s in summary
    ]

    # 持仓
    positions = await ib.reqPositionsAsync()
    pos_rows = [
        {
            "account": p.account,
            "symbol": p.contract.symbol,
            "secType": p.contract.secType,
            "currency": p.contract.currency,
            "position": p.position,
            "avgCost": p.avgCost,
        }
        for p in positions
    ]

    # 成交（fills，API 一般只保留最近 ~7 天 / 本次会话）
    fills = await ib.reqExecutionsAsync(ExecutionFilter())
    fill_rows = []
    for f in fills:
        e = f.execution
        c = f.contract
        cr = f.commissionReport
        fill_rows.append(
            {
                "time": str(e.time),
                "symbol": c.symbol,
                "secType": c.secType,
                "side": e.side,
                "shares": e.shares,
                "price": e.price,
                "avgPrice": e.avgPrice,
                "exchange": e.exchange,
                "orderId": e.orderId,
                "execId": e.execId,
                "commission": getattr(cr, "commission", None),
                "realizedPNL": getattr(cr, "realizedPNL", None),
            }
        )

    # 已完成订单
    completed = await ib.reqCompletedOrdersAsync(apiOnly=False)
    comp_rows = []
    for t in completed:
        o = t.order
        c = t.contract
        st = t.orderStatus
        comp_rows.append(
            {
                "symbol": c.symbol,
                "action": o.action,
                "orderType": o.orderType,
                "tif": o.tif,
                "qty": o.totalQuantity,
                "lmtPrice": o.lmtPrice,
                "status": st.status,
                "filled": st.filled,
                "avgFillPrice": st.avgFillPrice,
                "completedTime": getattr(o, "completedTime", ""),
                "completedStatus": getattr(o, "completedStatus", ""),
            }
        )

    import os

    os.makedirs(OUT_DIR, exist_ok=True)
    for name, rows in [
        ("account_summary", summary_rows),
        ("positions", pos_rows),
        ("fills", fill_rows),
        ("completed_orders", comp_rows),
    ]:
        path = os.path.join(OUT_DIR, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(rows, fp, ensure_ascii=False, indent=1, default=str)
        print(f"{name}: {len(rows)} rows -> {path}")

    ib.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print("ERROR:", type(exc).__name__, exc, file=sys.stderr)
        sys.exit(1)
