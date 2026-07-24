# -*- coding: utf-8 -*-
"""网关守望哨兵 (常驻 systemd 服务, 2026-07-24 网关死机事故后按用户确认的规格实现)。

状态机: UP -(连续4次握手失败,~2分钟)-> DOWN(告警+风险快照, 每30分钟重复)
        DOWN -(连续2次握手成功)-> 等90秒登录稳定 -> 恢复动作 -> UP

恢复动作只有一种: systemctl restart autotrader —— 让守护进程用自己的 clientId
重跑补执行机制 (错过哪步补哪步, 撤自己的单无跨客户端问题)。哨兵永远不直接碰订单。

护栏:
- 只在失联期间确有步骤报错时才重启 (无步骤受影响则只播报恢复, 避免无谓重启
  重置追踪单高水位 —— 已知设计疣);
- 每天最多重启 3 次, 两次间隔 >= 10 分钟;
- 15:35~15:50 ET 不重启 (不打断在途的 MOC 提交), 只告警;
- 03:40~04:10 ET 静默窗 (网关计划内自动重启), 不告警不动作;
- 空仓且非买入链时段的失联: 恢复时只告警不重启。

探针用 broker.probe_handshake (API 层): 能识别"端口通但 API 挂死"的半死状态
(2026-07-24 事故 08:56 起的形态), 纯 TCP 探测抓不到。
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "autotrader"))
from broker import probe_handshake  # noqa: E402
from notify import Notifier  # noqa: E402

ET = ZoneInfo("America/New_York")
PROBE_SEC = 30
FAILS_TO_DOWN = 4
OKS_TO_UP = 2
WARMUP_SEC = 90
REALERT_SEC = 30 * 60
MAX_RESTARTS_PER_DAY = 3
RESTART_GAP_SEC = 10 * 60


def now_et():
    return datetime.now(ET)


def hhmm(t=None):
    t = t or now_et()
    return t.hour * 100 + t.minute


def in_quiet_window(hm):
    """网关计划内自动重启窗 (03:40~04:10 ET): 不告警不动作。"""
    return 340 <= hm <= 410


def in_moc_window(hm):
    """15:35~15:50 ET: 不重启, 避免打断在途 MOC 提交。"""
    return 1535 <= hm <= 1550


def should_restart(errored_steps, restarts_today, last_restart_ts, now_ts, hm):
    """恢复时是否重启守护进程 (纯函数, 供测试)。返回 (bool, reason)。"""
    if in_quiet_window(hm):
        return False, "静默窗内"
    if in_moc_window(hm):
        return False, "MOC 提交窗内, 只告警"
    if not errored_steps:
        return False, "失联期间无步骤报错, 守护进程未受影响"
    if restarts_today >= MAX_RESTARTS_PER_DAY:
        return False, f"已达每日重启上限 {MAX_RESTARTS_PER_DAY} 次"
    if last_restart_ts and now_ts - last_restart_ts < RESTART_GAP_SEC:
        return False, "距上次重启不足 10 分钟"
    return True, "失联期间步骤报错: " + ", ".join(errored_steps[:5])


class Sentinel:
    def __init__(self):
        with open(os.path.join(HERE, "..", "autotrader", "config.json"), encoding="utf-8-sig") as f:
            self.cfg = json.load(f)
        self.db_path = os.path.join(HERE, "..", "autotrader", self.cfg.get("db_path", "journal.db"))
        self.notify = Notifier(self.cfg)
        self.fails = 0
        self.oks = 0
        self.down_since = None
        self.last_alert = 0.0
        self.restart_day = None
        self.restarts_today = 0
        self.last_restart_ts = None

    def probe(self):
        ib = self.cfg["ib"]
        return probe_handshake(ib["host"], ib["port"], timeout=10)

    def risk_snapshot(self):
        """失联告警的风险快照: 台账未平 lot + 状态 (在途卖单在 IB 服务器上不受影响)。"""
        try:
            c = sqlite3.connect(self.db_path)
            rows = c.execute("SELECT symbol, qty, state FROM lots"
                             " WHERE state NOT IN ('CLOSED','ERROR') ORDER BY lot_id").fetchall()
            c.close()
        except Exception as e:
            return f"(台账读取失败: {e})"
        if not rows:
            return "当前空仓, 无风险敞口"
        by = ", ".join(f"{s} x{q}[{st}]" for s, q, st in rows)
        return (f"未平 lot {len(rows)}: {by}\n"
                "已挂在 IB 服务器上的限价/追踪卖单不受网关断连影响, 仍会正常成交；"
                "但断连期间无法挂新单/改单。")

    def errored_steps_since(self, ts_iso):
        try:
            c = sqlite3.connect(self.db_path)
            rows = c.execute("SELECT DISTINCT step FROM executions"
                             " WHERE started_at >= ? AND status LIKE 'error%'", (ts_iso,)).fetchall()
            c.close()
            return [r[0] for r in rows]
        except Exception:
            return []

    def send(self, msg, level="warn"):
        try:
            self.notify.send("[网关哨兵] " + msg, level)
        except Exception:
            pass

    def on_confirmed_down(self):
        self.down_since = now_et()
        self.last_alert = time.time()
        self.send(f"🚨 网关 {self.cfg['ib']['host']}:{self.cfg['ib']['port']} 失联 "
                  f"(连续 {FAILS_TO_DOWN} 次 API 握手无响应, ~2 分钟)\n" + self.risk_snapshot(),
                  "critical")

    def on_still_down(self):
        if time.time() - self.last_alert >= REALERT_SEC:
            self.last_alert = time.time()
            mins = int((now_et() - self.down_since).total_seconds() / 60)
            self.send(f"网关仍失联 (已 {mins} 分钟)\n" + self.risk_snapshot(), "critical")

    def on_recovered(self):
        outage_start = self.down_since.isoformat(timespec="seconds")
        mins = int((now_et() - self.down_since).total_seconds() / 60)
        self.send(f"网关恢复 (失联 {mins} 分钟), 等 {WARMUP_SEC}s 登录稳定后评估恢复动作…")
        time.sleep(WARMUP_SEC)
        today = now_et().date()
        if self.restart_day != today:
            self.restart_day, self.restarts_today = today, 0
        errored = self.errored_steps_since(outage_start)
        ok, reason = should_restart(errored, self.restarts_today,
                                    self.last_restart_ts, time.time(), hhmm())
        if ok:
            self.restarts_today += 1
            self.last_restart_ts = time.time()
            r = subprocess.run(["systemctl", "restart", "autotrader"],
                               capture_output=True, text=True, timeout=60)
            self.send(("✅ 已重启守护进程 (今日第 %d 次): %s\n守护进程将自行补执行错过的步骤,"
                       " 结果见其后续播报" % (self.restarts_today, reason))
                      if r.returncode == 0 else
                      f"🚨 重启守护进程失败 (rc={r.returncode}): {r.stderr[:200]}",
                      "warn" if r.returncode == 0 else "critical")
        else:
            self.send(f"恢复动作: 不重启 ({reason})")
        self.down_since = None

    def run(self):
        self.send(f"哨兵启动, 每 {PROBE_SEC}s 探测 {self.cfg['ib']['host']}:{self.cfg['ib']['port']}")
        while True:
            up = self.probe()
            quiet = in_quiet_window(hhmm())
            if up:
                self.oks += 1
                self.fails = 0
                if self.down_since and self.oks >= OKS_TO_UP:
                    if quiet:
                        self.down_since = None  # 静默窗内的恢复: 不动作
                    else:
                        self.on_recovered()
            else:
                self.fails += 1
                self.oks = 0
                if not quiet:
                    if self.down_since is None and self.fails >= FAILS_TO_DOWN:
                        self.on_confirmed_down()
                    elif self.down_since is not None:
                        self.on_still_down()
            time.sleep(PROBE_SEC)


if __name__ == "__main__":
    Sentinel().run()
