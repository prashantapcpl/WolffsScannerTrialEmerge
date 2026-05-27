"""
rsi_cache.py
Pre-computes RSI for all symbols and timeframes and saves to disk.

Built at 3:31 PM after market close (or on first run).
Loaded at startup in under 5 seconds instead of re-reading 163MB of CSVs.

Cache file: data/rsi_cache.json
Format: { "symbol": { "timeframe": rsi_value, ... }, ... }
Also stores: full RSI series for carry-forward replay
"""

import os
import sys
import json
import pickle
from datetime import datetime, date
import pytz

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

IST        = pytz.timezone("Asia/Kolkata")
CACHE_FILE = os.path.join(ROOT, "data", "rsi_cache.pkl")


def load_config():
    with open(os.path.join(ROOT, "config.json"), "r") as f:
        return json.load(f)


class RSICache:
    """
    Stores pre-computed RSI values and series for all symbols/timeframes.
    Eliminates need to re-read CSVs on every startup or rescan.
    """

    def __init__(self):
        self._cache     = {}    # symbol → {tf → last_rsi}
        self._series    = {}    # symbol → {tf → [rsi_val, ...]}
        self._dates     = {}    # symbol → {tf → [datetime_str, ...]}
        self._closes    = {}    # symbol → {tf → [close, ...]}
        self._states    = {}    # symbol → {tf → {avg_gain, avg_loss, rsi, last_close, prev_close}}
        self._built_at  = None
        self._built_date= None

    def build(self, symbols: list, timeframes: list,
              data_store, rsi_period: int = 14):
        """
        Pre-compute RSI for all symbols and timeframes.
        Called once after market close. Takes 2-3 minutes.
        """
        from core.rsi_engine import RSICalculator

        total   = len(symbols) * len(timeframes)
        done    = 0
        skipped = 0

        print(f"\n🔧 Building RSI cache...")
        print(f"   Symbols: {len(symbols)} | Timeframes: {timeframes}")
        print(f"   RSI Period: {rsi_period}\n")

        for symbol in symbols:
            self._cache[symbol]  = {}
            self._series[symbol] = {}
            self._dates[symbol]  = {}
            self._closes[symbol] = {}

            for tf in timeframes:
                # D: build from 5m aggregation so daily close = last 5m
                # candle close (15:25), matching Fyers chart RSI.
                # W: use the weekly CSV (3 years = 162 bars, well-converged).
                # Fallback to CSV if no 5m data exists for this symbol.
                if tf == "D":
                    candles = data_store.load_daily_from_5m(symbol)
                    if not candles:
                        candles = data_store.load_candles(symbol, "D")
                else:
                    candles = data_store.load_candles(symbol, tf)

                if len(candles) < rsi_period + 2:
                    skipped += 1
                    done    += 1
                    continue

                # Deduplicate by datetime — keeps first occurrence
                # (real intraday candle, not Fyers settlement candle).
                # Also strip after-hours candles for intraday TFs; Fyers API
                # occasionally returns pre/post-market entries that shift RSI.
                seen_dt = set()
                clean   = []
                for c in candles:
                    key = c["datetime"]
                    if key in seen_dt:
                        continue
                    seen_dt.add(key)
                    if tf not in ("D", "W"):
                        h, m = key.hour, key.minute
                        if not ((h == 9 and m >= 15) or
                                (10 <= h <= 14) or
                                (h == 15 and m < 30)):
                            continue
                    clean.append(c)
                candles = clean

                closes    = [c["close"]    for c in candles]
                datetimes = [c["datetime"].strftime("%Y-%m-%d %H:%M:%S")
                             for c in candles]

                # Calculate full RSI series
                calc     = RSICalculator(period=rsi_period)
                rsi_vals = []
                for close in closes:
                    rsi = calc.update(close)
                    rsi_vals.append(round(rsi, 4) if rsi is not None else None)

                # Store last RSI value and calculator state
                last_rsi = next((r for r in reversed(rsi_vals) if r is not None), None)
                self._cache[symbol][tf]  = last_rsi
                self._series[symbol][tf] = rsi_vals
                self._dates[symbol][tf]  = datetimes
                self._closes[symbol][tf] = [round(c, 2) for c in closes]
                state = calc.get_state()
                if state:
                    self._states.setdefault(symbol, {})[tf] = state

                done += 1
                if done % 500 == 0:
                    pct = round((done / total) * 100, 1)
                    print(f"   ⏳ {done}/{total} ({pct}%)")

        self._built_at   = datetime.now(IST).isoformat()
        self._built_date = str(date.today())

        print(f"\n✅ RSI cache built: {done - skipped} combinations "
              f"({skipped} skipped — insufficient data)")
        self.save()

    def update_symbols(self, symbols: list, timeframes: list,
                       data_store, rsi_period: int = 14) -> int:
        """
        Update RSI cache in memory for specific symbols from freshly-saved CSVs.
        Called after gap-fill so carry-forward can replay the correct history.
        Does NOT save to disk or update _built_at/_built_date.
        Returns count of (symbol, tf) combinations updated.
        """
        from core.rsi_engine import RSICalculator

        updated = 0
        for symbol in symbols:
            self._cache.setdefault(symbol, {})
            self._series.setdefault(symbol, {})
            self._dates.setdefault(symbol, {})
            self._closes.setdefault(symbol, {})

            for tf in timeframes:
                if tf == "D":
                    candles = data_store.load_daily_from_5m(symbol)
                    if not candles:
                        candles = data_store.load_candles(symbol, "D")
                else:
                    candles = data_store.load_candles(symbol, tf)

                if len(candles) < rsi_period + 2:
                    continue

                seen_dt = set()
                clean   = []
                for c in candles:
                    key = c["datetime"]
                    if key in seen_dt:
                        continue
                    seen_dt.add(key)
                    if tf not in ("D", "W"):
                        h, m = key.hour, key.minute
                        if not ((h == 9 and m >= 15) or
                                (10 <= h <= 14) or
                                (h == 15 and m < 30)):
                            continue
                    clean.append(c)

                if len(clean) < rsi_period + 2:
                    continue

                closes    = [c["close"] for c in clean]
                datetimes = [c["datetime"].strftime("%Y-%m-%d %H:%M:%S")
                             for c in clean]

                calc     = RSICalculator(period=rsi_period)
                rsi_vals = []
                for close in closes:
                    rsi = calc.update(close)
                    rsi_vals.append(round(rsi, 4) if rsi is not None else None)

                last_rsi = next((r for r in reversed(rsi_vals) if r is not None), None)
                self._cache[symbol][tf]  = last_rsi
                self._series[symbol][tf] = rsi_vals
                self._dates[symbol][tf]  = datetimes
                self._closes[symbol][tf] = [round(c, 2) for c in closes]
                state = calc.get_state()
                if state:
                    self._states.setdefault(symbol, {})[tf] = state
                updated += 1

        return updated

    def save(self):
        """Save cache to disk as pickle (10-20x faster than JSON for numerical data)."""
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        data = {
            "built_at":   self._built_at,
            "built_date": self._built_date,
            "cache":      self._cache,
            "series":     self._series,
            "dates":      self._dates,
            "closes":     self._closes,
            "states":     self._states,
        }
        with open(CACHE_FILE, "wb") as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
        size_mb = round(os.path.getsize(CACHE_FILE) / (1024 * 1024), 2)
        print(f"💾 RSI cache saved: {size_mb} MB")

    def load(self) -> bool:
        """Load cache from disk. Returns True if loaded successfully."""
        # Try pickle first; fall back to old JSON format if present
        pkl  = CACHE_FILE
        json_file = pkl.replace(".pkl", ".json")
        target = pkl if os.path.exists(pkl) else (
                 json_file if os.path.exists(json_file) else None)
        if not target:
            return False
        try:
            if target.endswith(".pkl"):
                with open(target, "rb") as f:
                    data = pickle.load(f)
            else:
                with open(target, "r") as f:
                    data = json.load(f)
            self._cache      = data.get("cache",  {})
            self._series     = data.get("series", {})
            self._dates      = data.get("dates",  {})
            self._closes     = data.get("closes", {})
            self._states     = data.get("states", {})
            self._built_at   = data.get("built_at")
            self._built_date = data.get("built_date")
            size_mb = round(os.path.getsize(target) / (1024 * 1024), 2)
            print(f"✅ RSI cache loaded: {size_mb} MB "
                  f"(built: {self._built_date})")
            return True
        except Exception as e:
            print(f"⚠️  RSI cache load failed: {e}")
            return False

    def is_fresh(self) -> bool:
        """Returns True if cache was built today."""
        return self._built_date == str(date.today())

    def get_last_rsi(self, symbol: str, timeframe: str) -> float | None:
        """Get the last RSI value for a symbol+timeframe."""
        return self._cache.get(symbol, {}).get(timeframe)

    def get_rsi_series(self, symbol: str, timeframe: str) -> list:
        """Get full RSI series for carry-forward replay."""
        return self._series.get(symbol, {}).get(timeframe, [])

    def get_closes(self, symbol: str, timeframe: str) -> list:
        """Get stored close prices."""
        return self._closes.get(symbol, {}).get(timeframe, [])

    def get_datetimes(self, symbol: str, timeframe: str) -> list:
        """Get stored datetime strings."""
        return self._dates.get(symbol, {}).get(timeframe, [])

    def seed_rsi_engine(self, rsi_engine, symbols: list, timeframes: list):
        """
        Seed the live RSI engine from cache.
        Fast path: restore avg_gain/avg_loss directly (no history replay).
        Fallback: replay last 200 closes if state not saved (old cache file).
        """
        seeded = 0
        fast   = 0
        for symbol in symbols:
            for tf in timeframes:
                state = self._states.get(symbol, {}).get(tf)
                if state and state.get("avg_gain") is not None:
                    # Instant restore — just 5 numbers, no loop
                    rsi_engine.restore(symbol, tf, state)
                    fast   += 1
                    seeded += 1
                else:
                    # Fallback: replay 200 closes (old cache or missing state)
                    closes = self.get_closes(symbol, tf)
                    if closes and len(closes) >= 2:
                        rsi_engine.seed(symbol, tf, closes[-200:])
                        seeded += 1
        print(f"✅ RSI engine seeded: {seeded} combinations "
              f"({fast} instant, {seeded - fast} via replay)")
        return seeded


# Singleton
_rsi_cache = None

def get_rsi_cache() -> RSICache:
    global _rsi_cache
    if _rsi_cache is None:
        _rsi_cache = RSICache()
    return _rsi_cache
