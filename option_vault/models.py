from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class Chain:
    symbol: str
    spot: float
    timestamp: datetime | None
    source: str
    options: list[dict[str, Any]]
    fetched_at: datetime


class Source(Protocol):
    def chain(self, symbol: str) -> Chain: ...


@dataclass
class SymbolResult:
    symbol: str
    status: str
    source: str | None = None
    contracts: int = 0
    elapsed: float = 0
    error: str | None = None
    flagged_ratio: float = 0
    zero_bid_ratio: float = 0
    lag_seconds: float | None = None
    warnings: list[str] = field(default_factory=list)
