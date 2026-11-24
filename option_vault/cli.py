from __future__ import annotations

import argparse
import fcntl
import logging
import shutil
import signal
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .calculations import NY, iv_metrics
from .config import load_config
from .notifications import Notifier
from .runner import Runner
from .schedule import day_slots
from .storage import Storage

LOG = logging.getLogger(__name__)


def terminate(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


def main() -> None:
    parser = argparse.ArgumentParser(description="US delayed option snapshot collector")
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--no-telegram", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("once", help="One manual snapshot; never claims a scheduled slot")
    once.add_argument("--symbol", action="append", help="Repeat to select symbols")
    commands.add_parser("watch-once", help="Test the configured watched contract")
    commands.add_parser("tick", help="launchd entry; collect only if a scheduled slot is due")
    schedule = commands.add_parser("schedule")
    schedule.add_argument("--date", default=date.today().isoformat())
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", required=True)
    metrics = commands.add_parser("iv-metrics")
    metrics.add_argument("--symbol", required=True)
    args = parser.parse_args()
    cfg = load_config(args.config)
    root = Path(cfg["data_dir"])
    root.mkdir(parents=True, exist_ok=True)
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(
        logs / "collector.log", when="midnight", backupCount=0, encoding="utf-8", utc=True
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler, logging.StreamHandler()],
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    with (root / ".collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.warning("Another collector/backup holds the lock; skipping this tick")
            return
        storage = None
        notifier = None
        exit_code = 0
        try:
            storage = Storage(root)
            if args.command == "schedule":
                for slot in day_slots(date.fromisoformat(args.date), cfg["schedule"]):
                    print(
                        slot.label,
                        slot.at.isoformat(),
                        slot.at.astimezone(timezone.utc).isoformat(),
                    )
                return
            if args.command == "backup":
                destination = Path(args.destination).expanduser().resolve()
                if destination == root or root in destination.parents:
                    raise ValueError("Backup destination must be outside data_dir")
                target = destination / (
                    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    + "-"
                    + uuid.uuid4().hex[:8]
                )
                target.mkdir(parents=True)
                for child in root.iterdir():
                    if child.name in {
                        ".collector.lock",
                        "vault.sqlite3",
                        "vault.sqlite3-wal",
                        "vault.sqlite3-shm",
                        "research.sqlite3-wal",
                        "research.sqlite3-shm",
                        "research.sqlite3-journal",
                    }:
                        continue
                    if child.is_dir():
                        shutil.copytree(child, target / child.name)
                    elif child.name == "research.sqlite3":
                        with (
                            closing(
                                sqlite3.connect(child.resolve().as_uri() + "?mode=ro", uri=True)
                            ) as source,
                            closing(sqlite3.connect(target / child.name)) as destination_db,
                        ):
                            source.backup(destination_db)
                    else:
                        shutil.copy2(child, target / child.name)
                storage.backup(target / "vault.sqlite3")
                print(target)
                return
            if args.command == "iv-metrics":
                latest = storage.db.execute(
                    "SELECT day,iv FROM daily_iv WHERE symbol=? ORDER BY day DESC LIMIT 1",
                    (args.symbol,),
                ).fetchone()
                print(
                    iv_metrics(
                        latest["iv"],
                        storage.iv_history(args.symbol, latest["day"]),
                        cfg["iv"]["history_observations"],
                    )
                    if latest
                    else {"rank": None, "percentile": None}
                )
                return
            notifier = Notifier(cfg, storage, args.no_telegram)
            signal.signal(signal.SIGTERM, terminate)
            runner = Runner(cfg, storage, notifier)
            if args.command == "tick":
                runner.tick(datetime.now(timezone.utc))
            elif args.command == "watch-once":
                if not cfg.get("hourly_watch", {}).get("enabled"):
                    raise ValueError("hourly_watch is not enabled")
                now = datetime.now(timezone.utc)
                run = runner.watch_quote(
                    "watch-manual-" + uuid.uuid4().hex[:6], now.astimezone(NY).date().isoformat()
                )
                status = storage.db.execute(
                    "SELECT status FROM runs WHERE id=?", (run,)
                ).fetchone()[0]
                if status != "ok":
                    exit_code = 1
            else:
                symbols = args.symbol or cfg["symbols"]
                if len(set(symbols)) != len(symbols):
                    raise ValueError("--symbol values must be unique")
                if any(symbol not in cfg["symbols"] for symbol in symbols):
                    raise ValueError("--symbol must exactly match a configured symbol")
                now = datetime.now(timezone.utc)
                runner.recover()
                runner.check_missed(now, day_slots(now.astimezone(NY).date(), cfg["schedule"]))
                run = runner.collect(
                    "manual-" + now.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:6], symbols
                )
                status = storage.db.execute(
                    "SELECT status FROM runs WHERE id=?", (run,)
                ).fetchone()[0]
                if status != "ok":
                    exit_code = 1
        except BaseException as exc:
            LOG.exception("Fatal collector error")
            if notifier is None and args.command in {"tick", "once", "watch-once"}:
                try:
                    notifier = Notifier(cfg, storage, args.no_telegram)
                except (ValueError, OSError):
                    LOG.exception("Cannot initialize emergency Telegram notifier")
            if notifier is not None:
                notifier.alert(f"程序入口崩溃/退出：{type(exc).__name__}: {exc}")
            raise
        finally:
            if storage is not None:
                storage.close()
        if exit_code:
            raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
