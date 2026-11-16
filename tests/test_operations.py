import gzip
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from option_vault.notifications import Notifier
from option_vault.runner import Runner
from option_vault.schedule import day_slots, hourly_slots
from option_vault.sources.http import RequestGate, retry
from option_vault.sources.yahoo import ArchivedYahooSession, YahooSource
from option_vault.storage import Archive, Storage


def test_regular_early_close_holiday_and_dst(cfg):
    normal = day_slots(date(2026, 11, 16), cfg["schedule"])
    assert [s.label for s in normal] == [
        "10:00",
        "11:00",
        "12:00",
        "13:00",
        "14:00",
        "15:00",
        "15:50",
        "16:15-close",
    ]
    early = day_slots(date(2026, 11, 27), cfg["schedule"])
    assert early[-1].label == "13:15-close"
    assert all(s.at.hour < 13 for s in early[:-1])
    assert day_slots(date(2026, 11, 26), cfg["schedule"]) == []
    assert day_slots(date(2026, 11, 15), cfg["schedule"]) == []
    winter = day_slots(date(2026, 3, 6), cfg["schedule"])[0].at
    summer = day_slots(date(2026, 3, 9), cfg["schedule"])[0].at
    assert winter.astimezone(timezone.utc).hour == 15
    assert summer.astimezone(timezone.utc).hour == 14


def test_retry_limit(cfg):
    operation = Mock(side_effect=RuntimeError("unavailable"))
    with pytest.raises(RuntimeError):
        retry(operation, cfg["http"], Mock())
    assert operation.call_count == 3


def test_rate_limit_archived_and_cooldown(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("option_vault.sources.http.time.monotonic", lambda: 10)
    alert = Mock()
    gate = RequestGate(cfg["http"], Archive(tmp_path), alert, "2026-11-16", "10:00")
    gate.symbol = "SPCX"
    response = Mock(status_code=429, text='{"error":"rate limit"}', headers={"Retry-After": "120"})
    gate.response("https://example.test/chain?crumb=SECRET", response)
    assert gate.next_request == 130
    alert.assert_called_once()
    assert len(list(tmp_path.rglob("*.gz"))) == 1


def test_telegram_failure_recovery_and_token_not_logged(cfg, tmp_path, monkeypatch, caplog):
    cfg["telegram"]["enabled"] = True
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-for-test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    store = Storage(tmp_path)
    try:
        notifier = Notifier(cfg, store)
        post = Mock(side_effect=requests.ConnectionError("SECRET URL"))
        monkeypatch.setattr("option_vault.notifications.requests.post", post)
        notifier.send("first")
        assert store.state("telegram_failures") == "1"
        assert "SECRET" not in caplog.text
        post.side_effect = None
        post.return_value = Mock(status_code=200, json=lambda: {"ok": True})
        notifier.send("second")
        assert "1 次" in post.call_args.kwargs["json"]["text"]
        assert store.state("telegram_failures") == "0"
    finally:
        store.close()


def test_missing_slots_and_interrupted_recovery(cfg, tmp_path):
    store = Storage(tmp_path)
    try:
        notify = Mock()
        runner = Runner(cfg, store, notify)
        run = store.start("2026-11-16", "10:00")
        runner.recover()
        assert store.slot_status("2026-11-16", "10:00") == "interrupted"
        now = datetime(2026, 11, 16, 17, 15, tzinfo=timezone.utc)
        slots = day_slots(date(2026, 11, 16), cfg["schedule"])
        runner.check_missed(now, slots)
        assert "11:00,12:00" in notify.alert.call_args.args[0]
        count = notify.alert.call_count
        runner.check_missed(now, slots)
        assert notify.alert.call_count == count
        assert (
            store.db.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0]
            == "interrupted"
        )
    finally:
        store.close()


def test_tick_claims_slot_once_and_does_not_backfill(cfg, tmp_path, monkeypatch):
    store = Storage(tmp_path)
    try:
        runner = Runner(cfg, store, Mock())

        def collect(label, symbols, closing=False):
            run = store.start("2026-11-16", label)
            store.finish(run, "ok")

        mock = Mock(side_effect=collect)
        monkeypatch.setattr(runner, "collect", mock)
        now = datetime(2026, 11, 16, 15, 1, tzinfo=timezone.utc)
        runner.tick(now)
        runner.tick(now + timedelta(minutes=1))
        assert mock.call_count == 1
        runner.tick(now + timedelta(hours=2, minutes=20))
        assert mock.call_count == 1
    finally:
        store.close()


def test_symbol_isolation_fallback_and_persisted_counts(cfg, tmp_path, chain, monkeypatch):
    cfg["symbols"] = ["SPCX", "SPY"]
    primary = Mock()
    primary.chain.side_effect = lambda symbol: (
        (_ for _ in ()).throw(ValueError("no chain")) if symbol == "SPCX" else chain
    )
    yahoo = Mock()
    yahoo.chain.side_effect = ValueError("no backup")
    yahoo.auxiliary.return_value = {"history": [], "events": {}}
    monkeypatch.setattr("option_vault.runner.CboeSource", lambda *a: primary)
    monkeypatch.setattr("option_vault.runner.YahooSource", lambda *a: yahoo)
    store = Storage(tmp_path)
    try:
        notify = Mock()
        run = Runner(cfg, store, notify).collect("manual-test", cfg["symbols"])
        rows = store.db.execute(
            "SELECT symbol,status,contracts FROM symbol_runs ORDER BY symbol"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("SPCX", "failed", 0), ("SPY", "ok", 1)]
        assert (
            store.db.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0]
            == "partial"
        )
        assert primary.chain.call_count == 4 and yahoo.chain.call_count == 3
        notify.report.assert_called_once()
        assert any("切换备用" in c.args[0] for c in notify.alert.call_args_list)
    finally:
        store.close()


def test_daily_iv_storage_and_summary(cfg, tmp_path, chain, monkeypatch):
    cfg["symbols"] = ["SPY"]
    chain.options = [dict(chain.options[0], strike=100, expiry="2026-12-16")]
    primary, yahoo = Mock(), Mock()
    primary.chain.return_value = chain
    yahoo.auxiliary.return_value = {"history": [], "events": {}}
    monkeypatch.setattr("option_vault.runner.CboeSource", lambda *a: primary)
    monkeypatch.setattr("option_vault.runner.YahooSource", lambda *a: yahoo)
    store = Storage(tmp_path)
    try:
        notifier = Mock()
        Runner(cfg, store, notifier).collect("16:15-close", ["SPY"], closing=True)
        row = store.db.execute("SELECT * FROM daily_iv").fetchone()
        assert row["iv"] == 0.3
        assert json.loads(row["inputs"])[0]["contracts"][0]["strike"] == 100
        assert "标准化 IV：" in notifier.send.call_args.args[0]
    finally:
        store.close()


def test_yahoo_cookie_404_is_not_market_data_failure(cfg, tmp_path, monkeypatch):
    gate = RequestGate(cfg["http"], Archive(tmp_path), Mock(), "2026-11-16", "10:00")
    session = ArchivedYahooSession(gate)
    response = Mock(status_code=404, text="PRIVATE-AUTH-RESPONSE", headers={})
    monkeypatch.setattr("curl_cffi.requests.Session.request", Mock(return_value=response))
    try:
        assert session.get("https://fc.yahoo.com") is response
        path = next(tmp_path.rglob("*.gz"))
        with gzip.open(path, "rt") as handle:
            assert "PRIVATE-AUTH-RESPONSE" not in handle.read()
        response.status_code = 429
        with pytest.raises(RuntimeError, match="429"):
            session.get("https://query2.finance.yahoo.com/market/data")
    finally:
        session.close()


def test_early_close_grace_does_not_collect_after_market_close(cfg, tmp_path, monkeypatch):
    store = Storage(tmp_path)
    try:
        runner = Runner(cfg, store, Mock())
        collect = Mock()
        monkeypatch.setattr(runner, "collect", collect)
        runner.tick(datetime(2026, 11, 27, 18, 1, tzinfo=timezone.utc))
        collect.assert_not_called()
    finally:
        store.close()


def test_successful_fallback_and_partial_auxiliary_are_visible(cfg, tmp_path, chain, monkeypatch):
    cfg["symbols"] = ["SPY"]
    chain.source, chain.timestamp = "yfinance", None
    for key in ("delta", "gamma", "theta", "vega"):
        chain.options[0][key] = None
    primary, yahoo = Mock(), Mock()
    primary.chain.side_effect = ValueError("Cboe unavailable")
    yahoo.chain.return_value = chain
    yahoo.auxiliary.return_value = {
        "history": [{"Date": "2026-11-16", "Close": 100}],
        "events": {"fetch_errors": ["calendar HTTP 404"]},
        "errors": ["calendar HTTP 404"],
    }
    monkeypatch.setattr("option_vault.runner.CboeSource", lambda *a: primary)
    monkeypatch.setattr("option_vault.runner.YahooSource", lambda *a: yahoo)
    store = Storage(tmp_path)
    try:
        notifier = Mock()
        Runner(cfg, store, notifier).collect("manual-test", ["SPY"])
        row = store.db.execute("SELECT source,delta,flags FROM contracts").fetchone()
        assert row["source"] == "yfinance" and row["delta"] is None
        assert "missing_greeks" in row["flags"]
        assert store.db.execute("SELECT COUNT(*) FROM daily_history").fetchone()[0] == 1
        assert store.db.execute("SELECT status FROM symbol_runs").fetchone()[0] == "warning"
        assert any("日线已保存" in c.args[0] for c in notifier.alert.call_args_list)
    finally:
        store.close()


def test_storage_failure_is_not_reported_as_saved(cfg, tmp_path, chain, monkeypatch):
    cfg["symbols"] = ["SPY"]
    primary, yahoo = Mock(), Mock()
    primary.chain.return_value = chain
    yahoo.auxiliary.return_value = {"history": [], "events": {}}
    monkeypatch.setattr("option_vault.runner.CboeSource", lambda *a: primary)
    monkeypatch.setattr("option_vault.runner.YahooSource", lambda *a: yahoo)
    store = Storage(tmp_path)
    original = store.save_symbol

    def save(run, result, rows, iv=None):
        if rows:
            raise sqlite3.OperationalError("disk full")
        original(run, result, rows, iv)

    monkeypatch.setattr(store, "save_symbol", save)
    try:
        notifier = Mock()
        Runner(cfg, store, notifier).collect("manual-test", ["SPY"])
        row = store.db.execute("SELECT status,contracts FROM symbol_runs").fetchone()
        assert tuple(row) == ("failed", 0)
        assert any("存储写入失败" in c.args[0] for c in notifier.alert.call_args_list)
        report_result = notifier.report.call_args.args[2][0]
        assert report_result.contracts == 0 and report_result.status == "failed"
    finally:
        store.close()


def test_backup_cli_includes_raw_and_consistent_sqlite(cfg, tmp_path):
    root = Path(cfg["data_dir"])
    store = Storage(root)
    run = store.start("2026-11-16", "10:00")
    store.finish(run, "ok")
    Archive(root).write("2026-11-16", "SPY", "10:00", "chain", {"data": [1]})
    config = json.loads((Path(__file__).parents[1] / "config.example.json").read_text())
    config["data_dir"] = str(root)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    destination = tmp_path / "backups"
    try:
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
                str(destination),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        backup = Path(result.stdout.strip())
        assert len(list((backup / "raw").rglob("*.gz"))) == 1
        with sqlite3.connect(backup / "vault.sqlite3") as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT status FROM runs").fetchone()[0] == "ok"
    finally:
        store.close()


def test_count_drop_missing_fields_and_stale_iv_are_alerted(cfg, tmp_path, chain, monkeypatch):
    cfg["symbols"] = ["SPY"]
    primary, yahoo = Mock(), Mock()
    primary.chain.return_value = chain
    yahoo.auxiliary.return_value = {"history": [], "events": {}}
    monkeypatch.setattr("option_vault.runner.CboeSource", lambda *a: primary)
    monkeypatch.setattr("option_vault.runner.YahooSource", lambda *a: yahoo)
    store = Storage(tmp_path)
    try:
        notifier = Mock()
        runner = Runner(cfg, store, notifier)
        original = dict(chain.options[0])
        chain.options = [
            dict(original, strike=105 + i, occ_symbol=f"SPY261218C{(105 + i) * 1000:08d}")
            for i in range(4)
        ]
        runner.collect("manual-previous", ["SPY"])
        chain.options = [
            dict(
                original, bid=None, iv=None, oi=None, delta=None, gamma=None, theta=None, vega=None
            )
        ]
        chain.timestamp -= timedelta(hours=2)
        runner.collect("16:15-close", ["SPY"], closing=True)
        messages = " ".join(c.args[0] for c in notifier.alert.call_args_list)
        assert "骤降" in messages and "关键字段缺失 100%" in messages and "时间戳过旧" in messages
        assert store.db.execute("SELECT COUNT(*) FROM daily_iv").fetchone()[0] == 0
        assert store.db.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 5
    finally:
        store.close()


def test_crash_is_persisted_and_alerted(cfg, tmp_path, monkeypatch):
    primary, yahoo = Mock(), Mock()
    primary.chain.side_effect = KeyboardInterrupt("shutdown")
    monkeypatch.setattr("option_vault.runner.CboeSource", lambda *a: primary)
    monkeypatch.setattr("option_vault.runner.YahooSource", lambda *a: yahoo)
    store = Storage(tmp_path)
    try:
        notifier = Mock()
        with pytest.raises(KeyboardInterrupt):
            Runner(cfg, store, notifier).collect("manual-test", ["SPY"])
        assert store.db.execute("SELECT status FROM runs").fetchone()[0] == "crashed"
        assert any("崩溃/中断" in c.args[0] for c in notifier.alert.call_args_list)
    finally:
        store.close()


def test_database_failure_does_not_disable_telegram(cfg, tmp_path, monkeypatch):
    cfg["telegram"]["enabled"] = True
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-for-test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
    store = Mock()
    store.state.side_effect = sqlite3.DatabaseError("corrupt")
    store.set_state.side_effect = sqlite3.DatabaseError("corrupt")
    post = Mock(return_value=Mock(status_code=200, json=lambda: {"ok": True}))
    monkeypatch.setattr("option_vault.notifications.requests.post", post)
    Notifier(cfg, store).alert("SQLite failed")
    post.assert_called_once()


def test_disk_failure_does_not_bypass_rate_limit_backoff(cfg, monkeypatch):
    monkeypatch.setattr("option_vault.sources.http.time.monotonic", lambda: 10)
    archive = Mock()
    archive.write.side_effect = OSError("disk full")
    alert = Mock()
    gate = RequestGate(cfg["http"], archive, alert, "2026-11-16", "10:00")
    response = Mock(status_code=429, text="{}", headers={"Retry-After": "120"})
    with pytest.raises(OSError):
        gate.response("https://example.test/data", response)
    assert gate.next_request == 130
    assert any("429" in c.args[0] for c in alert.call_args_list)


def test_watch_schedule_holiday_early_close_and_dst(cfg):
    assert [s.label for s in hourly_slots(date(2026, 11, 16), cfg["schedule"])] == [
        "watch-10:00",
        "watch-11:00",
        "watch-12:00",
        "watch-13:00",
        "watch-14:00",
        "watch-15:00",
    ]
    assert [s.label for s in hourly_slots(date(2026, 11, 27), cfg["schedule"])] == [
        "watch-10:00",
        "watch-11:00",
        "watch-12:00",
    ]
    assert hourly_slots(date(2026, 11, 26), cfg["schedule"]) == []
    assert hourly_slots(date(2026, 3, 6), cfg["schedule"])[0].at.astimezone(timezone.utc).hour == 15
    assert hourly_slots(date(2026, 3, 9), cfg["schedule"])[0].at.astimezone(timezone.utc).hour == 14


def test_watch_beyond_one_year_exact_match_persists_and_reports(cfg, tmp_path, chain, monkeypatch):
    cfg["hourly_watch"]["enabled"] = True
    chain.symbol = "SBET"
    chain.options = [
        dict(chain.options[0], occ_symbol="SBET280121C00009000", expiry="2028-01-21", strike=9),
        dict(
            chain.options[0],
            occ_symbol="SBET280121C00008000",
            expiry="2028-01-21",
            strike=8,
            bid=1.15,
            ask=1.36,
            last=1.22,
        ),
    ]
    primary, yahoo = Mock(), Mock()
    primary.chain.return_value = chain
    monkeypatch.setattr("option_vault.runner.CboeSource", lambda *a: primary)
    monkeypatch.setattr("option_vault.runner.YahooSource", lambda *a: yahoo)
    store = Storage(tmp_path)
    try:
        notify = Mock()
        run = Runner(cfg, store, notify).watch_quote("watch-10:00", "2026-11-16")
        quote = store.db.execute("SELECT * FROM watch_quotes WHERE run_id=?", (run,)).fetchone()
        assert quote["occ_symbol"] == "SBET280121C00008000"
        assert (quote["bid"], quote["ask"], quote["last"]) == (1.15, 1.36, 1.22)
        report = notify.send.call_args.args[0]
        assert "$1.15" in report and "$1.36" in report and "$1.22" in report
        assert "Last" in report and "可能陈旧" in report
        yahoo.chain.assert_not_called()
        assert store.previous_counts("SBET", "2026-11-16", "10:00") == []
    finally:
        store.close()


def test_watch_missing_contract_falls_back_to_exact_expiry(cfg, tmp_path, chain, monkeypatch):
    cfg["hourly_watch"]["enabled"] = True
    chain.symbol = "SBET"
    chain.options = [dict(chain.options[0], occ_symbol="SBET280121C00008000")]
    primary, yahoo = Mock(), Mock()
    other = Mock(options=[dict(chain.options[0], occ_symbol="SBET280121C00009000")])
    primary.chain.return_value = other
    yahoo.chain.return_value = chain
    chain.source, chain.timestamp = "yfinance", None
    monkeypatch.setattr("option_vault.runner.CboeSource", lambda *a: primary)
    monkeypatch.setattr("option_vault.runner.YahooSource", lambda *a: yahoo)
    store = Storage(tmp_path)
    try:
        notify = Mock()
        runner = Runner(cfg, store, notify)
        runner.watch_quote("watch-10:00", "2026-11-16")
        yahoo.chain.assert_called_once_with("SBET", expiry="2028-01-21")
        assert "报价时间未知" in notify.send.call_args.args[0]
        assert any("切换 yfinance" in c.args[0] for c in notify.alert.call_args_list)
        assert store.db.execute("SELECT source FROM watch_quotes").fetchone()[0] == "yfinance"
    finally:
        store.close()


def test_watch_failure_records_and_alerts_without_guessing(cfg, tmp_path, monkeypatch):
    cfg["hourly_watch"]["enabled"] = True
    primary, yahoo = Mock(), Mock()
    primary.chain.side_effect = ValueError("unavailable")
    yahoo.chain.side_effect = ValueError("not found")
    monkeypatch.setattr("option_vault.runner.CboeSource", lambda *a: primary)
    monkeypatch.setattr("option_vault.runner.YahooSource", lambda *a: yahoo)
    store = Storage(tmp_path)
    try:
        notify = Mock()
        run = Runner(cfg, store, notify).watch_quote("watch-10:00", "2026-11-16")
        assert (
            store.db.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()[0] == "failed"
        )
        assert store.db.execute("SELECT COUNT(*) FROM watch_quotes").fetchone()[0] == 0
        assert any("报告失败" in c.args[0] for c in notify.alert.call_args_list)
    finally:
        store.close()


def test_watch_tick_is_deduplicated_and_never_backfills(cfg, tmp_path, monkeypatch):
    cfg["hourly_watch"]["enabled"] = True
    store = Storage(tmp_path)
    try:
        runner = Runner(cfg, store, Mock())

        def watch(label, day):
            run = store.start(day, label)
            store.finish(run, "ok")

        watch_mock = Mock(side_effect=watch)
        monkeypatch.setattr(runner, "watch_quote", watch_mock)
        monkeypatch.setattr(runner, "collect", Mock())
        now = datetime(2026, 11, 16, 15, 1, tzinfo=timezone.utc)
        runner.tick(now)
        runner.tick(now + timedelta(minutes=1))
        watch_mock.assert_called_once_with("watch-10:00", "2026-11-16")
        runner.tick(now + timedelta(hours=2, minutes=20))
        assert watch_mock.call_count == 1
        assert any("watch-11:00" in c.args[0] for c in runner.notify.alert.call_args_list)
    finally:
        store.close()


def test_watch_disabled_after_expiration(cfg, tmp_path, monkeypatch):
    cfg["hourly_watch"]["enabled"] = True
    store = Storage(tmp_path)
    try:
        runner = Runner(cfg, store, Mock())
        monkeypatch.setattr(runner, "watch_quote", Mock())
        monkeypatch.setattr(runner, "collect", Mock())
        runner.tick(datetime(2028, 1, 24, 15, 1, tzinfo=timezone.utc))
        runner.watch_quote.assert_not_called()
    finally:
        store.close()


def test_yahoo_watch_fetches_only_requested_expiry_even_beyond_year(cfg, tmp_path, monkeypatch):
    gate = RequestGate(cfg["http"], Archive(tmp_path), Mock(), "2026-11-16", "10:00")
    yahoo = YahooSource(gate, cfg["sources"])
    row = {
        "contractSymbol": "SBET280121C00008000",
        "bid": 1.15,
        "ask": 1.36,
        "lastPrice": 1.22,
    }
    frame = Mock()
    frame.to_dict.return_value = [row]
    empty = Mock()
    empty.to_dict.return_value = []
    option_chain = Mock(
        underlying={"symbol": "SBET", "regularMarketPrice": 10}, calls=frame, puts=empty
    )
    ticker = Mock(options=["2026-12-18", "2028-01-21"])
    ticker.option_chain.return_value = option_chain
    monkeypatch.setattr(yahoo, "ticker", Mock(return_value=ticker))
    try:
        chain = yahoo.chain("SBET", expiry="2028-01-21")
        ticker.option_chain.assert_called_once_with("2028-01-21")
        assert chain.options[0]["occ_symbol"] == row["contractSymbol"]
        assert chain.options[0]["last"] == 1.22
        assert chain.timestamp is None and chain.source == "yfinance"
    finally:
        yahoo.close()
