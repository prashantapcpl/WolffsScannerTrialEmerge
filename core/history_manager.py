"""
history_manager.py
Downloads and keeps historical candle data up to date.

- First run: downloads maximum available history for all symbols
- Daily: fetches only new candles since last stored date
- Handles Fyers 100-day per request limit by making multiple calls
- Used by: startup seeding, carry-forward, backtester
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from core.data_store import DataStore, MAX_HISTORY_DAYS, DAYS_PER_REQUEST

IST = pytz.timezone("Asia/Kolkata")


def load_config():
    with open(os.path.join(ROOT, "config.json"), "r") as f:
        return json.load(f)


class HistoryManager:
    """
    Downloads and maintains historical OHLCV data for all symbols.
    """

    def __init__(self, fyers_client):
        self.client = fyers_client
        self.store  = DataStore()
        self.config = load_config()

    # ─── Fetch from Fyers API ──────────────────────────────────────────────────
    def _fetch_range(self, symbol: str, timeframe: str,
                     from_date: datetime, to_date: datetime) -> list:
        """
        Fetch candles from Fyers API for a specific date range.
        Returns list of candle dicts.
        """
        data = {
            "symbol":      symbol,
            "resolution":  timeframe,
            "date_format": "1",
            "range_from":  from_date.strftime("%Y-%m-%d"),
            "range_to":    to_date.strftime("%Y-%m-%d"),
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
                        "open":     c[1],
                        "high":     c[2],
                        "low":      c[3],
                        "close":    c[4],
                        "volume":   c[5]
                    })
                # Strip after-hours candles for intraday timeframes —
                # Fyers API occasionally returns pre/post-market entries
                # that corrupt RSI series.
                if timeframe not in ("D", "W"):
                    candles = [c for c in candles
                               if self._is_market_hours(c["datetime"])]
                return candles
            else:
                return []
        except Exception as e:
            print(f"      ⚠️  API error {symbol} [{timeframe}]: {e}")
            return []

    @staticmethod
    def _is_market_hours(dt: datetime) -> bool:
        h, m = dt.hour, dt.minute
        return (h == 9 and m >= 15) or (10 <= h <= 14) or (h == 15 and m < 30)

    def _fetch_full_history(self, symbol: str, timeframe: str) -> list:
        """
        Fetch maximum available history by making multiple API calls
        if needed (Fyers allows 100 days per request for intraday,
        366 days for daily).
        """
        max_days     = MAX_HISTORY_DAYS.get(timeframe, 100)
        days_per_req = DAYS_PER_REQUEST.get(timeframe, 100)

        today      = datetime.now(IST).replace(
                        hour=0, minute=0, second=0, microsecond=0)
        all_candles = []

        # Work backwards from today
        end_date   = today
        start_date = today - timedelta(days=max_days)

        current_end = end_date
        while current_end > start_date:
            current_start = max(
                current_end - timedelta(days=days_per_req),
                start_date
            )

            candles = self._fetch_range(symbol, timeframe,
                                         current_start, current_end)
            if candles:
                all_candles = candles + all_candles

            current_end = current_start - timedelta(days=1)

            # Rate limiting
            time.sleep(0.15)

        return all_candles

    def _fetch_incremental(self, symbol: str, timeframe: str,
                            last_date: datetime) -> list:
        """
        Fetch only new candles since the last stored date.
        Much faster than full download — used every day.
        """
        today = datetime.now(IST).replace(
                    hour=23, minute=59, second=0, microsecond=0)
        from_date = last_date - timedelta(days=1)  # overlap 1 day to be safe

        candles = self._fetch_range(symbol, timeframe, from_date, today)

        # Filter to only candles newer than last stored
        new_candles = [c for c in candles if c["datetime"] > last_date]
        return new_candles

    # ─── Update single symbol ──────────────────────────────────────────────────
    def update_symbol(self, symbol: str, timeframe: str,
                      force_full: bool = False) -> int:
        """
        Update candle data for one symbol+timeframe.
        Returns number of new candles added.
        """
        last_date = self.store.get_last_stored_date(symbol, timeframe)
        now       = datetime.now(IST)

        if last_date is None or force_full:
            # No data stored yet — download full history
            candles = self._fetch_full_history(symbol, timeframe)
            if candles:
                self.store.save_candles(symbol, timeframe, candles,
                                         append=False)
            return len(candles)

        # After market close: skip API call when today's data is complete.
        # Uses candle count (not just last timestamp) so a symbol with candles
        # only from 15:00 onwards is NOT mistakenly treated as complete.
        # Expected counts (intraday): 5m=75, 10m=37, 15m=25, 30m=13, 60m=7
        _EXPECTED = {"5": 75, "10": 37, "15": 25, "30": 13, "60": 7}
        after_close = now.hour > 15 or (now.hour == 15 and now.minute >= 31)
        if after_close and last_date.date() == now.date():
            if timeframe in ("D", "W"):
                return 0
            today_count = self.store._count_today_candles(symbol, timeframe)
            threshold   = int(_EXPECTED.get(timeframe, 50) * 0.85)
            if today_count >= threshold:
                return 0  # today's data is complete — no gap to fill
            # today_count < threshold → gap detected → fall through to fetch

        # Same-day restart: live feed may have written a candle (e.g. 15:00)
        # BEFORE history manager ran, advancing last_stored_date past today's
        # earlier candles (09:15–14:45). A plain incremental fetch would filter
        # those out as "not newer than last_date". Use merge+dedup instead.
        if last_date.date() == now.date() and timeframe not in ("D", "W"):
            from_date = last_date.replace(
                hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
            candles = self._fetch_range(symbol, timeframe, from_date, now)
            if candles:
                # Only merge if there are candles earlier than last_date
                # (real gap); otherwise fall through to a normal append.
                gap_candles = [c for c in candles if c["datetime"] < last_date]
                if gap_candles:
                    self.store.save_candles_merged(symbol, timeframe, candles)
                    return len(gap_candles)
                else:
                    new_only = [c for c in candles if c["datetime"] > last_date]
                    if new_only:
                        self.store.save_candles(symbol, timeframe, new_only,
                                                 append=True)
                    return len(new_only)
            return 0

        # Normal incremental: last_date is from a previous day.
        # Skip before market open only when no trading day was missed:
        #   - gap == 1 day  → yesterday's close, nothing new yet
        #   - gap == 3 days AND today is Monday → Fri→Mon weekend, no candles
        # Any other gap (e.g. Mon→Wed = 2 days) means a full trading day is
        # missing and MUST be fetched even if we haven't opened yet.
        before_open = now.hour < 9 or (now.hour == 9 and now.minute < 15)
        if before_open:
            gap_days        = (now.date() - last_date.date()).days
            is_weekend_gap  = (gap_days == 3 and now.weekday() == 0)
            if gap_days <= 1 or is_weekend_gap:
                return 0

        new_candles = self._fetch_incremental(symbol, timeframe, last_date)
        if new_candles:
            self.store.save_candles(symbol, timeframe, new_candles, append=True)
            self.store.deduplicate(symbol, timeframe)
        return len(new_candles)

    # ─── Update all symbols ────────────────────────────────────────────────────
    def update_all(self, symbols: list, timeframes: list,
                   force_full: bool = False,
                   progress_callback=None) -> dict:
        """
        Update all symbols across all timeframes.
        Called at startup — runs in background thread.

        Returns summary dict.
        """
        total      = len(symbols) * len(timeframes)
        done       = 0
        new_total  = 0
        errors     = 0

        print(f"\n{'='*60}")
        print(f"  HISTORY UPDATE")
        print(f"  Symbols: {len(symbols)} | Timeframes: {timeframes}")
        print(f"  Mode: {'FULL DOWNLOAD' if force_full else 'INCREMENTAL'}")
        print(f"{'='*60}\n")

        for i, symbol in enumerate(symbols):
            symbol_new = 0
            for tf in timeframes:
                try:
                    new_count  = self.update_symbol(symbol, tf, force_full)
                    new_total += new_count
                    symbol_new += new_count
                    done      += 1
                except Exception as e:
                    errors += 1
                    done   += 1

                # Progress every 100 operations
                if done % 100 == 0:
                    pct = round((done / total) * 100, 1)
                    print(f"   ⏳ Progress: {done}/{total} ({pct}%) | "
                          f"New candles: {new_total}")

                if progress_callback:
                    progress_callback(done, total)

            # Rate-limit only when API calls were made (new data fetched).
            # When all timeframes skip instantly (count threshold passed),
            # sleeping wastes ~60s across 633 symbols for no reason.
            if symbol_new > 0:
                time.sleep(0.1)

        storage_mb = self.store.total_storage_mb()
        print(f"\n✅ History update complete!")
        print(f"   New candles added : {new_total}")
        print(f"   Errors            : {errors}")
        print(f"   Total storage     : {storage_mb} MB\n")

        return {
            "done":       done,
            "new_candles":new_total,
            "errors":     errors,
            "storage_mb": storage_mb
        }

    # ─── Checker bot ─────────────────────────────────────────────────────────
    def run_checker_bot(self, symbols: list, timeframes: list) -> dict:
        """
        Startup data validator.  Runs after update_all().

        Uses cohort-based staleness: for each timeframe, the date held by
        90% of symbols is the "cohort date".  Any symbol behind that date
        is force-fetched — timing heuristics (before_open, gap_days, count
        threshold) are completely bypassed.

        Returns summary dict: {checked, stale, fixed, errors}
        """
        print(f"\n{'='*60}")
        print(f"  CHECKER BOT — DATA VALIDATOR")
        print(f"  {len(symbols)} symbols × {len(timeframes)} timeframes")
        print(f"{'='*60}")

        # Phase 1: Fast scan — last stored date for every symbol×TF
        print("\n  [1] Scanning stored dates...")
        last_dates: dict = {}   # (symbol, tf) → tz-aware datetime | None
        for symbol in symbols:
            for tf in timeframes:
                last_dates[(symbol, tf)] = self.store.get_last_stored_date(symbol, tf)
        no_data = sum(1 for v in last_dates.values() if v is None)
        print(f"      {len(last_dates)} combos scanned, {no_data} with no data on disk")

        # Phase 2: Cohort date per TF (date held by ≥90% of symbols)
        print("  [2] Computing cohort dates...")
        cohort_dates: dict = {}   # tf → date
        for tf in timeframes:
            dates = sorted(
                [last_dates[(s, tf)].date() for s in symbols
                 if last_dates.get((s, tf)) is not None],
                reverse=True
            )
            if not dates:
                continue
            idx = max(0, int(len(dates) * 0.10))   # 10th-percentile from top
            cohort_dates[tf] = dates[idx]

        for tf in timeframes:
            c    = cohort_dates.get(tf, "—")
            have = sum(1 for s in symbols if last_dates.get((s, tf)) is not None)
            print(f"      [{tf:>3}]  cohort={c}  ({have}/{len(symbols)} symbols have data)")

        # Phase 3: Identify stale combos
        stale: list = []    # (symbol, tf, last_datetime|None)
        for tf in timeframes:
            cohort = cohort_dates.get(tf)
            if cohort is None:
                continue
            for symbol in symbols:
                ld      = last_dates.get((symbol, tf))
                ld_date = ld.date() if ld else None
                if ld_date is None or ld_date < cohort:
                    stale.append((symbol, tf, ld))

        stale_by_tf = {}
        for _, tf, _ in stale:
            stale_by_tf[tf] = stale_by_tf.get(tf, 0) + 1

        if not stale:
            print("\n  ✅ All data current — nothing to fetch.")
            print(f"{'='*60}\n")
            return {"checked": len(last_dates), "stale": 0, "fixed": 0, "errors": 0}

        print(f"\n  [3] Stale by TF: " +
              "  ".join(f"{tf}={stale_by_tf.get(tf, 0)}" for tf in timeframes))
        print(f"      Total {len(stale)} combos — fetching...")

        # Phase 4: Force-fetch every stale combo
        fixed  = 0
        errors = 0

        for symbol, tf, last_date in stale:
            try:
                if last_date is None:
                    # No data at all — full history download
                    candles = self._fetch_full_history(symbol, tf)
                    if candles:
                        self.store.save_candles(symbol, tf, candles, append=False)
                        fixed += 1
                    else:
                        errors += 1
                else:
                    # Has data but is behind cohort — incremental fetch
                    new_candles = self._fetch_incremental(symbol, tf, last_date)
                    if new_candles:
                        self.store.save_candles(symbol, tf, new_candles, append=True)
                        self.store.deduplicate(symbol, tf)
                        fixed += 1
                    # 0 new candles = API has nothing newer (holiday / delisted) — not an error
                time.sleep(0.15)
            except Exception as e:
                errors += 1
                print(f"    ✗ {symbol} [{tf}]: {e}")

        print(f"\n  [4] Done — fixed={fixed}  errors={errors}  "
              f"no-new-data={len(stale) - fixed - errors}")
        print(f"{'='*60}\n")

        return {
            "checked": len(last_dates),
            "stale":   len(stale),
            "fixed":   fixed,
            "errors":  errors,
        }

    # ─── Get closes for RSI seeding ───────────────────────────────────────────
    def get_closes_for_rsi(self, symbol: str, timeframe: str,
                            rsi_period: int = 14) -> list:
        """
        Get close prices for RSI seeding.
        Reads from local storage — no API call needed if data exists.
        """
        closes = self.store.load_closes(symbol, timeframe)
        if len(closes) >= rsi_period + 1:
            return closes
        # Fallback: fetch directly if not enough stored data
        candles = self._fetch_full_history(symbol, timeframe)
        return [c["close"] for c in candles]

    # ─── Carry-forward check ──────────────────────────────────────────────────
    def get_last_rsi_candle(self, symbol: str, timeframe: str,
                             rsi_engine, n_candles: int = 50) -> dict | None:
        """
        Get the last N candles and return last RSI value and close price.
        Used by carry-forward logic at startup.
        """
        closes = self.store.load_closes(symbol, timeframe)
        if len(closes) < n_candles:
            return None

        # Seed a temporary RSI calculator with recent closes
        from core.rsi_engine import RSICalculator
        calc = RSICalculator(period=14)
        last_rsi = calc.seed_from_history(closes[-n_candles:])

        candles = self.store.load_candles(symbol, timeframe)
        if not candles:
            return None

        last_candle = candles[-1]
        return {
            "close":    last_candle["close"],
            "datetime": last_candle["datetime"],
            "rsi":      last_rsi
        }
