# -*- coding: utf-8 -*-
"""用 IBKR Flex 报告 (Trades) 修正被 resync 清零/估价的 lot 出场记录。

用法:
  1. IBKR 网页 → Performance & Reports → Flex Queries → 导出 Trades CSV (含列
     Symbol, Date/Time, Quantity, TradePrice 或 T. Price, Buy/Sell)
  2. python3 deploy/repair_lots.py <flex.csv>            # 预览 (不写库)
     python3 deploy/repair_lots.py <flex.csv> --apply    # 写入
  3. 默认修 exit_how 以 'resync' 开头的 lot (含 resync@ 和 resync-est@);
     匹配规则: 同 symbol、卖出日在 [entry_date, exit_date+1] 内、数量按时间序累积到 lot qty。
"""
import csv
import sqlite3
import sys
from os.path import abspath, dirname, join

COMMISSION = 2.0  # 与 engine 记账口径一致的单笔往返佣金估计


def parse_flex(path):
    """返回 [(symbol, date 'YYYY-MM-DD', qty>0, price)] 仅卖出记录, 按时间升序。"""
    sells = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            get = lambda *ks: next((row[k].strip() for k in ks if k in row and row[k].strip()), "")
            sym = get("Symbol")
            side = get("Buy/Sell", "BuySell", "Side").upper()
            qty = get("Quantity", "Qty")
            px = get("TradePrice", "T. Price", "Price", "TradeMoney")
            dt = get("Date/Time", "DateTime", "TradeDate", "Date")
            trade_date = get("TradeDate") or dt
            if not sym or not qty or not px:
                continue
            try:
                q, p = float(qty.replace(",", "")), float(px.replace(",", ""))
            except ValueError:
                continue
            if side.startswith("S") or q < 0:  # SELL 或 Flex 负数量表示卖出
                d = trade_date.split(";")[0][:10].replace("/", "-")
                if len(d) == 8 and d.isdigit():  # 20260709 -> 2026-07-09
                    d = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                if len(d) != 10:
                    continue  # 无法识别的日期格式, 宁可跳过也不错配
                sells.append((sym, d, abs(q), p, dt))
    sells.sort(key=lambda r: r[4])
    return sells


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    apply = "--apply" in sys.argv
    db_path = join(dirname(dirname(abspath(__file__))), "autotrader", "journal.db")
    sells = parse_flex(args[0])
    print(f"Flex 卖出记录 {len(sells)} 条; 数据库 {db_path}\n")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    lots = conn.execute(
        "SELECT * FROM lots WHERE state='CLOSED' AND exit_how LIKE 'resync%'"
        " ORDER BY lot_id").fetchall()
    if not lots:
        print("没有需要修复的 lot (exit_how LIKE 'resync%')")
        return

    used = [0.0] * len(sells)  # 每条 Flex 记录已被分配的股数, 防重复配对
    fixed = 0
    for lot in lots:
        need, cash = lot["qty"], 0.0
        got = 0.0
        for i, (sym, d, q, p, _) in enumerate(sells):
            if sym != lot["symbol"] or not (lot["entry_date"] <= d):
                continue
            avail = q - used[i]
            if avail <= 0:
                continue
            take = min(avail, need - got)
            got += take
            cash += take * p
            used[i] += take
            if got >= need:
                break
        if got <= 0:
            print(f"  lot{lot['lot_id']:>4} {lot['symbol']:<6} x{lot['qty']}: Flex 中找不到匹配卖出, 跳过")
            continue
        px = cash / got
        pnl = round((px - lot["entry_price"]) * lot["qty"] - COMMISSION, 2)
        mark = "" if got >= lot["qty"] else f" (仅匹配到 {got:.0f}/{lot['qty']} 股)"
        print(f"  lot{lot['lot_id']:>4} {lot['symbol']:<6} x{lot['qty']}: "
              f"{lot['exit_price']} -> {px:.4f}  pnl {lot['pnl']} -> {pnl:+.2f}{mark}")
        if apply:
            conn.execute("UPDATE lots SET exit_price=?, pnl=?, exit_how='flex@fix' WHERE lot_id=?",
                         (round(px, 4), pnl, lot["lot_id"]))
            fixed += 1
    if apply:
        conn.commit()
        print(f"\n已写入 {fixed} 笔。")
    else:
        print("\n预览模式, 未写库。确认无误后加 --apply 执行。")


if __name__ == "__main__":
    main()
