# -*- coding: utf-8 -*-
"""影子出场实验: 对已平仓 lot 回填 '如果 T+1/T+2 收盘强制离场' 的假想盈亏。
与真实出场同一批交易、同一入场价, 一个月后即可用实盘数据对比三种出场规则。"""
import logging

log = logging.getLogger("shadow")


def _sim(bars, entry_px, target, ts_n):
    """bars: [(open, high, close)] 从 entry 次日起。返回假想出场价或 None(数据不足)。"""
    for k, (o, h, c) in enumerate(bars, start=1):
        if o >= target:
            return o
        if h >= target:
            return target
        if k >= ts_n:
            return c
    return None


def evaluate_shadows(db, notify=None, max_lots=50):
    lots = db.lots_needing_shadow()[:max_lots]
    if not lots:
        return 0
    import yfinance as yf

    done = 0
    for lot in lots:
        try:
            df = yf.download(lot["symbol"], start=lot["entry_date"], progress=False, auto_adjust=False)
            if hasattr(df.columns, "get_level_values") and df.columns.nlevels > 1:
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            dates = [str(d.date()) for d in df.index]
            if lot["entry_date"] not in dates:
                continue
            i0 = dates.index(lot["entry_date"])
            bars = [(float(r.Open), float(r.High), float(r.Close)) for r in df.iloc[i0 + 1:].itertuples()]
            if len(bars) < 2:
                continue  # T+2 数据还没齐, 下次再算
            t1_px = _sim(bars, lot["entry_price"], lot["target_price"], 1)
            t2_px = _sim(bars, lot["entry_price"], lot["target_price"], 2)
            if t1_px is None or t2_px is None:
                continue
            t1 = round((t1_px - lot["entry_price"]) * lot["qty"] - 2.0, 2)
            t2 = round((t2_px - lot["entry_price"]) * lot["qty"] - 2.0, 2)
            db.set_shadow(lot["lot_id"], t1, t2)
            done += 1
        except Exception as e:
            log.warning("shadow %s 失败: %s", lot["symbol"], e)
    if done and notify:
        log.info("影子实验回填 %d 笔", done)
    return done
