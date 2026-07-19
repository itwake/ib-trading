# -*- coding: utf-8 -*-
"""每周 Flex 自动对账: 从 IBKR Flex Web Service 拉取成交报告, 与台账逐笔核对。

做三件事 (只修账, 不碰交易):
  1. resync/resync-est (估价) 关闭的 lot => 用 Flex 真实成交价修正
  2. fill@ 关闭的 lot => 校验出场价 (>0.5% 偏差报警并修正)
  3. 回填每手真实佣金 (fees 列), pnl 重算 = (出-入)×qty − 真实佣金

前置 (一次性, 用户在 IBKR 网页操作):
  Performance & Reports -> Flex Queries -> 新建 Trades 查询 (格式 CSV, 含
  Symbol/TradeDate/Quantity/TradePrice/Buy/Sell/IBCommission 列, 期间=Last 30 days)
  -> Settings -> FlexWeb Service -> 生成 token
  然后在面板配置页填 flex.token 与 flex.query_id。

用法:
  .venv/bin/python deploy/flex_reconcile.py            # 拉取 + 预览
  .venv/bin/python deploy/flex_reconcile.py --apply    # 拉取 + 写库 + Discord 报告
  .venv/bin/python deploy/flex_reconcile.py --csv f.csv --apply   # 用本地文件 (调试)
systemd timer (ibtrading-flexrecon.timer) 每周六运行 --apply。"""
import sys
import time
from datetime import date, timedelta
from os.path import abspath, dirname, join

ROOT = dirname(dirname(abspath(__file__)))
sys.path.insert(0, join(ROOT, "autotrader"))
sys.path.insert(0, dirname(abspath(__file__)))

import requests  # noqa: E402
from repair_lots import parse_flex  # noqa: E402
from common import load_config  # noqa: E402
from notify import Notifier  # noqa: E402
from storage import DB  # noqa: E402

SEND_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
GET_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
UA = {"User-Agent": "ibtrading-flexrecon"}
PRICE_TOL = 0.005  # fill@ 出场价校验容差 0.5%


def fetch_flex(token, query_id):
    """两步取报告: SendRequest 拿 ReferenceCode -> GetStatement 轮询取正文。"""
    import re
    r = requests.get(SEND_URL, params={"t": token, "q": query_id, "v": 3},
                     headers=UA, timeout=30)
    r.raise_for_status()
    m = re.search(r"<ReferenceCode>(\w+)</ReferenceCode>", r.text)
    if not m:
        raise RuntimeError(f"SendRequest 未返回 ReferenceCode: {r.text[:300]}")
    ref = m.group(1)
    for i in range(10):  # 报告生成需要数秒到数分钟
        time.sleep(5 + i * 5)
        r = requests.get(GET_URL, params={"t": token, "q": ref, "v": 3},
                         headers=UA, timeout=60)
        r.raise_for_status()
        if "<code>1019</code>" in r.text or "generation in progress" in r.text.lower():
            continue
        if r.text.lstrip().startswith("<FlexStatementResponse"):
            raise RuntimeError(f"GetStatement 错误: {r.text[:300]}")
        return r.text  # CSV 正文
    raise RuntimeError("Flex 报告生成超时")


def parse_flex_all(path):
    """在 repair_lots.parse_flex (只取卖出) 基础上补买入与佣金: 返回
    sells/buys: [(sym, date, qty, price, dt)], comm: {(sym, date): 佣金合计}。"""
    import csv
    sells, buys, comm = [], [], {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            get = lambda *ks: next((row[k].strip() for k in ks if k in row and row[k].strip()), "")
            sym, side = get("Symbol"), get("Buy/Sell", "BuySell").upper()
            qty, px = get("Quantity", "Qty"), get("TradePrice", "T. Price", "Price")
            d = get("TradeDate") or get("Date/Time", "DateTime")
            c = get("IBCommission", "Commission")
            if not sym or not qty or not px:
                continue
            try:
                q, p = float(qty.replace(",", "")), float(px.replace(",", ""))
            except ValueError:
                continue
            d = d.split(";")[0][:10].replace("/", "-")
            if len(d) == 8 and d.isdigit():
                d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            if len(d) != 10:
                continue
            try:
                comm[(sym, d)] = comm.get((sym, d), 0.0) + abs(float(c.replace(",", "")))
            except (ValueError, AttributeError):
                pass
            dt = get("Date/Time", "DateTime") or d
            if side.startswith("S") or q < 0:
                sells.append((sym, d, abs(q), p, dt))
            else:
                buys.append((sym, d, abs(q), p, dt))
    sells.sort(key=lambda r: r[4])
    buys.sort(key=lambda r: r[4])
    return sells, buys, comm


def vwap_match(records, used, sym, need, lo_date, hi_date):
    """按时间序从 records 里凑 need 股 (日期在 [lo,hi]), 带跨 lot 去重。返回 (qty, cash)。"""
    got = cash = 0.0
    for i, (s, d, q, p, _) in enumerate(records):
        if s != sym or not (lo_date <= d <= hi_date):
            continue
        avail = q - used.get(i, 0.0)
        if avail <= 0:
            continue
        take = min(avail, need - got)
        got += take
        cash += take * p
        used[i] = used.get(i, 0.0) + take
        if got >= need:
            break
    return got, cash


def main():
    apply = "--apply" in sys.argv
    cfg = load_config()
    tok = (cfg.get("flex") or {}).get("token", "")
    qid = (cfg.get("flex") or {}).get("query_id", "")
    csv_path = None
    if "--csv" in sys.argv:
        csv_path = sys.argv[sys.argv.index("--csv") + 1]
    elif not tok or not qid:
        print("未配置 flex.token / flex.query_id (面板配置页填写), 跳过本次对账")
        return
    else:
        text = fetch_flex(tok, qid)
        csv_path = "/tmp/flex_weekly.csv"
        open(csv_path, "w", encoding="utf-8").write(text)
        print(f"Flex 报告已取: {len(text)} bytes")

    sells, buys, comm = parse_flex_all(csv_path)
    print(f"Flex: {len(sells)} 卖 / {len(buys)} 买")
    db = DB(cfg["db_path"])
    since = str(date.today() - timedelta(days=9))  # 覆盖上一整周
    lots = [dict(zip([c[0] for c in db.conn.execute("SELECT * FROM lots LIMIT 0").description], r))
            for r in db.conn.execute(
                "SELECT * FROM lots WHERE state='CLOSED' AND exit_date>=? ORDER BY lot_id", (since,))]
    used_s, used_b = {}, {}
    fixed, verified, alerts = [], 0, []
    for lot in lots:
        sym, qty = lot["symbol"], lot["qty"]
        lo, hi = lot["entry_date"], str(date.fromisoformat(lot["exit_date"]) + timedelta(days=1))
        sq, scash = vwap_match(sells, used_s, sym, qty, lo, hi)
        if sq < qty * 0.99:
            alerts.append(f"{sym} lot{lot['lot_id']}: Flex 卖出仅匹配 {sq:.0f}/{qty} 股")
            continue
        true_exit = scash / sq
        bq, bcash = vwap_match(buys, used_b, sym, qty, lot["entry_date"], lot["entry_date"])
        true_entry = bcash / bq if bq >= qty * 0.99 else lot["entry_price"]
        fees = round(comm.get((sym, lot["entry_date"]), 0.0) * (qty / max(bq, qty)) +
                     comm.get((sym, lot["exit_date"]), 0.0) * (qty / max(sq, qty)), 2) or 2.0
        true_pnl = round((true_exit - true_entry) * qty - fees, 2)
        est = (lot["exit_how"] or "").startswith("resync")
        drift = abs(true_exit - (lot["exit_price"] or 0)) / true_exit
        if est or drift > PRICE_TOL or abs(true_pnl - (lot["pnl"] or 0)) > 5:
            fixed.append(f"{sym} lot{lot['lot_id']}: 出场 {lot['exit_price']}→{true_exit:.4f}, "
                         f"pnl {lot['pnl']}→{true_pnl:+.2f} (费 {fees})")
            if apply:
                db.conn.execute(
                    "UPDATE lots SET exit_price=?, pnl=?, fees=?, exit_how=? WHERE lot_id=?",
                    (round(true_exit, 4), true_pnl, fees,
                     "flex@auto" if est else lot["exit_how"], lot["lot_id"]))
        else:
            verified += 1
            if apply and lot.get("fees") is None:
                db.conn.execute("UPDATE lots SET fees=?, pnl=? WHERE lot_id=?",
                                (fees, true_pnl, lot["lot_id"]))
    if apply:
        db.conn.commit()
    lines = [f"[Flex对账] 核对 {len(lots)} 手: 相符 {verified}, 修正 {len(fixed)}, 异常 {len(alerts)}"]
    lines += ["  修正: " + s for s in fixed[:10]]
    lines += ["  ⚠️ " + s for s in alerts[:5]]
    report = "\n".join(lines)
    print(report)
    if apply:
        try:
            Notifier(cfg).send(report, "warn" if (fixed or alerts) else "info")
        except Exception as e:
            print("Discord 通知失败:", e)


if __name__ == "__main__":
    main()
