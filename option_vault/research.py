from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .calculations import NY, iv_metrics
from .scoring import DEFAULTS, evaluate, scenarios


@contextmanager
def read_db(path: Path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


class Research:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = Path(cfg["data_dir"])
        self.scoring = {**DEFAULTS, **cfg.get("web", {}).get("scoring", {})}
        self._lock = threading.Lock()
        self._cache: tuple[float, list[dict]] = (float("-inf"), [])
        self._snapshots: dict[str, tuple[float, list[dict]]] = {}

    def prices(self, symbol: str, db: sqlite3.Connection) -> list[dict]:
        path = self.root / "research.sqlite3"
        if path.exists():
            with read_db(path) as research:
                exists = research.execute(
                    "SELECT 1 FROM imports WHERE symbol=?", (symbol,)
                ).fetchone()
                if exists:
                    return [
                        dict(row)
                        for row in research.execute(
                            "SELECT day,close FROM prices WHERE symbol=? ORDER BY day", (symbol,)
                        )
                    ]
        result = []
        for row in db.execute(
            "SELECT day,payload FROM daily_history WHERE symbol=? ORDER BY day", (symbol,)
        ):
            payload = json.loads(row["payload"])
            if payload.get("Close") is not None:
                result.append({"day": row["day"], "close": payload["Close"]})
        return result

    def ranked(self, snapshot: str | None = None) -> list[dict]:
        with self._lock:
            cache = self._snapshots.get(snapshot, (float("-inf"), [])) if snapshot else self._cache
            if time.monotonic() - cache[0] < 60:
                return cache[1]
            now = datetime.now(timezone.utc)
            results = []
            with read_db(self.root / "vault.sqlite3") as db:
                for symbol in self.cfg["symbols"]:
                    latest = db.execute(
                        "SELECT s.run_id FROM symbol_runs s JOIN runs r ON r.id=s.run_id "
                        "WHERE s.symbol=? AND s.status IN ('ok','warning') "
                        "AND r.ended IS NOT NULL "
                        + ("AND r.id=? " if snapshot else "")
                        + "ORDER BY r.started DESC LIMIT 1",
                        (symbol, snapshot) if snapshot else (symbol,),
                    ).fetchone()
                    if not latest:
                        continue
                    prices = self.prices(symbol, db)
                    windows: dict[tuple[int, str], list[dict]] = {}
                    history_ends: dict[str, str | None] = {}
                    for row in db.execute(
                        "SELECT * FROM contracts WHERE run_id=? AND symbol=?",
                        (latest["run_id"], symbol),
                    ):
                        item = dict(row)
                        collected = (
                            datetime.fromisoformat(item["collected_utc"]).astimezone(NY).date()
                        )
                        expiry = datetime.fromisoformat(item["expiry"]).date()
                        current_dte = (expiry - now.astimezone(NY).date()).days
                        if current_dte < 0 and not snapshot:
                            continue
                        # Historical horizons match the quote's original DTE, never the aging quote.
                        horizon = (expiry - collected).days
                        key = (horizon, collected.isoformat())
                        if key not in windows:
                            windows[key] = scenarios(
                                prices, horizon, collected, self.scoring["endpoint_tolerance_days"]
                            )
                        date_key = collected.isoformat()
                        if date_key not in history_ends:
                            history_ends[date_key] = next(
                                (p["day"] for p in reversed(prices) if p["day"] < date_key), None
                            )
                        item["dte"] = horizon
                        ranked = evaluate(
                            item,
                            windows[key],
                            now,
                            self.scoring,
                            self.cfg["quality"],
                            history_ends[date_key],
                        )
                        if current_dte != horizon:
                            ranked["reasons"].append("旧快照期限已变化，效率按报价原期限计算")
                        ranked["dte"] = horizon if snapshot else current_dte
                        if current_dte <= 0:
                            ranked["low_confidence"] = True
                            ranked["reasons"].append("已到期或今日到期，不作当前效率排名")
                        ranked["quote_dte"] = horizon
                        ranked["current_dte"] = current_dte
                        # Detailed cases are computed on demand, not sent for every ranking row.
                        ranked["statistics"] = {
                            k: v
                            for k, v in ranked["statistics"].items()
                            if k not in {"worst", "upside", "distribution"}
                        }
                        results.append(ranked)
            cached = (time.monotonic(), results)
            if snapshot:
                if len(self._snapshots) >= 8:
                    self._snapshots.pop(next(iter(self._snapshots)))
                self._snapshots[snapshot] = cached
            else:
                self._cache = cached
            return results

    def snapshots(self, day: str | None) -> dict:
        with read_db(self.root / "vault.sqlite3") as db:
            dates = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT r.day FROM runs r JOIN symbol_runs s ON r.id=s.run_id "
                    "WHERE r.ended IS NOT NULL ORDER BY r.day DESC"
                )
            ]
            selected = day or (dates[0] if dates else None)
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT DISTINCT r.id,r.label,r.status,r.started FROM runs r "
                    "JOIN symbol_runs s ON r.id=s.run_id WHERE r.day=? "
                    "AND r.ended IS NOT NULL ORDER BY r.started DESC",
                    (selected,),
                )
            ]
        return {"days": dates, "day": selected, "runs": rows}

    def overview(self) -> dict:
        with read_db(self.root / "vault.sqlite3") as db:
            iv = []
            histories = []
            for symbol in self.cfg["symbols"]:
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT day,symbol,iv,source,source_timestamp FROM daily_iv "
                        "WHERE symbol=? ORDER BY day",
                        (symbol,),
                    )
                ]
                latest = rows[-1] if rows else {"symbol": symbol, "iv": None, "day": None}
                metrics = (
                    iv_metrics(
                        latest["iv"],
                        [r["iv"] for r in rows[:-1]],
                        self.cfg["iv"]["history_observations"],
                    )
                    if rows
                    else {"rank": None, "percentile": None}
                )
                iv.append({**latest, **metrics, "history": rows})
                prices = self.prices(symbol, db)
                histories.append(
                    {
                        "symbol": symbol,
                        "count": len(prices),
                        "first": prices[0]["day"] if prices else None,
                        "last": prices[-1]["day"] if prices else None,
                    }
                )
            runs = [
                dict(r) for r in db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT 60")
            ]
        return {
            "iv": iv,
            "runs": runs,
            "histories": histories,
            "scoring": self.scoring,
            "symbols": self.cfg["symbols"],
            "watch": self.cfg.get("hourly_watch"),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        }

    def contract(self, symbol: str, code: str, snapshot: str | None = None) -> dict:
        with read_db(self.root / "vault.sqlite3") as db:
            history = [
                dict(row)
                for row in db.execute(
                    "SELECT collected_utc,collected_et,source_timestamp,source,bid,ask,last,mid,"
                    "iv,delta,gamma,theta,vega,volume,oi,spot,flags FROM contracts "
                    "WHERE symbol=? AND occ_symbol=? ORDER BY collected_utc",
                    (symbol, code),
                )
            ]
            watched = [
                dict(row)
                for row in db.execute(
                    "SELECT collected_utc,source_timestamp,source,bid,ask,last FROM watch_quotes "
                    "WHERE symbol=? AND occ_symbol=? ORDER BY collected_utc",
                    (symbol, code),
                )
            ]
            prices = self.prices(symbol, db)
        candidate = next(
            (r for r in self.ranked(snapshot) if r["symbol"] == symbol and r["occ_symbol"] == code),
            None,
        )
        statistics = None
        if candidate:
            cutoff = datetime.fromisoformat(candidate["collected_utc"]).astimezone(NY).date()
            windows = scenarios(
                prices, candidate["quote_dte"], cutoff, self.scoring["endpoint_tolerance_days"]
            )
            full = evaluate(
                {**candidate, "dte": candidate["quote_dte"]},
                windows,
                datetime.now(timezone.utc),
                self.scoring,
                self.cfg["quality"],
                next((p["day"] for p in reversed(prices) if p["day"] < cutoff.isoformat()), None),
                details=True,
            )
            statistics = full["statistics"]
        return {
            "candidate": candidate,
            "history": history,
            "watch": watched,
            "statistics": statistics,
        }
