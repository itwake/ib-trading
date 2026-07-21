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
    买入链锚定实际收盘时刻 (兼容半日市); 卖出链三个时点由配置的分钟偏移决定:
      overnight_sells_offset_min  相对隔夜时段开盘 20:00 ET (前一日历日晚, 周日~周四)
      premarket_sells_offset_min  相对盘前时段开始 04:00 ET
      open_trail_offset_min       相对开盘 09:30 ET (负数=开盘前, 正数=开盘后)
    """
    close = market_close_et(d)
    open_ = market_open_et(d)
    s = cfg["schedule_et"]
    on_off = float(s.get("overnight_sells_offset_min", 5))
    pm_off = float(s.get("premarket_sells_offset_min", 5))
    tr_off = float(s.get("open_trail_offset_min", 1))
    md_off = float(s.get("midday_reconcile_offset_min", 145))  # 默认 11:55 ET, 须在 12:00 ET 前

    # 隔夜时段属于"下一交易日", 在其前一个日历日晚间开盘。
    # 例: 周一交易日的隔夜挂单在周日 20:00+offset; 跨周末持仓因此不会漏排。
    prev = d - timedelta(days=1)
    sched = [
        ("gate_check", close - timedelta(minutes=27)),
        ("build_plan", close - timedelta(minutes=22)),
        ("pre_attrib", close - timedelta(minutes=21)),  # 预买影子裁决 (LLM, 只记录不执行)
        ("submit_moc", close - timedelta(minutes=15)),
        ("confirm_fills", close + timedelta(minutes=10)),
        ("overnight_sells", datetime(prev.year, prev.month, prev.day, 20, 0, tzinfo=ET)
         + timedelta(minutes=on_off)),
        ("premarket_sells", datetime(d.year, d.month, d.day, 4, 0, tzinfo=ET)
         + timedelta(minutes=pm_off)),
        ("open_trail", open_ + timedelta(minutes=tr_off)),
        # 上海时区网关的"当日成交"窗口在 12:00 ET 翻页, 午前固化上午成交 (半日市 13:00 收盘同样适用)
        ("midday_reconcile", open_ + timedelta(minutes=md_off)),
        ("daily_report", close + timedelta(minutes=20)),
    ]
    return sorted(sched, key=lambda x: x[1])
