"""
price_store.py
Lightweight store for live tick prices.
Updated on every tick; flushed to disk every 3 seconds.
Dashboard reads this for the "Now" price column instead of the
60-second-stale current_price field in the full state file.
"""

import json
import os
import time
import threading
from datetime import datetime
import pytz

ROOT       = os.path.dirname(os.path.dirname(__file__))
IST        = pytz.timezone("Asia/Kolkata")
PRICE_FILE = os.path.join(ROOT, "data", "live_prices.json")

_FLUSH_INTERVAL = 3   # seconds


class PriceStore:

    def __init__(self):
        self._prices = {}          # {symbol: price}
        self._dirty  = False
        self._lock   = threading.Lock()

    def update(self, symbol: str, price: float):
        with self._lock:
            self._prices[symbol] = price
            self._dirty = True

    def get(self, symbol: str) -> float | None:
        return self._prices.get(symbol)

    def flush(self):
        with self._lock:
            if not self._dirty:
                return
            data = {
                "prices":     dict(self._prices),
                "updated_at": datetime.now(IST).isoformat(),
            }
            snapshot = data
            self._dirty = False

        os.makedirs(os.path.dirname(PRICE_FILE), exist_ok=True)
        with open(PRICE_FILE, "w") as f:
            json.dump(snapshot, f)

    def start_flush_thread(self):
        def loop():
            while True:
                time.sleep(_FLUSH_INTERVAL)
                try:
                    self.flush()
                except Exception:
                    pass
        threading.Thread(target=loop, daemon=True).start()


# Singleton
_store = None

def get_price_store() -> PriceStore:
    global _store
    if _store is None:
        _store = PriceStore()
    return _store


def load_live_prices() -> dict:
    """Read live_prices.json from disk — called by the dashboard."""
    if not os.path.exists(PRICE_FILE):
        return {}
    try:
        with open(PRICE_FILE, "r") as f:
            return json.load(f).get("prices", {})
    except Exception:
        return {}
