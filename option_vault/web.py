from __future__ import annotations

import argparse
import csv
import io
import ipaddress
import json
import logging
import math
import re
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import plotly
from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from waitress import serve
from werkzeug.exceptions import HTTPException

from .config import load_config
from .parsing import OCC
from .research import Research
from .scoring import weighted_score


def create_app(cfg: dict) -> Flask:
    app = Flask(__name__)
    research = Research(cfg)
    app.config["RESEARCH"] = research

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(400)
    @app.errorhandler(404)
    def invalid(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(Exception)
    def failed(error):
        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        app.logger.exception("Dashboard request failed")
        return jsonify(error="服务器读取失败，请检查 web 日志；没有返回伪造数据。"), 500

    def symbol_arg() -> str:
        symbol = request.args.get("symbol", "")
        if symbol not in cfg["symbols"]:
            abort(400, description="Unknown configured symbol")
        return symbol

    def code_arg() -> str:
        code = request.args.get("code", "")
        if not OCC.fullmatch(code) or len(code) > 40:
            abort(400, description="Invalid OCC contract")
        return code

    def number_arg(name: str, default: float) -> float:
        try:
            value = float(request.args.get(name, default))
        except ValueError:
            abort(400, description=f"Invalid number: {name}")
        if not math.isfinite(value):
            abort(400, description=f"Invalid number: {name}")
        return value

    def weights_arg() -> dict:
        weights = {
            key: number_arg(key, value) for key, value in research.scoring["weights"].items()
        }
        if any(w < 0 or w > 100 for w in weights.values()) or sum(weights.values()) <= 0:
            abort(400, description="Weights must be 0..100 with a positive sum")
        return weights

    def snapshot_arg() -> str | None:
        value = request.args.get("snapshot") or None
        if value is not None and not re.fullmatch(r"[a-f0-9]{32}", value):
            abort(400, description="Invalid snapshot")
        return value

    def filtered() -> list[dict]:
        strategy = request.args.get("strategy", "P")
        if strategy not in {"P", "C"}:
            abort(400, description="strategy must be P or C")
        symbol = request.args.get("symbol")
        if symbol and symbol not in cfg["symbols"]:
            abort(400, description="Unknown configured symbol")
        minimum, maximum = number_arg("min_dte", 1), number_arg("max_dte", 366)
        strike = number_arg("strike", 0)
        if not 0 <= minimum <= maximum <= 3660:
            abort(400, description="Invalid DTE range")
        weights = weights_arg()
        rows = []
        for row in research.ranked(snapshot_arg()):
            if row["side"] != strategy or symbol and row["symbol"] != symbol:
                continue
            if (
                not minimum <= row["dte"] <= maximum
                or strike
                and abs(row["strike"] - strike) > 1e-8
            ):
                continue
            if request.args.get("include_low") != "1" and row["low_confidence"]:
                continue
            rows.append({**row, "score": weighted_score(row["parts"], weights)})
        return sorted(rows, key=lambda r: (r["score"] is not None, r["score"] or 0), reverse=True)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/assets/plotly.js")
    def charts():
        return send_file(
            Path(plotly.__file__).parent / "package_data" / "plotly.min.js",
            mimetype="application/javascript",
        )

    @app.get("/api/overview")
    def overview():
        return jsonify(research.overview())

    @app.get("/api/rankings")
    def rankings():
        rows = filtered()
        page = number_arg("page", 1)
        if not page.is_integer() or page < 1:
            abort(400, description="page must be a positive integer")
        start = (int(page) - 1) * 50
        return jsonify(
            total=len(rows),
            page=int(page),
            rows=rows[start : start + 50],
            generated_utc=datetime.now(timezone.utc).isoformat(),
        )

    @app.get("/api/contract")
    def contract():
        return jsonify(research.contract(symbol_arg(), code_arg(), snapshot_arg()))

    @app.get("/api/snapshots")
    def snapshots():
        day = request.args.get("day")
        if day:
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                abort(400, description="Invalid snapshot date")
        return jsonify(research.snapshots(day))

    @app.get("/api/expiries")
    def expiries():
        symbol = symbol_arg()
        strike = number_arg("strike", 0)
        strategy = request.args.get("strategy", "P")
        rows = [
            r
            for r in research.ranked()
            if r["symbol"] == symbol and abs(r["strike"] - strike) < 1e-8 and r["side"] == strategy
        ]
        available = sorted(
            {
                r["expiry"]
                for r in research.ranked()
                if r["symbol"] == symbol and r["side"] == strategy
            }
        )
        return jsonify(rows=sorted(rows, key=lambda r: r["expiry"]), available_expiries=available)

    @app.get("/api/strikes")
    def strikes():
        symbol = symbol_arg()
        strategy = request.args.get("strategy", "P")
        values = sorted(
            {
                r["strike"]
                for r in research.ranked()
                if r["symbol"] == symbol and r["side"] == strategy
            }
        )
        return jsonify(strikes=values)

    @app.get("/api/export")
    def export():
        kind = request.args.get("kind", "rankings")
        if kind == "rankings":
            rows = filtered()
            fields = [
                "symbol",
                "occ_symbol",
                "expiry",
                "side",
                "strike",
                "spot",
                "dte",
                "quote_dte",
                "current_dte",
                "bid",
                "ask",
                "mid",
                "last",
                "iv",
                "delta",
                "oi",
                "volume",
                "efficiency",
                "score",
                "low_confidence",
                "reasons",
                "parts",
                "statistics",
                "source",
                "collected_utc",
                "collected_et",
                "source_timestamp",
                "flags",
                "score_weights",
                "units",
            ]
        elif kind in {"contract", "watch"}:
            detail = research.contract(symbol_arg(), code_arg(), snapshot_arg())
            rows = detail["history" if kind == "contract" else "watch"]
            fields = list(rows[0]) if rows else ["collected_utc", "bid", "ask", "last", "source"]
        elif kind == "iv":
            rows = [point for series in research.overview()["iv"] for point in series["history"]]
            fields = ["day", "symbol", "iv", "source", "source_timestamp"]
        else:
            abort(400, description="Unknown export kind")
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if kind == "rankings":
                row = {
                    **row,
                    "score_weights": weights_arg(),
                    "units": "USD/share; IV, efficiency as fractions; timestamps ISO8601",
                }
            cleaned = {}
            for key, value in row.items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                if isinstance(value, str) and re.match(r"^[\s]*[=+@\-\t\r]", value):
                    value = "'" + value
                cleaned[key] = value
            writer.writerow(cleaned)
        return Response(
            "\ufeff" + buffer.getvalue(),
            content_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="option-vault-{kind}.csv"'},
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only option research dashboard")
    parser.add_argument("--config", default="config.local.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tailscale-ip", help="Also listen on this Tailnet IPv4, never 0.0.0.0")
    args = parser.parse_args()
    cfg = load_config(args.config)
    logs = Path(cfg["data_dir"]) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(),
            TimedRotatingFileHandler(
                logs / "web.log", when="midnight", utc=True, backupCount=0, encoding="utf-8"
            ),
        ],
        format="%(asctime)s %(levelname)s %(message)s",
    )
    listeners = f"127.0.0.1:{args.port}"
    if args.tailscale_ip:
        address = ipaddress.ip_address(args.tailscale_ip)
        if address not in ipaddress.ip_network("100.64.0.0/10"):
            parser.error("--tailscale-ip must be an IPv4 address in 100.64.0.0/10")
        listeners += f" {address}:{args.port}"
    serve(create_app(cfg), listen=listeners, threads=4)


if __name__ == "__main__":
    main()
