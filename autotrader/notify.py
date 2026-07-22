# -*- coding: utf-8 -*-
"""Discord Webhook 通知 (无 webhook 时降级为日志)。"""
import logging

import requests

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.buffer = None  # 设为 list 时, 同步收集消息文本 (供执行流水记录)

    @property
    def url(self):
        return self.cfg["notify"].get("discord_webhook", "")

    def send(self, msg: str, level: str = "info"):
        prefix = {"info": "", "warn": "⚠️ ", "critical": "🚨 "}.get(level, "")
        text = f"{prefix}{msg}"
        log.info("[notify] %s", text)
        if self.buffer is not None:
            self.buffer.append(text)
        if not self.url:
            return
        try:
            # 同步调用跑在事件循环上, 会阻塞后续交易动作: 连接/读取分开设短超时,
            # 标量 timeout=10 实际最坏 ~20s+DNS, 曾挤占买入链的安全边际。
            requests.post(self.url, json={"content": text[:1900]}, timeout=(3, 5))
        except Exception as e:
            log.error("discord 发送失败: %s", e)
