from __future__ import annotations

import gzip
import json
import os
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SymbolResult

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, day TEXT NOT NULL, label TEXT NOT NULL,
 started TEXT NOT NULL, ended TEXT, status TEXT NOT NULL, error TEXT);
CREATE INDEX IF NOT EXISTS runs_slot ON runs(day,label);
CREATE TABLE IF NOT EXISTS symbol_runs(
 run_id TEXT NOT NULL REFERENCES runs(id), symbol TEXT NOT NULL,
 status TEXT NOT NULL, source TEXT, contracts INTEGER NOT NULL,
 elapsed REAL NOT NULL, error TEXT, flagged_ratio REAL, zero_bid_ratio REAL,
 lag_seconds REAL, warnings TEXT, PRIMARY KEY(run_id,symbol));
CREATE TABLE IF NOT EXISTS contracts(
 run_id TEXT NOT NULL REFERENCES runs(id), symbol TEXT NOT NULL,
 occ_symbol TEXT NOT NULL, collected_utc TEXT, collected_et TEXT, source_timestamp TEXT,
 spot REAL, expiry TEXT, strike REAL, side TEXT,
 bid REAL, ask REAL, last REAL, volume REAL, oi REAL, iv REAL,
 delta REAL, gamma REAL, theta REAL, vega REAL, source TEXT, mid REAL,
 dte INTEGER, annualized_yield REAL, spread REAL, spread_ratio REAL, flags TEXT,
 PRIMARY KEY(run_id,symbol,occ_symbol));
CREATE INDEX IF NOT EXISTS contracts_lookup ON contracts(symbol,expiry,collected_utc);
CREATE TABLE IF NOT EXISTS daily_iv(
 day TEXT NOT NULL, symbol TEXT NOT NULL, iv REAL NOT NULL,
 source TEXT NOT NULL, source_timestamp TEXT, target_days INTEGER NOT NULL,
 inputs TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id), PRIMARY KEY(day,symbol));
CREATE TABLE IF NOT EXISTS daily_history(
 symbol TEXT NOT NULL, day TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(symbol,day));
CREATE TABLE IF NOT EXISTS corporate_events(
 symbol TEXT PRIMARY KEY, collected_utc TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watch_quotes(
 run_id TEXT PRIMARY KEY REFERENCES runs(id), symbol TEXT NOT NULL,
 occ_symbol TEXT NOT NULL, collected_utc TEXT NOT NULL, source_timestamp TEXT,
 source TEXT NOT NULL, bid REAL, ask REAL, last REAL);
"""


class Archive:
    def __init__(self, root: Path):
        self.root = root

    def write(self, day: str, symbol: str, label: str, endpoint: str, payload: Any) -> Path:
        folder = self.root / "raw" / day / symbol / label.replace(":", "")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{endpoint}-{uuid.uuid4().hex}.json.gz"
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("wb") as raw:
                with gzip.open(raw, "wt", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=True, default=str, allow_nan=False)
                raw.flush()
                os.fsync(raw.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return path


class Storage:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "vault.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def start(self, day: str, label: str) -> str:
        run = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,NULL,'running',NULL)",
                (run, day, label, datetime.now(timezone.utc).isoformat()),
            )
        return run

    def finish(self, run: str, status: str, error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET ended=?,status=?,error=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), status, error, run),
            )

    def save_symbol(
        self,
        run: str,
        result: SymbolResult,
        rows: list[dict[str, Any]],
        iv: dict[str, Any] | None = None,
    ) -> None:
        values = asdict(result)
        values["warnings"] = json.dumps(values["warnings"])
        with self.db:
            keys = ["run_id", *values]
            self.db.execute(
                f"INSERT INTO symbol_runs({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                [run, *values.values()],
            )
            if rows:
                keys = ["run_id", *rows[0]]
                self.db.executemany(
                    f"INSERT INTO contracts({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                    [
                        [run, *(json.dumps(v) if k == "flags" else v for k, v in r.items())]
                        for r in rows
                    ],
                )
            if iv is not None:
                entry = {**iv, "inputs": json.dumps(iv["inputs"]), "run_id": run}
                keys = list(entry)
                self.db.execute(
                    f"INSERT OR REPLACE INTO daily_iv({','.join(keys)}) "
                    f"VALUES({','.join('?' for _ in keys)})",
                    list(entry.values()),
                )

    def state(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, value))

    def save_watch_quote(self, run: str, symbol: str, chain: Any, quote: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO watch_quotes VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run,
                    symbol,
                    quote["occ_symbol"],
                    chain.fetched_at.isoformat(),
                    chain.timestamp.isoformat() if chain.timestamp else None,
                    chain.source,
                    quote["bid"],
                    quote["ask"],
                    quote["last"],
                ),
            )

    def slot_status(self, day: str, label: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM runs WHERE day=? AND label=? ORDER BY started DESC LIMIT 1",
            (day, label),
        ).fetchone()
        return row[0] if row else None

    def previous_counts(self, symbol: str, day: str, label: str) -> list[int]:
        previous = self.db.execute(
            "SELECT s.contracts FROM symbol_runs s JOIN runs r ON r.id=s.run_id "
            "WHERE s.symbol=? AND s.status IN ('ok','warning') AND r.ended IS NOT NULL "
            "ORDER BY r.started DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        same_slot = self.db.execute(
            "SELECT s.contracts FROM symbol_runs s JOIN runs r ON r.id=s.run_id "
            "WHERE s.symbol=? AND s.status IN ('ok','warning') AND r.label=? AND r.day<? "
            "ORDER BY r.day DESC LIMIT 1",
            (symbol, label, day),
        ).fetchone()
        return [r[0] for r in (previous, same_slot) if r]

    def save_auxiliary(self, symbol: str, payload: dict[str, Any]) -> None:
        with self.db:
            for row in payload["history"]:
                self.db.execute(
                    "INSERT OR REPLACE INTO daily_history VALUES(?,?,?)",
                    (symbol, str(row["Date"])[:10], json.dumps(row, default=str)),
                )
            self.db.execute(
                "INSERT OR REPLACE INTO corporate_events VALUES(?,?,?)",
                (
                    symbol,
                    datetime.now(timezone.utc).isoformat(),
                    json.dumps(payload["events"], default=str),
                ),
            )

    def iv_history(self, symbol: str, before: str) -> list[float]:
        return [
            r[0]
            for r in self.db.execute(
                "SELECT iv FROM daily_iv WHERE symbol=? AND day<? ORDER BY day", (symbol, before)
            )
        ]

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
