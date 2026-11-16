from __future__ import annotations

import logging
import sqlite3
from typing import Any

import requests

from .config import telegram_credentials
from .models import SymbolResult
from .storage import Storage

LOG = logging.getLogger(__name__)


class Notifier:
    def __init__(self, cfg: dict[str, Any], storage: Storage | None, disabled: bool = False):
        self.storage = storage
        self.enabled = cfg["telegram"]["enabled"] and not disabled
        self.timeout = cfg["telegram"]["timeout_seconds"]
        self.token, self.chat = telegram_credentials(cfg) if self.enabled else (None, None)
        if self.enabled and (not self.token or not self.chat):
            raise ValueError(
                "Telegram enabled but token/chat_id missing; configure or use --no-telegram"
            )
        self.failures = 0
        if storage is not None:
            try:
                self.failures = int(storage.state("telegram_failures") or "0")
            except (sqlite3.Error, ValueError):
                LOG.exception("Cannot read Telegram failure counter; alerts remain available")

    def persist_failures(self) -> None:
        if self.storage is not None:
            try:
                self.storage.set_state("telegram_failures", str(self.failures))
            except sqlite3.Error:
                LOG.exception("Cannot persist Telegram failure counter")

    def send(self, message: str) -> None:
        LOG.info("Notification: %s", message)
        if not self.enabled:
            return
        try:
            previous = self.failures
            prefix = f"⚠️ 上次成功后有 {previous} 次 Telegram 发送失败。\n" if previous else ""
            text = prefix + message
            # Telegram limits sendMessage to 4096 characters.
            for offset in range(0, len(text), 3500):
                response = requests.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": self.chat, "text": text[offset : offset + 3500]},
                    timeout=self.timeout,
                )
                if response.status_code != 200 or not response.json().get("ok"):
                    raise RuntimeError(
                        f"Telegram status={response.status_code}, API rejected message"
                    )
            self.failures = 0
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            # Never log requests exception strings: they may contain the bot token URL.
            LOG.error("Telegram send failed (%s); collection continues", type(exc).__name__)
            self.failures += 1
        self.persist_failures()

    def alert(self, message: str) -> None:
        self.send("⚠️ 采集告警\n" + message)

    def report(self, label: str, elapsed: float, results: list[SymbolResult]) -> None:
        lines = [f"数据获取报告 | {label} ET | {elapsed:.1f}s"]
        for r in results:
            icon = "❌" if r.status == "failed" else "⚠️" if r.status == "warning" else "✅"
            lag = f"{r.lag_seconds / 60:.1f}m" if r.lag_seconds is not None else "未知"
            lines.append(
                f"{icon} {r.symbol}: {r.contracts}份 | {r.source or '-'} | "
                f"标记 {r.flagged_ratio:.0%}/Bid0 {r.zero_bid_ratio:.0%} | 延迟 {lag}"
                + (f" | {r.error}" if r.error else "")
                + (f" | {'; '.join(r.warnings)}" if r.warnings else "")
            )
        self.send("\n".join(lines))
