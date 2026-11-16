from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..models import Chain
from ..parsing import parse_cboe
from .http import CboeHTTP


class CboeSource:
    def __init__(self, http: CboeHTTP, cfg: dict[str, Any]):
        self.http, self.cfg = http, cfg

    def chain(self, symbol: str) -> Chain:
        self.http.gate.symbol = symbol
        payload = self.http.json(f"{self.cfg['cboe_base_url']}/options/{symbol}.json")
        return parse_cboe(
            payload, symbol, datetime.now(timezone.utc), self.cfg["cboe_timestamp_timezone"]
        )

    def quote(self, symbol: str) -> dict[str, Any]:
        self.http.gate.symbol = symbol
        return self.http.json(f"{self.cfg['cboe_base_url']}/quotes/{symbol}.json")
