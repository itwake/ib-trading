# -*- coding: utf-8 -*-
"""配置加载与基础工具。"""
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ET = ZoneInfo("America/New_York")


def load_config():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    if not os.path.isabs(cfg.get("db_path", "")):
        cfg["db_path"] = os.path.join(HERE, cfg.get("db_path") or "journal.db")
    return cfg


def now_et():
    return datetime.now(tz=ET)


def setup_logging():
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    logfile = os.path.join(HERE, "logs", f"autotrader_{datetime.now():%Y%m%d}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"), logging.StreamHandler()],
    )
    for noisy in ("ib_async.wrapper", "ib_async.client", "ib_async.ib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("autotrader")
