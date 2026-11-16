from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from yfinance.exceptions import YFException

from ..storage import Archive


class RequestGate:
    def __init__(
        self,
        cfg: dict[str, Any],
        archive: Archive,
        alert: Callable[[str], None],
        day: str,
        label: str,
    ):
        self.cfg, self.archive, self.alert = cfg, archive, alert
        self.day, self.label = day, label
        self.next_request = 0.0
        self.symbol = "SYSTEM"
        self.statuses: dict[str, int] = {}

    def before(self) -> None:
        time.sleep(max(0, self.next_request - time.monotonic()))
        self.next_request = time.monotonic() + self.cfg["interval_seconds"]

    def response(self, url: str, response: Any) -> None:
        parsed = urlsplit(url)
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        self.statuses[clean_url] = response.status_code
        endpoint = parsed.path.rstrip("/").split("/")[-1].replace(".", "_") or "response"
        # Query strings, headers and cookies can contain Yahoo credentials; never persist them.
        if (
            parsed.netloc in {"fc.yahoo.com", "guce.yahoo.com", "consent.yahoo.com"}
            or "getcrumb" in parsed.path
        ):
            body = {"redacted": "Yahoo authentication response; not market data"}
        else:
            text = response.text
            try:
                body = json.loads(text)
            except ValueError:
                body = {"non_json_body": text}
        if response.status_code == 429:
            delay = self.cfg["max_backoff_seconds"]
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    try:
                        delay = max(
                            delay,
                            (
                                parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                            ).total_seconds(),
                        )
                    except (ValueError, TypeError):
                        pass
            self.next_request = max(self.next_request, time.monotonic() + delay)
            self.alert(f"{self.symbol} 请求限流 HTTP 429；暂停至少 {delay:.0f}s，接口 {endpoint}")
        try:
            self.archive.write(
                self.day,
                self.symbol,
                self.label,
                endpoint,
                {
                    "fetched_utc": datetime.now(timezone.utc).isoformat(),
                    "endpoint": clean_url,
                    "status": response.status_code,
                    "body": body,
                },
            )
        except OSError as exc:
            self.alert(f"{self.symbol} 原始响应存储写入失败：{type(exc).__name__}: {exc}")
            raise


class CboeHTTP:
    def __init__(self, gate: RequestGate):
        self.gate = gate
        self.session = requests.Session()
        self.session.headers["User-Agent"] = gate.cfg["user_agent"]

    def json(self, url: str) -> dict[str, Any]:
        self.gate.before()
        response = self.session.get(url, timeout=self.gate.cfg["timeout_seconds"])
        self.gate.response(url, response)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self.session.close()


def retry(
    operation: Callable[[], Any], cfg: dict[str, Any], on_retry: Callable[[str], None]
) -> Any:
    for attempt in range(cfg["retries"] + 1):
        try:
            return operation()
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            requests.RequestException,
            YFException,
        ) as exc:
            if attempt == cfg["retries"]:
                raise
            delay = min(cfg["max_backoff_seconds"], cfg["backoff_seconds"] * 2**attempt)
            on_retry(
                f"attempt {attempt + 1} failed ({type(exc).__name__}: {exc}); retry in {delay}s"
            )
            time.sleep(delay)
    raise RuntimeError("Unreachable retry state")
