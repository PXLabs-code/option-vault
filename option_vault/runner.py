from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any

from .calculations import NY, decorate, standardized_iv
from .models import Chain, Source, SymbolResult
from .notifications import Notifier
from .parsing import contract
from .schedule import Slot, day_slots, hourly_slots
from .sources.cboe import CboeSource
from .sources.http import CboeHTTP, RequestGate, retry
from .sources.yahoo import YahooSource
from .storage import Archive, Storage

LOG = logging.getLogger(__name__)


class Runner:
    def __init__(self, cfg: dict[str, Any], storage: Storage, notifier: Notifier):
        self.cfg, self.storage, self.notify = cfg, storage, notifier

    def recover(self) -> None:
        interrupted = self.storage.db.execute(
            "SELECT id,day,label FROM runs WHERE status='running'"
        ).fetchall()
        for row in interrupted:
            self.storage.finish(
                row["id"], "interrupted", "Previous process exited before completion"
            )
            self.notify.alert(
                f"{row['day']} {row['label']} 上次运行中断：进程退出、断电或重启；不回填历史"
            )

    def check_missed(self, now: datetime, slots: list[Slot], key_prefix: str = "missed") -> None:
        day = now.astimezone(NY).date().isoformat()
        grace = timedelta(minutes=self.cfg["schedule"]["grace_minutes"])
        missed = [
            s.label
            for s in slots
            if s.at + grace < now and self.storage.slot_status(day, s.label) is None
        ]
        signature = ",".join(missed)
        key = f"{key_prefix}:{day}"
        if missed and self.storage.state(key) != signature:
            self.notify.alert(
                f"{day} 漏跑时点：{signature} ET；数据库无运行记录，"
                "可能程序未运行、机器休眠或上一轮耗时过长。不用当前行情伪造历史。"
            )
            self.storage.set_state(key, signature)

    def tick(self, now: datetime) -> None:
        tick_started = time.monotonic()
        self.recover()
        day = now.astimezone(NY).date()
        slots = day_slots(day, self.cfg["schedule"])
        self.check_missed(now, slots)
        grace = timedelta(minutes=self.cfg["schedule"]["grace_minutes"])
        watch = self.cfg.get("hourly_watch", {})
        watched = False
        if watch.get("enabled") and day <= date.fromisoformat(
            contract(watch["occ_symbol"])["expiry"]
        ):
            watch_slots = hourly_slots(day, self.cfg["schedule"])
            self.check_missed(now, watch_slots, "watch_missed")
            for slot in watch_slots:
                if (
                    slot.at <= now <= slot.at + grace
                    and self.storage.slot_status(day.isoformat(), slot.label) is None
                ):
                    self.watch_quote(slot.label, day.isoformat())
                    watched = True
        if watched and any(slot.at <= now <= slot.at + grace for slot in slots):
            time.sleep(self.cfg["http"]["interval_seconds"])
        now += timedelta(seconds=time.monotonic() - tick_started)
        for slot in slots:
            market_close = slots[-1].at - timedelta(
                minutes=self.cfg["schedule"]["close_delay_minutes"]
            )
            if (
                (slot.closing or now < market_close)
                and slot.at <= now <= slot.at + grace
                and self.storage.slot_status(day.isoformat(), slot.label) is None
            ):
                self.collect(slot.label, self.cfg["symbols"], closing=slot.closing)
        if slots and now > slots[-1].at + grace:
            key = f"summary:{day}"
            if not self.storage.state(key):
                self.daily_summary(day.isoformat(), slots)

    def obtain(self, source: Source, symbol: str) -> Chain:
        return retry(
            lambda: source.chain(symbol),
            self.cfg["http"],
            lambda message: LOG.warning("%s %s", symbol, message),
        )

    def watch_quote(self, label: str, day: str) -> str:
        watch = self.cfg["hourly_watch"]
        symbol, code = watch["symbol"], watch["occ_symbol"]
        details = contract(code)
        expiry = details["expiry"]
        run = self.storage.start(day, label)
        gate = RequestGate(
            self.cfg["http"], Archive(Path(self.cfg["data_dir"])), self.notify.alert, day, label
        )
        http = CboeHTTP(gate)
        primary = CboeSource(http, self.cfg["sources"])
        yahoo = YahooSource(gate, self.cfg["sources"])

        def find(chain: Chain) -> tuple[Chain, dict[str, Any]]:
            matches = [row for row in chain.options if row["occ_symbol"].replace(" ", "") == code]
            if len(matches) != 1:
                raise ValueError(f"{symbol} 未找到唯一合约 {code}（匹配 {len(matches)}）")
            return chain, matches[0]

        def on_retry(message: str) -> None:
            LOG.warning("%s 指定合约 %s", symbol, message)

        try:
            try:
                chain, quote = retry(
                    lambda: find(primary.chain(symbol)), self.cfg["http"], on_retry
                )
            except Exception as exc:
                LOG.exception("%s targeted Cboe fetch failed", code)
                self.notify.alert(f"{code} Cboe 指定合约获取失败：{type(exc).__name__}: {exc}")
                if not self.cfg["sources"]["yfinance_fallback"]:
                    raise
                self.notify.alert(f"{code} 指定合约切换 yfinance，报价时间未知")
                chain, quote = retry(
                    lambda: find(yahoo.chain(symbol, expiry=expiry)), self.cfg["http"], on_retry
                )
            self.storage.save_watch_quote(run, symbol, chain, quote)
            lag = (chain.fetched_at - chain.timestamp).total_seconds() if chain.timestamp else None
            q = self.cfg["quality"]
            warnings = []
            if lag is None:
                warnings.append("报价时间未知")
            elif lag > q["stale_seconds"]:
                warnings.append("数据源时间戳过旧")
            elif lag < -q["future_timestamp_tolerance_seconds"]:
                warnings.append("数据源时间戳超前")
            if quote["bid"] is None or quote["ask"] is None:
                warnings.append("买价或卖价缺失")
            elif quote["bid"] == 0:
                warnings.append("买价为0")
            if quote["last"] is None:
                warnings.append("成交价缺失")

            def price(value: float | None) -> str:
                return "缺失" if value is None else f"${value}"

            source_time = chain.timestamp.isoformat() if chain.timestamp else "未知"
            lag_text = f"{lag / 60:.1f}分钟" if lag is not None else "未知"
            self.notify.send(
                f"{'⚠️' if warnings else '✅'} 指定期权每小时报告 | {day} {label} ET\n"
                f"{code} | {expiry} | {'Call' if details['side'] == 'C' else 'Put'} | "
                f"行权价 ${details['strike']:.2f}\n"
                f"买价 Bid {price(quote['bid'])} | 卖价 Ask {price(quote['ask'])} | "
                f"最近成交 Last {price(quote['last'])}\n"
                f"来源 {chain.source} | 采集 {chain.fetched_at.astimezone(NY).isoformat()} | "
                f"源时间 {source_time} | 延迟 {lag_text}\n"
                "延迟行情，Last 为最近成交价，可能陈旧。"
                + (f"\n⚠️ {'；'.join(warnings)}" if warnings else "")
            )
            self.storage.finish(run, "ok")
        except Exception as exc:
            LOG.exception("%s watch collection failed", code)
            self.notify.alert(f"{code} {day} {label} 指定合约报告失败：{type(exc).__name__}: {exc}")
            self.storage.finish(run, "failed", str(exc))
        except BaseException as exc:
            self.notify.alert(f"{code} 指定合约采集中断：{type(exc).__name__}: {exc}")
            self.storage.finish(run, "crashed", str(exc))
            raise
        finally:
            http.close()
            yahoo.close()
        return run

    def collect(self, label: str, symbols: list[str], closing: bool = False) -> str:
        started = time.monotonic()
        day = datetime.now(timezone.utc).astimezone(NY).date().isoformat()
        run = self.storage.start(day, label)
        gate = RequestGate(
            self.cfg["http"], Archive(Path(self.cfg["data_dir"])), self.notify.alert, day, label
        )
        http = CboeHTTP(gate)
        primary = CboeSource(http, self.cfg["sources"])
        yahoo = YahooSource(gate, self.cfg["sources"])
        results = []
        try:
            for symbol in symbols:
                symbol_start = time.monotonic()
                result = SymbolResult(symbol, "failed")
                try:
                    try:
                        chain = self.obtain(primary, symbol)
                    except Exception as exc:
                        LOG.exception("%s Cboe exhausted retries", symbol)
                        self.notify.alert(
                            f"{symbol} Cboe 采集/解析重试后失败：{type(exc).__name__}: {exc}"
                        )
                        if not self.cfg["sources"]["yfinance_fallback"]:
                            raise
                        self.notify.alert(
                            f"{symbol} 已切换备用数据源 yfinance；无 Greeks，报价时间未知"
                        )
                        chain = self.obtain(yahoo, symbol)
                        result.warnings.append("备用源，无 Greeks/报价时间")
                    result.source = chain.source
                    result.lag_seconds = (
                        (chain.fetched_at - chain.timestamp).total_seconds()
                        if chain.timestamp
                        else None
                    )
                    rows = decorate(chain, self.cfg)
                    result.contracts = len(rows)
                    if not rows:
                        raise ValueError("No contracts within configured scope")
                    result.flagged_ratio = sum(bool(r["flags"]) for r in rows) / len(rows)
                    result.zero_bid_ratio = sum("zero_bid" in r["flags"] for r in rows) / len(rows)
                    prior = self.storage.previous_counts(symbol, day, label)
                    q = self.cfg["quality"]
                    if any(len(rows) < n * q["count_drop_ratio"] for n in prior):
                        result.warnings.append("合约数较上一轮/上交易日同时点骤降")
                    required_fields = ("bid", "ask", "iv", "oi")
                    if chain.source == "cboe":
                        required_fields += ("delta", "gamma", "theta", "vega")
                    missing = sum(any(r[k] is None for k in required_fields) for r in rows) / len(
                        rows
                    )
                    if missing >= q["missing_fields_ratio"]:
                        result.warnings.append(f"关键字段缺失 {missing:.0%}")
                    if result.zero_bid_ratio >= q["zero_bid_alert_ratio"]:
                        result.warnings.append(f"Bid 为0占比异常 {result.zero_bid_ratio:.0%}")
                    if any("stale_timestamp" in r["flags"] for r in rows):
                        result.warnings.append(f"数据源时间戳过旧 {result.lag_seconds:.0f}s")
                    if any("future_timestamp" in r["flags"] for r in rows):
                        result.warnings.append("数据源时间戳超前，请检查时区配置")
                    if chain.timestamp is None:
                        result.warnings.append("数据源时间戳未知")
                    iv = None
                    if closing:
                        stale = result.lag_seconds is not None and (
                            result.lag_seconds > q["stale_seconds"]
                            or result.lag_seconds < -q["future_timestamp_tolerance_seconds"]
                        )
                        iv = standardized_iv(chain, self.cfg) if not stale else None
                        if iv is None:
                            result.warnings.append(
                                "标准化 IV 未记录：无有效30天两侧ATM合约或时间戳异常"
                            )
                    aux_key = f"aux:{day}:{symbol}"
                    if closing or not self.storage.state(aux_key):
                        try:
                            auxiliary = retry(
                                partial(yahoo.auxiliary, symbol),
                                self.cfg["http"],
                                partial(LOG.warning, "%s auxiliary %s", symbol),
                            )
                            self.storage.save_auxiliary(symbol, auxiliary)
                            if auxiliary.get("errors"):
                                message = "; ".join(auxiliary["errors"])
                                result.warnings.append(f"辅助事件不可用：{message}")
                                self.notify.alert(
                                    f"{symbol} 辅助事件环节异常：{message}；日线已保存"
                                )
                            else:
                                self.storage.set_state(aux_key, "ok")
                        except Exception as exc:
                            LOG.exception("%s auxiliary data failed", symbol)
                            result.warnings.append(f"辅助股价/事件失败：{type(exc).__name__}")
                            self.notify.alert(f"{symbol} yfinance 辅助股价/事件环节失败：{exc}")
                    result.status = "warning" if result.warnings or result.flagged_ratio else "ok"
                    result.elapsed = time.monotonic() - symbol_start
                    try:
                        self.storage.save_symbol(run, result, rows, iv)
                    except Exception as exc:
                        self.notify.alert(f"{symbol} 存储写入失败：{type(exc).__name__}: {exc}")
                        raise
                    if result.warnings:
                        self.notify.alert(f"{symbol} 数据检查：{'; '.join(result.warnings)}")
                except Exception as exc:
                    LOG.exception("%s collection failed", symbol)
                    result.status, result.error, result.contracts = "failed", str(exc), 0
                    result.elapsed = time.monotonic() - symbol_start
                    self.notify.alert(
                        f"{symbol} 采集/写入环节最终失败：{type(exc).__name__}: {exc}"
                    )
                    try:
                        self.storage.save_symbol(run, result, [])
                    except Exception:
                        LOG.exception("%s failed result cannot be persisted", symbol)
                        self.notify.alert(f"{symbol} 失败运行记录也无法写入 SQLite，请检查磁盘")
                results.append(result)
            status = "ok" if all(r.status != "failed" for r in results) else "partial"
            if all(r.status == "failed" for r in results):
                status = "failed"
                self.notify.alert(f"{day} {label} 整轮采集失败")
            self.storage.finish(run, status)
            self.notify.report(f"{day} {label}", time.monotonic() - started, results)
            if closing:
                self.daily_summary(
                    day, day_slots(datetime.fromisoformat(day).date(), self.cfg["schedule"])
                )
            return run
        except BaseException as exc:
            LOG.exception("Run crashed")
            self.notify.alert(f"{day} {label} 程序崩溃/中断：{type(exc).__name__}: {exc}")
            self.storage.finish(run, "crashed", str(exc))
            raise
        finally:
            http.close()
            yahoo.close()

    def daily_summary(self, day: str, slots: list[Slot]) -> None:
        lines = [f"每日数据汇总 | {day} ET"]
        for slot in slots:
            status = self.storage.slot_status(day, slot.label)
            icon = "✅" if status == "ok" else "❌"
            lines.append(f"{icon} {slot.label}: {status or '缺失（程序未运行/未及时执行）'}")
        recorded = {
            r[0] for r in self.storage.db.execute("SELECT symbol FROM daily_iv WHERE day=?", (day,))
        }
        missing = [s for s in self.cfg["symbols"] if s not in recorded]
        lines.append("标准化 IV：" + (f"缺失 {', '.join(missing)}" if missing else "全部已记录"))
        self.notify.send("\n".join(lines))
        self.storage.set_state(f"summary:{day}", "sent")
