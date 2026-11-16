from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import yfinance as yf
from curl_cffi.requests import Session
from curl_cffi.requests.exceptions import RequestException

from ..calculations import NY, year_horizon
from ..models import Chain
from ..parsing import number, parse_yahoo_row
from .http import RequestGate


class ArchivedYahooSession(Session):
    def __init__(self, gate: RequestGate):
        super().__init__(impersonate="chrome")
        self.gate = gate

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
        self.gate.before()
        kwargs["timeout"] = self.gate.cfg["timeout_seconds"]
        try:
            response = super().request(method, url, *args, **kwargs)
        except RequestException as exc:
            raise RuntimeError(f"Yahoo transport failure ({type(exc).__name__})") from None
        self.gate.response(url, response)
        # yfinance must handle its own 401/403 crumb refresh; fc.yahoo.com's
        # cookie bootstrap can legitimately return 404 with Set-Cookie.
        if response.status_code == 429:
            raise RuntimeError(f"Yahoo HTTP {response.status_code}")
        return response


class YahooSource:
    def __init__(self, gate: RequestGate, cfg: dict[str, Any]):
        self.gate, self.cfg = gate, cfg
        self.session = ArchivedYahooSession(gate)

    def ticker(self, symbol: str) -> Any:
        self.gate.symbol = symbol
        return yf.Ticker(symbol, session=self.session)

    def chain(self, symbol: str, expiry: str | None = None) -> Chain:
        ticker = self.ticker(symbol)
        now = datetime.now(timezone.utc)
        today = now.astimezone(NY).date()
        expiries = [
            e
            for e in ticker.options
            if (
                e == expiry
                if expiry is not None
                else today <= date.fromisoformat(e) <= year_horizon(today)
            )
        ]
        if not expiries:
            raise ValueError(f"No Yahoo expirations for exact symbol {symbol}")
        rows = []
        spot = None
        for selected_expiry in expiries:
            chain = ticker.option_chain(selected_expiry)
            underlying = chain.underlying or {}
            returned_symbol = underlying.get("symbol")
            if returned_symbol != symbol:
                raise ValueError(f"Yahoo response symbol {returned_symbol!r} != {symbol}")
            spot = number(underlying.get("regularMarketPrice")) or spot
            for frame in (chain.calls, chain.puts):
                rows.extend(parse_yahoo_row(row) for row in frame.to_dict("records"))
        if not spot or not rows:
            raise ValueError(f"Incomplete Yahoo chain for {symbol}")
        # Yahoo exposes underlying market time, not an option-chain snapshot time.
        # Do not present lastTradeDate (last transaction) as quote freshness.
        return Chain(symbol, spot, None, "yfinance", rows, datetime.now(timezone.utc))

    def auxiliary(self, symbol: str) -> dict[str, Any]:
        ticker = self.ticker(symbol)
        history = ticker.history(
            period=self.cfg["daily_history_period"], auto_adjust=False, raise_errors=True
        )
        if history.empty:
            raise ValueError(f"Empty Yahoo daily history for {symbol}")
        calendar = ticker.calendar
        if calendar is None:
            raise ValueError(f"Yahoo corporate calendar unavailable for {symbol}")
        events = {
            "calendar": calendar,
            "note": "Unknown/unpublished event dates remain null; ETFs may have no earnings.",
        }
        for field in ("Earnings Date", "Ex-Dividend Date"):
            events[field] = calendar.get(field)
        errors = [
            f"corporate calendar HTTP {status}"
            for url, status in self.gate.statuses.items()
            if f"/quoteSummary/{symbol}" in url and status >= 400
        ]
        payload = {
            "history": json.loads(
                history.reset_index().to_json(orient="records", date_format="iso")
            ),
            "events": events,
            "errors": errors,
        }
        if errors:
            events["fetch_errors"] = errors
        self.gate.archive.write(self.gate.day, symbol, self.gate.label, "yfinance-aux", payload)
        return payload

    def close(self) -> None:
        self.session.close()
