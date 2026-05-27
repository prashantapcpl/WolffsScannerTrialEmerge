"""
data_feed.py
Fyers v3 WebSocket — correctly handles the message format where
symbol name comes in the 'n' field and price data follows.
"""

import json
import os
import time
import threading
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")


def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, "r") as f:
        return json.load(f)


class DataFeed:

    def __init__(self, fyers_client, symbols: list,
                 on_tick_callback, timeframes: list):
        self.client         = fyers_client
        self.symbols        = symbols
        self.on_tick        = on_tick_callback
        self.timeframes     = timeframes
        self.config         = load_config()
        self.creds          = self.config["fyers_credentials"]
        self.connected      = False
        self.last_tick_time = None
        self.tick_count     = 0
        self.ws             = None
        self._last_symbol   = None   # tracks last symbol from 'n' field
        self.gap_start      = None   # set by restart(); gap-fill reads this

    def _get_stored_token(self) -> str:
        config     = load_config()
        token_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            config["token_file"]
        )
        with open(token_file, "r") as f:
            data = json.load(f)
        return data["access_token"]

    # ─── Callbacks ─────────────────────────────────────────────────────────────
    def _onopen(self):
        """Subscribe to symbols inside onopen — correct Fyers v3 pattern."""
        print(f"✅ WebSocket connected! Subscribing to {len(self.symbols)} symbols...")
        self.connected = True

        for i in range(0, len(self.symbols), 200):
            batch = self.symbols[i:i + 200]
            try:
                self.ws.subscribe(symbols=batch, data_type="SymbolUpdate")
                print(f"  📡 Subscribed batch {i//200 + 1}: {len(batch)} symbols")
                time.sleep(0.3)
            except Exception as e:
                print(f"  ⚠️  Subscribe error: {e}")

        print("✅ All symbols subscribed. Live ticks starting...")

    def _onmessage(self, message):
        """
        Handle Fyers v3 WebSocket messages.
        
        Fyers v3 sends data in this format:
        - Authentication/subscription messages: {'type': 'cn/sub/ful', ...}
        - Price tick: {'ltp': 1234.5, 'vol_traded_today': ..., 'n': 'NSE:RELIANCE-EQ', ...}
        
        The symbol is in the 'n' field within the tick message itself.
        """
        try:
            data = message
            if isinstance(message, (str, bytes)):
                try:
                    data = json.loads(message)
                except:
                    return

            if isinstance(data, list):
                for item in data:
                    self._process_message(item)
            elif isinstance(data, dict):
                self._process_message(data)

        except Exception as e:
            print(f"⚠️  Message error: {e}")

    def _process_message(self, data: dict):
        """Process a single message dict."""
        if not isinstance(data, dict):
            return

        # Skip system messages (authentication, subscription confirmations)
        msg_type = data.get("type", "")
        if msg_type in ("cn", "sub", "ful", "error"):
            return

        # Skip error messages
        if data.get("s") == "error":
            return

        # Extract symbol — Fyers v3 puts it in 'n' field
        symbol = (data.get("n") or
                  data.get("symbol") or
                  data.get("tk") or "")

        # Extract LTP
        ltp = (data.get("ltp") or
               data.get("last_price") or
               data.get("lp") or None)

        # Extract volume
        vol = (data.get("vol_traded_today") or
               data.get("volume") or
               data.get("vol") or 0)

        if symbol and ltp:
            try:
                self.last_tick_time = datetime.now(IST)
                self.tick_count    += 1

                if self.tick_count <= 3:
                    print(f"  ✅ TICK {self.tick_count}: {symbol} @ ₹{ltp}")

                if self.on_tick:
                    self.on_tick(symbol, float(ltp), int(vol or 0),
                                 self.last_tick_time)
            except Exception as e:
                print(f"⚠️  Callback error: {e}")
        elif ltp and not symbol:
            # LTP present but no symbol — log for debugging
            if self.tick_count == 0:
                print(f"  ⚠️  Tick has no symbol field. Full message: {str(data)[:300]}")

    def _onerror(self, message):
        if isinstance(message, dict) and message.get("code") == -300:
            # Invalid symbol — not a real error, just skip
            invalid = message.get("invalid_symbols", [])
            print(f"  ⚠️  Invalid symbols (removed from scan): {invalid}")
            return
        print(f"❌ WebSocket error: {message}")

    def _onclose(self, message):
        print(f"🔌 WebSocket closed: {message}")
        self.connected = False

    # ─── Connect ───────────────────────────────────────────────────────────────
    def connect_websocket(self):
        try:
            from fyers_apiv3.FyersWebsocket import data_ws

            token        = self._get_stored_token()
            access_token = f"{self.creds['client_id']}:{token}"

            print(f"🔌 Initializing Fyers WebSocket...")

            self.ws = data_ws.FyersDataSocket(
                access_token  = access_token,
                log_path      = "",
                litemode      = False,
                write_to_file = False,
                reconnect     = False,
                on_connect    = self._onopen,
                on_close      = self._onclose,
                on_error      = self._onerror,
                on_message    = self._onmessage,
            )

            self.ws.connect()

        except Exception as e:
            print(f"❌ WebSocket failed: {e}")
            import traceback
            traceback.print_exc()
            self.connected = False

    def start(self):
        thread = threading.Thread(target=self.connect_websocket, daemon=True)
        thread.start()
        print("🚀 Data feed starting...")
        return thread

    def restart(self):
        """Close the current WebSocket and reconnect — called by the feed watchdog."""
        print("🔌 DataFeed restarting...")
        # Record gap start BEFORE resetting last_tick_time so gap-fill knows
        # how far back to fetch missed candles.
        self.gap_start = self.last_tick_time
        try:
            if self.ws:
                # Fyers v3 SDK uses stop() not close()
                for method_name in ("stop", "disconnect", "close_socket", "close"):
                    fn = getattr(self.ws, method_name, None)
                    if fn:
                        fn()
                        break
        except Exception as e:
            print(f"   Close error (ignored): {e}")
        self.ws        = None
        self.connected = False
        # Do NOT reset last_tick_time here — watchdog uses tick_count to detect
        # real recovery, so faking last_tick_time would cause a false positive.
        time.sleep(2)
        threading.Thread(target=self.connect_websocket, daemon=True).start()
        print("🔌 DataFeed restart initiated.")

    # ─── Historical data ───────────────────────────────────────────────────────
    def fetch_historical(self, symbol: str, timeframe: str,
                          days_back: int = 5) -> list:
        tf_map     = {"1":"1","2":"2","5":"5","15":"15",
                      "30":"30","60":"60","D":"D","W":"W"}
        fyers_tf   = tf_map.get(str(timeframe), "5")
        end_date   = datetime.now(IST)
        start_date = end_date - timedelta(days=days_back)

        data = {
            "symbol":      symbol,
            "resolution":  fyers_tf,
            "date_format": "1",
            "range_from":  start_date.strftime("%Y-%m-%d"),
            "range_to":    end_date.strftime("%Y-%m-%d"),
            "cont_flag":   "1"
        }
        try:
            response = self.client.history(data=data)
            if response.get("code") == 200 and "candles" in response:
                candles = []
                for c in response["candles"]:
                    ts = datetime.fromtimestamp(c[0], tz=IST)
                    candles.append({
                        "datetime": ts,
                        "open": c[1], "high": c[2],
                        "low":  c[3], "close":c[4], "volume":c[5]
                    })
                return candles
            return []
        except Exception as e:
            print(f"  ❌ History error {symbol}: {e}")
            return []

    def fetch_historical_closes(self, symbol: str, timeframe: str,
                                 days_back: int = 10) -> list:
        return [c["close"] for c in
                self.fetch_historical(symbol, timeframe, days_back)]

    def get_status(self) -> dict:
        return {
            "connected":      self.connected,
            "symbols_count":  len(self.symbols),
            "tick_count":     self.tick_count,
            "last_tick_time": self.last_tick_time.isoformat()
                              if self.last_tick_time else None
        }
