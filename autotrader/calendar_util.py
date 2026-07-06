# -*- coding: utf-8 -*-
"""NYSE 交易日历: 假日/半日市感知, 生成每日事件时刻表 (ET)。"""
from datetime import datetime, timedelta

import exchange_calendars as xcals
import pandas as pd

from common import ET

_cal = xcals.get_calendar("XNYS")


def is_trading_day(d) -> bool:
    return _cal.is_session(pd.Timestamp(d))


def next_trading_day(d):
    ts = pd.Timestamp(d)
    if _cal.is_session(ts):
        return _cal.next_session(ts).date()
    return _cal.date_to_session(ts, direction="next").date()


def market_close_et(d) -> datetime:
    """该交易日的收盘时刻 (半日市自动 13:00)。"""
    ts = _cal.session_close(pd.Timestamp(d))
    return ts.tz_convert(ET).to_pydatetime()


def market_open_et(d) -> datetime:
    ts = _cal.session_open(pd.Timestamp(d))
    return ts.tz_convert(ET).to_pydatetime()


def todays_schedule(cfg, d):
    """给定交易日 d, 返回 [(name, datetime_et)] 事件表。
    收盘相关事件锚定实际收盘时刻 (兼容半日市); 其余用固定 ET 时刻。"""
    close = market_close_et(d)
    open_ = market_open_et(d)
    sched = []

    def at(hhmm, base_date=d):
        h, m = map(int, hhmm.split(":"))
        return datetime(base_date.year, base_date.month, base_date.day, h, m, tzinfo=ET)

    s = cfg["schedule_et"]
    sched.append(("gate_check", close - timedelta(minutes=27)))
    sched.append(("build_plan", close - timedelta(minutes=22)))
    sched.append(("submit_moc", close - timedelta(minutes=15)))
    sched.append(("confirm_fills", close + timedelta(minutes=10)))
    # 隔夜时段属于"下一交易日", 在其前一个日历日晚间开盘 (周日~周四 20:00 ET)。
    # 例: 周一交易日的隔夜挂单在周日 20:05; 跨周末持仓因此不会漏排。
    sched.append(("overnight_sells", at(s["overnight_sells"], base_date=d - timedelta(days=1))))
    sched.append(("premarket_sells", at(s["premarket_sells"])))
    sched.append(("open_trail", open_ + timedelta(minutes=1)))
    sched.append(("daily_report", close + timedelta(minutes=20)))
    return sorted(sched, key=lambda x: x[1])
