# -*- coding: utf-8 -*-
"""每日备份 journal.db (在线安全备份), 保留最近 30 份; 顺带清理 30 天前的日志。"""
import glob
import os
import sqlite3
import sys
import time

sys.path.insert(0, "/opt/ib-trading/autotrader")
from common import load_config  # noqa: E402

cfg = load_config()
bdir = "/opt/ib-trading/backups"
os.makedirs(bdir, exist_ok=True)

dst = os.path.join(bdir, f"journal_{time.strftime('%Y%m%d')}.db")
src = sqlite3.connect(cfg["db_path"])
out = sqlite3.connect(dst)
with out:
    src.backup(out)
out.close()
src.close()
print("backup ->", dst)

backups = sorted(glob.glob(os.path.join(bdir, "journal_*.db")))
for old in backups[:-30]:
    os.remove(old)

cutoff = time.time() - 30 * 86400
for f in glob.glob("/opt/ib-trading/autotrader/logs/*.log"):
    if os.path.getmtime(f) < cutoff:
        os.remove(f)
