"""
data_store.py
Stores and manages historical OHLCV candle data locally on disk.
- Saves candles as CSV files per symbol per timeframe
- Fetches only MISSING candles on each startup (incremental update)
- Used by: Live Scanner, Carry-Forward logic, Backtester

Storage location: data/price_history/
File naming: NSE_RELIANCE-EQ_5m.csv
"""

import os
import json
import csv
import time
from datetime import datetime, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

# ─── Max history per timeframe ─────────────────────────────────────────────────
MAX_HISTORY_DAYS = {
    "1":  100,
    "2":  100,
    "5":  100,
    "15": 100,
    "30": 100,
    "60": 100,
    "D":  1095,   # 3 years (3 x 365)
    "W":  1095,   # 3 years (derived from daily)
}

# How many days per single API request (Fyers limit)
DAYS_PER_REQUEST = {
    "1":  100,
    "2":  100,
    "5":  100,
    "15": 100,
    "30": 100,
    "60": 100,
    "D":  366,
    "W":  366,
}

CSV_HEADERS = ["datetime", "open", "high", "low", "close", "volume"]


def load_config():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "config.json"), "r") as f:
        return json.load(f)


class DataStore:
    """
    Local candle data storage.
    One CSV file per symbol per timeframe.
    """

    def __init__(self):
        config = load_config()
        root   = os.path.dirname(os.path.dirname(__file__))
        self.history_dir = os.path.join(root, "data", "price_history")
        os.makedirs(self.history_dir, exist_ok=True)
        # In-memory cache of last-written candle time per (symbol, tf).
        # Prevents duplicate appends when live feed and history manager
        # write the same candle (e.g. Fyers settlement candles).
        self._last_saved: dict = {}   # (symbol, tf) → datetime

    # ─── File path helper ──────────────────────────────────────────────────────
    def _filepath(self, symbol: str, timeframe: str) -> str:
        """Get CSV file path for a symbol+timeframe."""
        safe_sym = symbol.replace(":", "_").replace("/", "_")
        return os.path.join(self.history_dir, f"{safe_sym}_{timeframe}m.csv")

    # ─── Save candles ──────────────────────────────────────────────────────────
    def save_candles(self, symbol: str, timeframe: str, candles: list,
                     append: bool = False):
        """
        Save candles to CSV.
        candles: list of dicts with keys: datetime, open, high, low, close, volume
        append=True adds to existing file, False overwrites.
        Sat/Sun rows are filtered out for intraday TFs -- Fyers API has been
        observed to return phantom non-trading-day rows that poison RSI.
        """
        if not candles:
            return

        # Filter out non-trading-day rows for intraday timeframes. D and W
        # CSV files are aggregated daily/weekly so don't need this filter.
        if timeframe not in ("D", "W"):
            filtered = []
            for c in candles:
                dt = c["datetime"]
                if not isinstance(dt, datetime):
                    try:
                        dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        continue
                if dt.weekday() >= 5:
                    continue   # Saturday/Sunday -- skip
                filtered.append(c)
            candles = filtered
            if not candles:
                return

        if append:
            # Skip candles whose timestamp is not newer than the last we saved.
            # This prevents duplicates from Fyers settlement candles and from
            # the live feed writing a candle that history manager already stored.
            key      = (symbol, timeframe)
            last_key = self._last_saved.get(key)
            if last_key is None:
                # Lazy-init: find last row in CSV without loading everything
                last_key = self._read_last_datetime(symbol, timeframe)
                self._last_saved[key] = last_key
            if last_key is not None:
                candles = [c for c in candles
                           if (c["datetime"] if isinstance(c["datetime"], datetime)
                               else datetime.strptime(c["datetime"], "%Y-%m-%d %H:%M:%S")) > last_key]
            if not candles:
                return

        filepath = self._filepath(symbol, timeframe)
        mode     = "a" if append and os.path.exists(filepath) else "w"

        with open(filepath, mode, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            if mode == "w":
                writer.writeheader()
            for candle in candles:
                dt = candle["datetime"]
                if isinstance(dt, datetime):
                    dt = dt.strftime("%Y-%m-%d %H:%M:%S")
                writer.writerow({
                    "datetime": dt,
                    "open":     round(candle["open"],  2),
                    "high":     round(candle["high"],  2),
                    "low":      round(candle["low"],   2),
                    "close":    round(candle["close"], 2),
                    "volume":   int(candle.get("volume", 0)),
                })

        # Update last-saved tracker
        if append:
            last_dt = candles[-1]["datetime"]
            if not isinstance(last_dt, datetime):
                last_dt = datetime.strptime(last_dt, "%Y-%m-%d %H:%M:%S")
            self._last_saved[(symbol, timeframe)] = last_dt

    def save_candles_merged(self, symbol: str, timeframe: str, new_candles: list):
        """
        Merge new_candles into the existing CSV: load all, combine, sort,
        dedup (first occurrence kept), rewrite. Used when gap-fill is needed
        (e.g. scanner restarted mid-day and live candles advanced _last_saved
        past earlier same-day historical candles).
        """
        if not new_candles:
            return
        existing = self.load_candles(symbol, timeframe)
        combined = existing + new_candles
        combined.sort(key=lambda c: c["datetime"])
        seen, deduped = set(), []
        for c in combined:
            dt = c["datetime"]
            ts = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, "strftime") else str(dt)[:19]
            if ts not in seen:
                seen.add(ts)
                deduped.append(c)
        self.save_candles(symbol, timeframe, deduped, append=False)
        if deduped:
            last_dt = deduped[-1]["datetime"]
            if not isinstance(last_dt, datetime):
                last_dt = datetime.strptime(str(last_dt)[:19], "%Y-%m-%d %H:%M:%S")
            if last_dt.tzinfo is None:
                last_dt = IST.localize(last_dt)
            self._last_saved[(symbol, timeframe)] = last_dt

    def _count_today_candles(self, symbol: str, timeframe: str) -> int:
        """Count candles from today by scanning the tail of the CSV (fast, no full load)."""
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        filepath  = self._filepath(symbol, timeframe)
        if not os.path.exists(filepath):
            return 0
        count = 0
        try:
            with open(filepath, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # 8 KB covers ~100+ rows — more than enough for one trading day
                f.seek(max(0, size - 8192))
                tail = f.read().decode("utf-8", errors="ignore")
            for line in tail.split("\n"):
                if line.startswith(today_str):
                    count += 1
        except Exception:
            pass
        return count

    def _read_last_datetime(self, symbol: str, timeframe: str) -> datetime | None:
        """Read only the last datetime from the CSV — fast, no full load."""
        filepath = self._filepath(symbol, timeframe)
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "rb") as f:
                # Seek to end and scan backwards for last non-empty line
                f.seek(0, 2)
                size = f.tell()
                if size < 2:
                    return None
                pos = size - 1
                while pos > 0:
                    f.seek(pos)
                    ch = f.read(1)
                    if ch == b"\n" and pos < size - 1:
                        break
                    pos -= 1
                last_line = f.read().decode("utf-8", errors="ignore").strip()
            if not last_line or last_line.startswith("datetime"):
                return None
            dt_str = last_line.split(",")[0]
            return IST.localize(datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return None

    # ─── Load candles ──────────────────────────────────────────────────────────
    @staticmethod
    def _is_market_hours(dt: datetime) -> bool:
        # dt is the candle OPEN time. Candles opening at 15:25 close at 15:30
        # (last real candle). Candles opening AT 15:30 are NSE closing-auction
        # candles with an artificial settlement price — excluded from RSI data.
        # Sat (weekday=5) and Sun (weekday=6) are non-trading days — exclude
        # them to prevent phantom Fyers-API rows from poisoning RSI.
        if dt.weekday() >= 5:
            return False
        h, m = dt.hour, dt.minute
        return (h == 9 and m >= 15) or (10 <= h <= 14) or (h == 15 and m < 30)

    def load_candles(self, symbol: str, timeframe: str,
                     from_date: datetime = None,
                     to_date:   datetime = None) -> list:
        """
        Load candles from CSV.
        Returns list of dicts: {datetime, open, high, low, close, volume}
        Intraday timeframes are filtered to market hours only (09:15–15:30 IST)
        so legacy junk rows from old API downloads never corrupt RSI seeding.
        """
        filepath = self._filepath(symbol, timeframe)
        if not os.path.exists(filepath):
            return []

        intraday = timeframe not in ("D", "W")
        candles = []
        with open(filepath, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
                    dt = IST.localize(dt)

                    if intraday and not self._is_market_hours(dt):
                        continue
                    if from_date and dt < from_date:
                        continue
                    if to_date and dt > to_date:
                        continue

                    candles.append({
                        "datetime": dt,
                        "open":     float(row["open"]),
                        "high":     float(row["high"]),
                        "low":      float(row["low"]),
                        "close":    float(row["close"]),
                        "volume":   int(row["volume"]),
                    })
                except Exception:
                    continue

        return candles

    def load_closes(self, symbol: str, timeframe: str,
                    from_date: datetime = None) -> list:
        """Load just the close prices — used for RSI seeding."""
        candles = self.load_candles(symbol, timeframe, from_date=from_date)
        return [c["close"] for c in candles]

    # ─── Aggregate daily / weekly from 5m ─────────────────────────────────────
    def load_daily_from_5m(self, symbol: str) -> list:
        """
        Build daily OHLCV by aggregating 5m candles.
        Daily close = last 5m candle close (15:25 open → 15:30 close),
        matching Fyers chart's RSI which also uses this price, not the
        NSE closing-auction settlement price stored in the daily CSV.
        Falls back to empty list if no 5m data is available.
        """
        candles_5m = self.load_candles(symbol, "5")
        if not candles_5m:
            return []

        from collections import defaultdict
        days: dict = defaultdict(list)
        for c in candles_5m:
            days[c["datetime"].date()].append(c)

        daily = []
        for day in sorted(days.keys()):
            dc = days[day]
            daily.append({
                "datetime": IST.localize(
                    datetime(day.year, day.month, day.day, 9, 15)),
                "open":   dc[0]["open"],
                "high":   max(c["high"]   for c in dc),
                "low":    min(c["low"]    for c in dc),
                "close":  dc[-1]["close"],
                "volume": sum(c["volume"] for c in dc),
            })
        return daily

    def load_weekly_from_daily(self, daily_candles: list) -> list:
        """
        Build weekly OHLCV from daily candles grouped by ISO week.
        Weekly close = last trading day's close.
        """
        if not daily_candles:
            return []

        from collections import defaultdict
        weeks: dict = defaultdict(list)
        for c in daily_candles:
            iso_year, iso_week, _ = c["datetime"].isocalendar()
            weeks[(iso_year, iso_week)].append(c)

        weekly = []
        for key in sorted(weeks.keys()):
            wc = sorted(weeks[key], key=lambda c: c["datetime"])
            weekly.append({
                "datetime": wc[0]["datetime"],
                "open":     wc[0]["open"],
                "high":     max(c["high"]   for c in wc),
                "low":      min(c["low"]    for c in wc),
                "close":    wc[-1]["close"],
                "volume":   sum(c["volume"] for c in wc),
            })
        return weekly

    # ─── Last stored date ──────────────────────────────────────────────────────
    def get_last_stored_date(self, symbol: str, timeframe: str) -> datetime | None:
        """Get the datetime of the most recent stored candle."""
        return self._read_last_datetime(symbol, timeframe)

    def has_data(self, symbol: str, timeframe: str) -> bool:
        filepath = self._filepath(symbol, timeframe)
        return os.path.exists(filepath) and os.path.getsize(filepath) > 100

    # ─── Deduplicate ──────────────────────────────────────────────────────────
    def deduplicate(self, symbol: str, timeframe: str):
        """Remove duplicate rows from CSV (same datetime)."""
        candles = self.load_candles(symbol, timeframe)
        if not candles:
            return
        seen = set()
        unique = []
        for c in candles:
            key = c["datetime"].strftime("%Y-%m-%d %H:%M:%S")
            if key not in seen:
                seen.add(key)
                unique.append(c)
        if len(unique) < len(candles):
            self.save_candles(symbol, timeframe, unique, append=False)

    # ─── Stats ────────────────────────────────────────────────────────────────
    def get_stats(self, symbol: str, timeframe: str) -> dict:
        """Return stats about stored data for a symbol+timeframe."""
        candles = self.load_candles(symbol, timeframe)
        if not candles:
            return {"count": 0, "from": None, "to": None}
        return {
            "count": len(candles),
            "from":  candles[0]["datetime"].strftime("%Y-%m-%d"),
            "to":    candles[-1]["datetime"].strftime("%Y-%m-%d"),
        }

    def total_storage_mb(self) -> float:
        """Return total size of all stored data in MB."""
        total = 0
        for f in os.listdir(self.history_dir):
            total += os.path.getsize(os.path.join(self.history_dir, f))
        return round(total / (1024 * 1024), 2)


# Singleton
_store = None
def get_store() -> DataStore:
    global _store
    if _store is None:
        _store = DataStore()
    return _store
