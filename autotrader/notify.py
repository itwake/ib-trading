# -*- coding: utf-8 -*-
"""Discord Webhook 通知 (无 webhook 时降级为日志)。"""
import logging

import requests

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def url(self):
        return self.cfg["notify"].get("discord_webhook", "")

    def send(self, msg: str, level: str = "info"):
        prefix = {"info": "", "warn": "⚠️ ", "critical": "🚨 "}.get(level, "")
        text = f"{prefix}{msg}"
        log.info("[notify] %s", text)
        if not self.url:
            return
        try:
            requests.post(self.url, json={"content": text[:1900]}, timeout=10)
        except Exception as e:
            log.error("discord 发送失败: %s", e)
