"""
replay_engine.py
Generic chronological candle replay with point-in-time D/W RSI tracking.

Purpose:
  Walks historical candles for a symbol across multiple timeframes in
  chronological close-time order, advancing each timeframe's RSI as the
  walk progresses. Crucially, Daily and Weekly RSI are advanced at the
  exact moment they would have advanced live (end of trading day / week),
  so intraday candles are always evaluated against the D/W RSI that was
  in force at that moment historically -- not against today's stale value.

Used by:
  - Scanner state restoration at startup (full N-day lookback)
  - Settings-change rescan (recompute state under new thresholds)
  - A/B testing different strategy parameters

Strategy-agnostic: the caller passes a callback that receives each candle
close + the live RSIEngine + the current point-in-time D-RSI and W-RSI.
"""

import os
import sys
from datetime import datetime, timedelta
from typing import Callable, Optional, Iterable
import pytz

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

from core.rsi_engine import RSIEngine, RSICalculator

IST = pytz.timezone("Asia/Kolkata")

# Minutes per timeframe -- mirrors CandleEngine
_TF_MIN = {"1": 1, "2": 2, "5": 5, "10": 10, "15": 15,
           "30": 30, "60": 60, "D": 375, "W": 1875}


def _tf_close_time(open_dt: datetime, tf: str) -> datetime:
    """Return the close time of a candle given its open time and timeframe.
    Intraday TFs add tf minutes. Daily candles close at 15:30 same day.
    Weekly candles close at 15:30 the Friday of their open week."""
    if tf == "D":
        return open_dt.replace(hour=15, minute=30, second=0, microsecond=0)
    if tf == "W":
        # Open is Mon 09:15 by Fyers convention; close at Fri 15:30
        # weekday(): Mon=0, Fri=4
        days_to_fri = 4 - open_dt.weekday()
        if days_to_fri < 0:
            days_to_fri += 7
        fri = open_dt + timedelta(days=days_to_fri)
        return fri.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_dt + timedelta(minutes=_TF_MIN.get(tf, 1))


def _tf_sort_key(tf: str) -> int:
    """Sort timeframes so smaller intraday closes are processed first when
    multiple TFs close at the same moment. D and W process LAST so their
    RSI advancement happens after all intraday at that boundary."""
    if tf == "D":
        return 10_000
    if tf == "W":
        return 100_000
    return _TF_MIN.get(tf, 1)


# Per-candle callback signature:
#   fn(candle: dict, timeframe: str, rsi_engine: RSIEngine,
#      d_rsi: Optional[float], w_rsi: Optional[float]) -> None
CandleCallback = Callable[[dict, str, RSIEngine, Optional[float], Optional[float]], None]


class ReplayEngine:
    """
    Multi-TF chronological replay with point-in-time D/W RSI.

    Typical usage:

        eng = ReplayEngine(data_store)
        result = eng.replay(
            symbol     = "NSE:RELIANCE-EQ",
            tfs        = ["5", "10", "60"],   # intraday TFs to feed
            from_dt    = datetime(2026, 4, 24, 9, 15, tzinfo=IST),
            to_dt      = datetime(2026, 5, 22, 15, 30, tzinfo=IST),
            callback   = my_on_candle_close,
        )
    """

    def __init__(self, data_store, rsi_period: int = 14):
        self.data_store = data_store
        self.rsi_period = rsi_period

    # ─── Public API ─────────────────────────────────────────────────────
    def replay(self,
               symbol: str,
               tfs: Iterable[str],
               from_dt: datetime,
               to_dt: Optional[datetime] = None,
               callback: Optional[CandleCallback] = None,
               seed_lookback_days: int = 60,
               external_engine: Optional[RSIEngine] = None) -> dict:
        """
        Walk all candles for `symbol` across `tfs` in close-time order.

        Before reaching from_dt the RSIEngine is seeded by replaying
        `seed_lookback_days` of pre-window candles so RSI is properly
        warmed up when the visible replay window begins. Callback is NOT
        invoked during seeding -- only during [from_dt, to_dt].

        Args:
          symbol: NSE symbol, e.g. "NSE:RELIANCE-EQ"
          tfs:    intraday timeframes to feed the callback (e.g., ["5","10"]).
                  D and W are ALWAYS tracked internally for D/W RSI; you
                  don't need to include them unless your callback wants the
                  D or W candle events themselves.
          from_dt: start of visible replay window (inclusive)
          to_dt:   end of visible replay window (inclusive). Defaults to now.
          callback: invoked once per candle close inside the window.
          seed_lookback_days: how far back to read history for RSI seeding.
          external_engine: if provided, all RSI updates are written to this
                  engine instead of an internal one. Caller must reset/seed
                  it appropriately before calling. Use this when the strategy
                  already holds the engine you want advanced (so its
                  on_candle_close sees the same state during replay).

        Returns:
          dict {
            "engine":     RSIEngine populated with final state,
            "candles":    int  # candles passed to callback,
            "d_rsi_end":  float or None,
            "w_rsi_end":  float or None,
          }
        """
        if to_dt is None:
            to_dt = datetime.now(IST)
        if from_dt.tzinfo is None:
            from_dt = IST.localize(from_dt)
        if to_dt.tzinfo is None:
            to_dt = IST.localize(to_dt)

        # Build a single chronological timeline across all required TFs.
        # We always include D and W internally so D/W RSI advances correctly.
        intraday_tfs = [tf for tf in tfs if tf not in ("D", "W")]
        all_tfs      = list(set(intraday_tfs) | {"D", "W"})

        # Seed-vs-walk window selection:
        # - If caller provided external_engine, they've already seeded it for
        #   the pre-window history; we only walk candles inside [from_dt, to_dt].
        # - Otherwise, build a fresh engine: walk D/W from ALL available history
        #   (so Wilder converges) and intraday from `from_dt - seed_lookback_days`.
        if external_engine is not None:
            timeline_start = from_dt
        else:
            timeline_start = None  # see per-TF logic below

        intraday_seed_start = from_dt - timedelta(days=seed_lookback_days)

        # Build candle stream per TF
        timeline: list = []
        for tf in all_tfs:
            candles = self._load_candles_for_tf(symbol, tf)
            if external_engine is not None:
                truncate_start = timeline_start
            else:
                truncate_start = None if tf in ("D", "W") else intraday_seed_start
            for c in candles:
                ct = _tf_close_time(c["datetime"], tf)
                if truncate_start is not None and ct < truncate_start:
                    continue
                if ct > to_dt:
                    continue
                timeline.append((ct, _tf_sort_key(tf), tf, c))
        timeline.sort()

        engine = external_engine if external_engine is not None \
                 else RSIEngine(period=self.rsi_period)
        # If using external engine, initialize d_rsi/w_rsi from its current
        # state so the first intraday candle in the window has correct D/W.
        if external_engine is not None:
            d_rsi = engine.get_rsi(symbol, "D")
            w_rsi = engine.get_rsi(symbol, "W")
        else:
            d_rsi = None
            w_rsi = None
        seen   = 0  # callback invocations

        # Which TFs the caller wants to RECEIVE in the callback.
        callback_tfs = set(intraday_tfs) | (
            {"D"} if "D" in tfs else set()) | (
            {"W"} if "W" in tfs else set())

        for ct, _key, tf, candle in timeline:
            close = candle["close"]

            # Advance the engine's RSI for this TF
            engine.update(symbol, tf, close)

            # Track point-in-time D and W RSI
            if tf == "D":
                latest = engine.get_rsi(symbol, "D")
                if latest is not None:
                    d_rsi = latest
            elif tf == "W":
                latest = engine.get_rsi(symbol, "W")
                if latest is not None:
                    w_rsi = latest

            # Only invoke the callback inside the visible window AND for
            # timeframes the caller asked for.
            if (callback is not None
                    and tf in callback_tfs
                    and ct >= from_dt):
                callback(candle, tf, engine, d_rsi, w_rsi)
                seen += 1

        return {
            "engine":    engine,
            "candles":   seen,
            "d_rsi_end": d_rsi,
            "w_rsi_end": w_rsi,
        }

    # ─── Internals ──────────────────────────────────────────────────────
    def _load_candles_for_tf(self, symbol: str, tf: str) -> list:
        """Per-TF source selection: daily-from-5m, weekly-from-daily-CSV,
        intraday directly. Mirrors verified accuracy path."""
        if tf == "D":
            c = self.data_store.load_daily_from_5m(symbol)
            if not c:
                c = self.data_store.load_candles(symbol, "D")
            return c
        if tf == "W":
            # Build weekly from daily CSV (3 years) -- matches Fyers chart
            # weekly RSI methodology (settlement prices).
            daily = self.data_store.load_candles(symbol, "D")
            return self.data_store.load_weekly_from_daily(daily) if daily else []

        # Intraday: dedupe + market-hours filter (matches rsi_cache logic)
        raw = self.data_store.load_candles(symbol, tf)
        seen_dt = set()
        clean   = []
        for c in raw:
            key = c["datetime"]
            if key in seen_dt:
                continue
            seen_dt.add(key)
            h, m = key.hour, key.minute
            if not ((h == 9 and m >= 15) or
                    (10 <= h <= 14) or
                    (h == 15 and m < 30)):
                continue
            clean.append(c)
        return clean
