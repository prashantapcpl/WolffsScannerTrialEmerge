"""
webhook_server.py — Incoming webhook server for Strategy 3.

Listens on port 5001 for signals from Chartink (or any external source).
Converts plain stock names to Fyers format and forwards them to
Strategy3WebhookMirror.

Chartink sends POST requests with:
  stocks         = "RELIANCE,TCS,INFY"     (comma-separated plain names)
  trigger_prices = "2500,3500,1500"        (comma-separated, may be absent)
  scan_name      = "My Scan"               (informational)

Also accepts JSON: {"stocks": "RELIANCE,TCS", "scan_name": "..."}
"""

import os
import sys
import json
import socket
import threading
from datetime import datetime
import pytz

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

IST = pytz.timezone("Asia/Kolkata")


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class WebhookServer:
    """
    Runs a Flask server in a background daemon thread.
    Converts incoming Chartink signals → strategy3.process_incoming_signal().
    """

    def __init__(self, strategy3, mapper, port: int = 5001):
        self.strategy3 = strategy3
        self.mapper    = mapper
        self.port      = port
        self._app      = None
        self._started  = False

    # ── Symbol parsing ────────────────────────────────────────────────────────
    def _parse_payload(self, request):
        """
        Extract stock symbols from a Chartink POST request.
        Returns list of Fyers-format symbols.
        """
        # Try JSON body first, then form-encoded
        try:
            if request.is_json:
                data = request.get_json(force=True) or {}
            else:
                data = request.form.to_dict()
        except Exception:
            data = {}

        stocks_raw = data.get("stocks", "")
        if isinstance(stocks_raw, list):
            plain_names = [str(s).strip().upper() for s in stocks_raw if str(s).strip()]
        else:
            plain_names = [s.strip().upper()
                           for s in str(stocks_raw).split(",") if s.strip()]

        fyers_symbols = []
        for plain in plain_names:
            sym = self.mapper.get_fyers_symbol(plain)
            if sym:
                fyers_symbols.append(sym)

        return fyers_symbols

    # ── Flask app ─────────────────────────────────────────────────────────────
    def _create_app(self):
        try:
            from flask import Flask, request, jsonify
        except ImportError:
            print("❌ Flask not installed. Run: pip install flask")
            return None

        app = Flask(__name__)
        # Suppress Flask startup banner and request logs
        import logging
        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)

        strategy3 = self.strategy3

        @app.route("/s3/buy", methods=["POST"])
        def s3_buy():
            symbols = self._parse_payload(request)
            now     = datetime.now(IST)
            if not symbols:
                return jsonify({"status": "error", "message": "no symbols parsed"}), 400
            for sym in symbols:
                price = _live_price(sym)
                strategy3.process_incoming_signal("BUY", sym, price, now)
            return jsonify({"status": "ok", "symbols": symbols}), 200

        @app.route("/s3/sell", methods=["POST"])
        def s3_sell():
            symbols = self._parse_payload(request)
            now     = datetime.now(IST)
            if not symbols:
                return jsonify({"status": "error", "message": "no symbols parsed"}), 400
            for sym in symbols:
                price = _live_price(sym)
                strategy3.process_incoming_signal("SELL", sym, price, now)
            return jsonify({"status": "ok", "symbols": symbols}), 200

        @app.route("/s3/exit-buy", methods=["POST"])
        def s3_exit_buy():
            symbols = self._parse_payload(request)
            now     = datetime.now(IST)
            if not symbols:
                return jsonify({"status": "error", "message": "no symbols parsed"}), 400
            for sym in symbols:
                price = _live_price(sym)
                strategy3.process_incoming_signal("EXIT_BUY", sym, price, now)
            return jsonify({"status": "ok", "symbols": symbols}), 200

        @app.route("/s3/exit-sell", methods=["POST"])
        def s3_exit_sell():
            symbols = self._parse_payload(request)
            now     = datetime.now(IST)
            if not symbols:
                return jsonify({"status": "error", "message": "no symbols parsed"}), 400
            for sym in symbols:
                price = _live_price(sym)
                strategy3.process_incoming_signal("EXIT_SELL", sym, price, now)
            return jsonify({"status": "ok", "symbols": symbols}), 200

        @app.route("/s3/status", methods=["GET"])
        def s3_status():
            active = len(strategy3.state_store.all_active())
            return jsonify({
                "status":  "running",
                "scanner": "scanner_3",
                "active":  active,
            }), 200

        return app

    # ── Start ─────────────────────────────────────────────────────────────────
    def start(self):
        if self._started:
            return

        app = self._create_app()
        if app is None:
            return

        self._app = app

        def _run():
            try:
                ip = _get_local_ip()
                print(f"\n✅ S3 Webhook server started on port {self.port}")
                print(f"   BUY  : http://{ip}:{self.port}/s3/buy")
                print(f"   SELL : http://{ip}:{self.port}/s3/sell")
                print(f"   EXIT-BUY  : http://{ip}:{self.port}/s3/exit-buy")
                print(f"   EXIT-SELL : http://{ip}:{self.port}/s3/exit-sell\n")
                app.run(host="0.0.0.0", port=self.port,
                        threaded=True, use_reloader=False, debug=False)
            except OSError as e:
                if "Address already in use" in str(e) or "10048" in str(e):
                    print(f"\n❌ S3 Webhook server FAILED: port {self.port} is already in use.")
                    print(f"   Either stop the other process or change 'server_port' in config.json.\n")
                else:
                    print(f"\n❌ S3 Webhook server error: {e}\n")
            except Exception as e:
                print(f"\n❌ S3 Webhook server error: {e}\n")

        thread = threading.Thread(target=_run, name="s3-webhook-server", daemon=True)
        thread.start()
        self._started = True


# ── Module-level live price helper ───────────────────────────────────────────────
# Reads from the shared price_store singleton (avoids circular import).
def _live_price(symbol: str) -> float:
    """Return the latest tick price for symbol, or 0.0 if not available."""
    try:
        from core.price_store import get_price_store
        price = get_price_store().get(symbol)
        return float(price) if price else 0.0
    except Exception:
        return 0.0
