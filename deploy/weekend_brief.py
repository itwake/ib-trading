# -*- coding: utf-8 -*-
"""周末持仓风险简报: 对每只将持有过周末的仓位, 用 codex+网络搜索排查未来 5 个交易日的
二元事件 (财报/FDA/判决/增发交割/解禁) 与进行中的风险 (做空战/ATM 压制)。
每周五 17:00 ET 由 systemd timer 运行 (ibtrading-weekendbrief.timer) -> Discord + briefs 表。
依据: 全年审计 8 笔最惨隔夜跳空 6 笔发生在周末/拦截日; 首周 BE 做空战周末案例。
手动: .venv/bin/python deploy/weekend_brief.py"""
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from os.path import abspath, dirname, join

ROOT = dirname(dirname(abspath(__file__)))
sys.path.insert(0, join(ROOT, "autotrader"))
from common import load_config  # noqa: E402
from notify import Notifier  # noqa: E402
from storage import DB  # noqa: E402

PROMPT = (
    '{sym} is a US stock held over the coming weekend (today is {d}). Using web search: '
    '(a) find SCHEDULED binary events for {sym} in the next 5 US trading days: earnings date, '
    'FDA/PDUFA or clinical data readout, court ruling, offering settlement, lockup expiry, '
    'index add/remove; (b) find ACTIVE situations: short-seller campaign, ongoing ATM/offering '
    'pressure, pending M&A. Be factual; if nothing found say so. Reply ONLY JSON: '
    '{{"risk":"high|medium|low","events":["short item", "..."],"reason":"one sentence"}}')
RANK = {"high": 0, "medium": 1, "low": 2}
ICON = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def probe(row):
    sym, qty, ep = row
    try:
        out = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "-s", "read-only",
             "-c", "tools.web_search=true", PROMPT.format(sym=sym, d=date.today())],
            capture_output=True, timeout=240, cwd="/tmp").stdout.decode("utf-8", "replace")
        m = re.findall(r'\{[^{}]*"risk"[^{}]*\}', out)
        if not m:
            return (sym, qty, {"risk": "medium", "events": [], "reason": "归因无输出(降级为中风险)"})
        return (sym, qty, json.loads(m[-1]))
    except Exception as e:
        return (sym, qty, {"risk": "medium", "events": [], "reason": f"查询失败: {str(e)[:50]}"})


def main():
    cfg = load_config()
    db = DB(cfg["db_path"])
    lots = db.conn.execute(
        "SELECT symbol, SUM(qty), ROUND(AVG(entry_price),2) FROM lots"
        " WHERE state NOT IN ('CLOSED','ERROR') GROUP BY symbol").fetchall()
    if not lots:
        print("无持仓过周末, 跳过简报")
        return
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(probe, lots))
    results.sort(key=lambda r: RANK.get(str(r[2].get("risk", "medium")).lower(), 1))
    L = [f"🛡 周末持仓风险简报 {date.today()} ({len(results)} 只)"]
    for sym, qty, j in results:
        risk = str(j.get("risk", "medium")).lower()
        evs = "；".join(str(e)[:60] for e in (j.get("events") or [])[:3]) or "无已排期事件"
        L.append(f"{ICON.get(risk, '🟡')} {sym}×{qty}: {evs}")
        L.append(f"   {str(j.get('reason', ''))[:110]}")
    n_high = sum(1 for _, _, j in results if str(j.get("risk")).lower() == "high")
    if n_high:
        L.insert(1, f"⚠️ {n_high} 只高风险仓位, 建议周一开盘前复查")
    text = "\n".join(L)
    print(text)
    db.conn.execute("INSERT OR REPLACE INTO briefs VALUES ('weekend', ?, ?)",
                    (str(date.today()), text))
    db.conn.commit()
    try:
        Notifier(cfg).send(text, "warn" if n_high else "info")
    except Exception as e:
        print("Discord 失败:", e)


if __name__ == "__main__":
    main()
