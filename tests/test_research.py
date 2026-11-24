import csv
import io
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from option_vault.calculations import NY, decorate
from option_vault.models import SymbolResult
from option_vault.research import Research, read_db
from option_vault.research_history import SCHEMA, refresh
from option_vault.scoring import DEFAULTS, evaluate, scenarios, summarize, weighted_score
from option_vault.storage import Storage
from option_vault.web import create_app


def test_nonoverlapping_windows_never_see_quote_date_or_future():
    prices = [
        {"day": (date(2020, 1, 1) + timedelta(days=i)).isoformat(), "close": 100 + i}
        for i in range(25)
    ]
    samples = scenarios(prices, 7, date(2020, 1, 24))
    assert [(s["start"], s["end"]) for s in samples] == [
        ("2020-01-01", "2020-01-08"),
        ("2020-01-09", "2020-01-16"),
    ]
    assert samples[0]["return"] == pytest.approx(0.07)
    assert scenarios(prices, 0, date(2020, 1, 24)) == []
    assert scenarios(prices, 365, date(2020, 1, 24)) == []


def test_weekend_endpoints_and_large_gaps_are_not_fabricated():
    prices = [
        {"day": day, "close": close}
        for day, close in [
            ("2020-01-03", 100),
            ("2020-01-06", 90),
            ("2020-01-10", 80),
            ("2020-02-20", 500),
        ]
    ]
    samples = scenarios(prices, 9, date(2020, 3, 1), tolerance=2)
    assert len(samples) == 1
    assert samples[0]["end"] == "2020-01-10"
    assert samples[0]["days"] == 7


def test_otm_is_not_profit_or_assignment_and_call_risks_separate():
    samples = [
        {"start": "2020-01-01", "end": "2020-02-01", "days": 31, "return": r}
        for r in [-0.5, -0.1, 0.2]
    ]
    put = summarize(samples, 0.9, "P", details=True)
    call = summarize(samples, 1.1, "C", details=True)
    assert put["rate"] == pytest.approx(2 / 3)
    assert call["rate"] == pytest.approx(2 / 3)
    assert put["worst"][0]["return"] == -0.5
    assert call["worst"][0]["return"] == -0.5  # A falling stock can still be an OTM call.
    assert call["worst"][0]["otm"] is True
    assert call["upside"][0]["return"] == 0.2
    assert 0 <= put["interval"][0] < put["rate"] < put["interval"][1] <= 1


def test_scoring_quality_missing_inputs_and_strategy_denominator(option, cfg):
    now = datetime.now(timezone.utc)
    row = {
        **option,
        "symbol": "SPY",
        "spot": 100,
        "collected_utc": now.isoformat(),
        "source_timestamp": now.isoformat(),
        "dte": 30,
    }
    samples = [{"start": "2020-01-01", "end": "2020-02-01", "days": 31, "return": 0.1}]
    call = evaluate(row, samples, now, DEFAULTS, cfg["quality"], now.date().isoformat())
    put = evaluate(
        {**row, "side": "P"}, samples, now, DEFAULTS, cfg["quality"], now.date().isoformat()
    )
    assert call["efficiency"] == pytest.approx(1.1 / 100 * 365 / 30)
    assert put["efficiency"] == pytest.approx(1.1 / 105 * 365 / 30)
    assert call["score"] is not None and call["low_confidence"] is True
    assert (
        evaluate({**row, "bid": 0}, samples, now, DEFAULTS, cfg["quality"], None)["score"] is None
    )
    assert evaluate(row, [], now, DEFAULTS, cfg["quality"], None)["score"] is None
    assert (
        weighted_score({"efficiency": 20, "history": None}, {"efficiency": 100, "history": 0}) == 20
    )
    assert weighted_score({"efficiency": 20}, {"efficiency": 0}) is None


@pytest.fixture
def web_data(cfg, chain):
    cfg["symbols"] = ["SPY"]
    chain.fetched_at = datetime.now(timezone.utc).replace(hour=18)
    chain.timestamp = chain.fetched_at
    expiry = chain.fetched_at.astimezone(NY).date() + timedelta(days=30)
    code = "SPY" + expiry.strftime("%y%m%d") + "C00105000"
    chain.options[0].update(occ_symbol=code, expiry=expiry.isoformat())
    root = Path(cfg["data_dir"])
    store = Storage(root)
    run = store.start(chain.fetched_at.date().isoformat(), "10:00")
    store.save_symbol(run, SymbolResult("SPY", "ok", "cboe", 1), decorate(chain, cfg))
    store.finish(run, "ok")
    store.save_watch_quote(run, "SPY", chain, chain.options[0])
    first = chain.fetched_at.date() - timedelta(days=365 * 5)
    with sqlite3.connect(root / "research.sqlite3") as db:
        db.executescript(SCHEMA)
        rows = [
            ("SPY", (first + timedelta(days=i)).isoformat(), 100 * (1.0001**i))
            for i in range(365 * 5)
        ]
        db.executemany("INSERT INTO prices VALUES(?,?,?)", rows)
        db.execute(
            "INSERT INTO imports VALUES(?,?,?,?,?,?)",
            ("SPY", chain.fetched_at.isoformat(), rows[0][1], rows[-1][1], len(rows), "test"),
        )
    store.close()
    return cfg, code, root


def test_read_only_api_filters_pagination_weights_and_csv(web_data):
    cfg, code, root = web_data
    app = create_app(cfg)
    client = app.test_client()
    data = client.get("/api/rankings?include_low=1&strategy=C").get_json()
    assert data["total"] == 1 and data["rows"][0]["occ_symbol"] == code
    assert data["rows"][0]["statistics"]["samples"] > 20
    assert "distribution" not in data["rows"][0]["statistics"]
    assert client.get("/api/rankings?include_low=1&strategy=P").get_json()["total"] == 0
    assert client.get("/api/rankings?include_low=1&strategy=C&page=2").get_json()["rows"] == []
    assert client.get("/api/rankings?page=nan").status_code == 400
    assert client.get("/api/rankings?efficiency=-1").status_code == 400
    assert client.get("/api/contract?symbol=SPY&code=../../config.local.json").status_code == 400
    detail = client.get(f"/api/contract?symbol=SPY&code={code}").get_json()
    assert len(detail["history"]) == 1 and len(detail["watch"]) == 1
    assert detail["statistics"]["worst"]
    assert client.get("/api/overview").get_json()["histories"][0]["count"] == 365 * 5
    response = client.get("/api/export?kind=rankings&include_low=1&strategy=C")
    assert response.status_code == 200 and "text/csv" in response.content_type
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True).lstrip("\ufeff"))))
    assert rows[0]["occ_symbol"] == code and "score_weights" in rows[0]
    assert client.get("/api/export?kind=watch&symbol=SPY&code=" + code).status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/assets/plotly.js").status_code == 200
    assert client.get("/static/app.js").headers["X-Content-Type-Options"] == "nosniff"
    with read_db(root / "vault.sqlite3") as db, pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM contracts")


def test_old_quotes_do_not_gain_annualized_yield_as_they_age(web_data):
    cfg, _code, root = web_data
    with sqlite3.connect(root / "vault.sqlite3") as db:
        collected = datetime.now(timezone.utc) - timedelta(days=10)
        db.execute(
            "UPDATE contracts SET collected_utc=?,collected_et=?",
            (collected.isoformat(), collected.astimezone(NY).isoformat()),
        )
    row = Research(cfg).ranked()[0]
    assert row["quote_dte"] > row["dte"]
    assert row["efficiency"] == pytest.approx(row["mid"] / row["spot"] * 365 / row["quote_dte"])
    assert row["low_confidence"]
    snapshot_row = Research(cfg).ranked(row["run_id"])[0]
    assert snapshot_row["dte"] == snapshot_row["quote_dte"]
    assert snapshot_row["current_dte"] < snapshot_row["quote_dte"]


def test_expired_contract_remains_browsable_only_in_historical_snapshot(web_data):
    cfg, code, root = web_data
    with sqlite3.connect(root / "vault.sqlite3") as db:
        collected = datetime.now(timezone.utc) - timedelta(days=31)
        expiry = datetime.now(NY).date() - timedelta(days=1)
        db.execute(
            "UPDATE contracts SET collected_utc=?,collected_et=?,expiry=?",
            (collected.isoformat(), collected.astimezone(NY).isoformat(), expiry.isoformat()),
        )
        snapshot = db.execute("SELECT id FROM runs").fetchone()[0]
    research = Research(cfg)
    assert research.ranked() == []
    historical = research.contract("SPY", code, snapshot)["candidate"]
    assert historical["current_dte"] == -1
    assert historical["quote_dte"] == 30


def test_cache_first_read_cannot_return_uninitialized_empty_result(web_data, monkeypatch):
    cfg, _, _ = web_data
    monkeypatch.setattr("option_vault.research.time.monotonic", lambda: 0.001)
    assert len(Research(cfg).ranked()) == 1


def test_missing_database_reports_failure_not_success_shaped_empty(cfg):
    app = create_app(cfg)
    response = app.test_client().get("/api/rankings")
    assert response.status_code == 500
    assert "error" in response.get_json()


def test_latest_snapshot_does_not_resurrect_disappeared_contract(web_data):
    cfg, _, root = web_data
    store = Storage(root)
    newer = store.start("2099-01-01", "11:00")
    store.db.execute("UPDATE runs SET started='2099-01-01T12:00:00+00:00' WHERE id=?", (newer,))
    store.save_symbol(newer, SymbolResult("SPY", "ok", "cboe", 0), [])
    store.finish(newer, "ok")
    store.close()
    assert Research(cfg).ranked() == []


def test_snapshot_selection_is_isolated_and_export_retains_weights(web_data):
    cfg, code, _root = web_data
    app = create_app(cfg)
    client = app.test_client()
    snapshots = client.get("/api/snapshots").get_json()
    assert len(snapshots["days"]) == 1
    snapshot = snapshots["runs"][0]["id"]
    query = f"strategy=C&include_low=1&snapshot={snapshot}"
    result = client.get(f"/api/rankings?{query}").get_json()
    assert result["rows"][0]["occ_symbol"] == code
    assert result["rows"][0]["dte"] == result["rows"][0]["quote_dte"]
    assert client.get("/api/rankings?snapshot=../../etc").status_code == 400
    assert client.get("/api/snapshots?day=2026-99-99").status_code == 400
    csv_response = client.get(f"/api/export?{query}&efficiency=100&history=0&tail=0&quality=0")
    rows = list(csv.DictReader(io.StringIO(csv_response.get_data(as_text=True).lstrip("\ufeff"))))
    assert json.loads(rows[0]["score_weights"])["efficiency"] == 100
    assert float(rows[0]["score"]) == pytest.approx(json.loads(rows[0]["parts"])["efficiency"])


def test_history_import_failure_preserves_existing_series(web_data, monkeypatch):
    cfg, _code, root = web_data
    yahoo = Mock()
    yahoo.ticker.return_value.history.side_effect = ValueError("unavailable")
    monkeypatch.setattr("option_vault.research_history.YahooSource", lambda *args: yahoo)
    assert refresh(cfg, ["SPY"]) == 1
    with read_db(root / "research.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 365 * 5


def test_history_import_replaces_split_adjusted_series_atomically(web_data, monkeypatch):
    cfg, _code, root = web_data
    yahoo = Mock()
    frame = Mock(empty=False)
    frame.reset_index.return_value.to_json.return_value = json.dumps(
        [
            {"Date": "2020-01-01T00:00:00.000Z", "Close": 20},
            {"Date": "2020-01-02T00:00:00.000Z", "Close": 21},
        ]
    )
    yahoo.ticker.return_value.history.return_value = frame
    monkeypatch.setattr("option_vault.research_history.YahooSource", lambda *args: yahoo)
    assert refresh(cfg, ["SPY"]) == 0
    yahoo.ticker.return_value.history.assert_called_once_with(
        period="max", auto_adjust=False, raise_errors=True
    )
    with read_db(root / "research.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 2
        assert db.execute("SELECT first_day,last_day FROM imports").fetchone()[0] == "2020-01-01"


def test_backup_includes_consistent_research_database(web_data, tmp_path):
    cfg, _code, _root = web_data
    cfg["http"].update(backoff_seconds=1, max_backoff_seconds=1)
    path = tmp_path / "web-config.json"
    path.write_text(json.dumps(cfg))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "option_vault.cli",
            "--config",
            str(path),
            "--no-telegram",
            "backup",
            "--destination",
            str(tmp_path / "backup"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    target = Path(result.stdout.strip())
    with read_db(target / "research.sqlite3") as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 365 * 5


def test_negative_oi_cannot_produce_negative_quality_score(option, cfg):
    now = datetime.now(timezone.utc)
    row = {
        **option,
        "symbol": "SPY",
        "oi": -500,
        "spot": 100,
        "dte": 30,
        "collected_utc": now.isoformat(),
        "source_timestamp": now.isoformat(),
    }
    score = evaluate(row, [], now, DEFAULTS, cfg["quality"], None)
    assert score["parts"]["quality"] == 0
    assert score["low_confidence"]
