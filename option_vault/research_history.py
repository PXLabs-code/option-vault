from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .sources.http import RequestGate, retry
from .sources.yahoo import YahooSource
from .storage import Archive

LOG = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices(
 symbol TEXT NOT NULL, day TEXT NOT NULL, close REAL NOT NULL, PRIMARY KEY(symbol,day));
CREATE TABLE IF NOT EXISTS imports(
 symbol TEXT PRIMARY KEY, fetched_utc TEXT NOT NULL, first_day TEXT, last_day TEXT,
 observations INTEGER NOT NULL, source TEXT NOT NULL);
"""


def refresh(cfg: dict, symbols: list[str]) -> int:
    root = Path(cfg["data_dir"])
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    gate = RequestGate(
        cfg["http"], Archive(root), LOG.warning, now.date().isoformat(), "research-history"
    )
    yahoo = YahooSource(gate, cfg["sources"])
    failures = 0
    try:
        with sqlite3.connect(root / "research.sqlite3", timeout=30) as db:
            db.executescript(SCHEMA)
            for symbol in symbols:
                try:
                    frame = retry(
                        lambda symbol=symbol: yahoo.ticker(symbol).history(
                            period="max", auto_adjust=False, raise_errors=True
                        ),
                        cfg["http"],
                        LOG.warning,
                    )
                    if frame.empty:
                        raise ValueError(f"{symbol}: empty full price history")
                    payload = json.loads(
                        frame.reset_index().to_json(orient="records", date_format="iso")
                    )
                    gate.archive.write(
                        now.date().isoformat(),
                        symbol,
                        "research-history",
                        "max-daily-prices",
                        payload,
                    )
                    rows = [
                        (symbol, row["Date"][:10], row["Close"])
                        for row in payload
                        if row.get("Close") is not None and row["Close"] > 0
                    ]
                    if not rows:
                        raise ValueError(f"{symbol}: no valid closes")
                    with db:
                        # Replace the complete series to keep split adjustments consistent.
                        db.execute("DELETE FROM prices WHERE symbol=?", (symbol,))
                        db.executemany("INSERT INTO prices VALUES(?,?,?)", rows)
                        db.execute(
                            "INSERT OR REPLACE INTO imports VALUES(?,?,?,?,?,?)",
                            (
                                symbol,
                                now.isoformat(),
                                rows[0][1],
                                rows[-1][1],
                                len(rows),
                                "yfinance Close (split-adjusted, not dividend-adjusted)",
                            ),
                        )
                    LOG.info(
                        "%s: saved %s daily closes (%s to %s)",
                        symbol,
                        len(rows),
                        rows[0][1],
                        rows[-1][1],
                    )
                except Exception:
                    failures += 1
                    LOG.exception("%s history import failed; existing history retained", symbol)
    finally:
        yahoo.close()
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Import all available daily price history")
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--symbol", action="append")
    args = parser.parse_args()
    cfg = load_config(args.config)
    symbols = args.symbol or cfg["symbols"]
    if any(symbol not in cfg["symbols"] for symbol in symbols):
        parser.error("--symbol must exactly match a configured symbol")
    root = Path(cfg["data_dir"]) / "logs"
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                root / f"history-{datetime.now(timezone.utc):%Y-%m-%d}.log", encoding="utf-8"
            ),
        ],
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if refresh(cfg, symbols):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
