# -*- coding: utf-8 -*-
"""每周体检报告: 真实盈亏 vs 基准、选股漏斗、指标积累进度、环境小结。
systemd timer 每周六 21:00 (服务器时区) 运行 => Discord + weekly_reports 表 (面板可看)。
手动: .venv/bin/python deploy/weekly_report.py [--quiet(不推Discord)]"""
import sys
from datetime import date, timedelta
from os.path import abspath, dirname, join

ROOT = dirname(dirname(abspath(__file__)))
sys.path.insert(0, join(ROOT, "autotrader"))

from common import load_config  # noqa: E402
from notify import Notifier  # noqa: E402
from storage import DB  # noqa: E402


def week_window(today=None):
    """最近一个完整交易周 (周一..周五)。周六跑 => 本周; 其他日子跑 => 含今天的这周。"""
    t = today or date.today()
    monday = t - timedelta(days=t.weekday())
    if t.weekday() >= 5:  # 周六/日: 报本周一~五
        return monday, monday + timedelta(days=4)
    return monday, monday + timedelta(days=4)


def main():
    cfg = load_config()
    db = DB(cfg["db_path"])
    q = lambda sql, a=(): db.conn.execute(sql, a).fetchall()
    lo, hi = week_window()
    lo_s, hi_s = str(lo), str(hi)
    L = [f"📋 周报 {lo_s} ~ {hi_s}"]

    # 1. 已实现盈亏 (按出场日) + 各批次
    total = q("SELECT ROUND(COALESCE(SUM(pnl),0),2), COUNT(*),"
              " SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) FROM lots"
              " WHERE state='CLOSED' AND exit_date BETWEEN ? AND ?", (lo_s, hi_s))[0]
    L.append(f"已实现: ${total[0]:+,.2f} | {total[1]} 手 | 胜率 "
             f"{(100 * (total[2] or 0) / total[1]):.0f}%" if total[1] else "已实现: 本周无平仓")
    for r in q("SELECT entry_date, COUNT(*), ROUND(SUM(pnl),2) FROM lots"
               " WHERE state='CLOSED' AND exit_date BETWEEN ? AND ? GROUP BY entry_date"
               " ORDER BY entry_date", (lo_s, hi_s)):
        L.append(f"  批次 {r[0]}: {r[1]} 手 ${r[2]:+,.2f}")
    est = q("SELECT COUNT(*) FROM lots WHERE exit_date BETWEEN ? AND ?"
            " AND exit_how LIKE 'resync%'", (lo_s, hi_s))[0][0]
    if est:
        L.append(f"  ⚠️ {est} 手为估价/对账关闭, 待 Flex 对账修正")

    # 2. 净值与基准
    snaps = q("SELECT date, netliq FROM snapshots WHERE date<=? ORDER BY date", (hi_s,))
    if len(snaps) >= 2:
        prior = [s for s in snaps if s[0] < lo_s]
        base = prior[-1] if prior else snaps[0]
        L.append(f"NetLiq: ${base[1]:,.0f} ({base[0]}) → ${snaps[-1][1]:,.0f} ({snaps[-1][0]})"
                 f" = {100 * (snaps[-1][1] / base[1] - 1):+.2f}%")
    try:
        import yfinance as yf
        spy = yf.download("SPY", start=str(lo - timedelta(days=7)), progress=False,
                          auto_adjust=False)["Close"].squeeze().dropna()
        spy_in = spy[[str(x.date()) <= hi_s for x in spy.index]]
        spy_pre = spy_in[[str(x.date()) < lo_s for x in spy_in.index]]
        if len(spy_pre) and len(spy_in):
            L.append(f"同期 SPY: {100 * (float(spy_in.iloc[-1]) / float(spy_pre.iloc[-1]) - 1):+.2f}%")
    except Exception:
        pass

    # 3. 选股漏斗
    for r in q("SELECT date, n_planned, note, cand_n FROM nightly_runs"
               " WHERE date BETWEEN ? AND ? ORDER BY date", (lo_s, hi_s)):
        filled = q("SELECT COUNT(*) FROM lots WHERE entry_date=?", (r[0],))[0][0]
        flag = "" if filled == r[1] else f" ⚠️缺口{r[1] - filled}"
        L.append(f"  {r[0]}: 计划{r[1]}/成交{filled}{flag} | 候选池{int(r[3] or 0)} | {(r[2] or '')[:20]}")

    # 4. 环境与影子闸门
    env = q("SELECT COUNT(*), SUM(gate_shadow), ROUND(MIN(vix),1), ROUND(MAX(vix),1)"
            " FROM nightly_runs WHERE date BETWEEN ? AND ? AND vix>0", (lo_s, hi_s))[0]
    if env[0]:
        L.append(f"环境: VIX {env[2]}~{env[3]} | 影子闸门放行 {int(env[1] or 0)}/{env[0]} 晚")
        sim = q("SELECT ROUND(SUM(CASE WHEN r.gate_shadow=1 THEN b.pnl ELSE 0 END),0),"
                " ROUND(SUM(b.pnl),0) FROM nightly_runs r JOIN"
                " (SELECT entry_date, SUM(pnl) pnl FROM lots WHERE state='CLOSED' GROUP BY entry_date) b"
                " ON b.entry_date=r.date WHERE r.date BETWEEN ? AND ?", (lo_s, hi_s))[0]
        if sim[1] is not None:
            L.append(f"  本周实际 ${sim[1]:+,.0f} vs 只做影子放行晚 ${sim[0] or 0:+,.0f}")

    # 5. 指标积累进度
    n_eval = q("SELECT COUNT(*) FROM watchlist WHERE shadow_ret_pct IS NOT NULL")[0][0]
    n_all = q("SELECT COUNT(*) FROM watchlist")[0][0]
    L.append(f"指标样本: 候选 {n_all} 行, 已回填 {n_eval} 行 (毕业线参考: 每桶≥60)")

    # 6. 当前持仓
    open_ = q("SELECT symbol, qty, entry_price FROM lots WHERE state NOT IN ('CLOSED','ERROR')")
    if open_:
        L.append("持仓: " + "、".join(f"{s}×{n}@{p}" for s, n, p in open_))

    text = "\n".join(L)
    print(text)
    db.conn.execute("CREATE TABLE IF NOT EXISTS weekly_reports"
                    " (week TEXT PRIMARY KEY, created_at TEXT, text TEXT)")
    from datetime import datetime
    db.conn.execute("INSERT OR REPLACE INTO weekly_reports VALUES (?,?,?)",
                    (lo_s, datetime.now().isoformat(timespec="minutes"), text))
    db.conn.commit()
    if "--quiet" not in sys.argv:
        try:
            Notifier(cfg).send(text)
        except Exception as e:
            print("Discord 通知失败:", e)


if __name__ == "__main__":
    main()
