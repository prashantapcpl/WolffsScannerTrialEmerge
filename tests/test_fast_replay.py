"""
test_fast_replay.py — regression for ReplayEngine.fast_replay_from_cache.

Why
---
Scanner 4's run_carry_forward was the single slowest path on cold startup
(~12 min for ~250 symbols × 30 days of 15m candles). It re-parsed CSVs and
re-computed RSI via engine.update for every candle, even though rsi_cache
already had every (datetime, close, rsi) triple.

`fast_replay_from_cache` reads from the cache directly. This test verifies:
  • Callback fires for every signal-TF candle inside [from_dt, to_dt].
  • d_rsi / w_rsi delivered to the callback advance with the cached D/W
    timeline (point-in-time, not "today's value").
  • Candles outside the window are skipped.
  • Empty cache returns 0 candles without crashing.
"""
import os
import sys
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.replay_engine import ReplayEngine  # noqa: E402
from core.rsi_engine import RSIEngine  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


class FakeCache:
    """Stand-in for RSICache with just enough surface for fast_replay."""
    def __init__(self):
        self.data = {}  # (symbol, tf) → {"dts": [...], "closes": [...], "rsis": [...]}

    def add(self, symbol, tf, dts, closes, rsis):
        self.data[(symbol, tf)] = {"dts": dts, "closes": closes, "rsis": rsis}

    def get_datetimes(self, symbol, tf):
        return self.data.get((symbol, tf), {}).get("dts", [])

    def get_closes(self, symbol, tf):
        return self.data.get((symbol, tf), {}).get("closes", [])

    def get_rsi_series(self, symbol, tf):
        return self.data.get((symbol, tf), {}).get("rsis", [])


def run():
    print("\n  ReplayEngine.fast_replay_from_cache regression\n")

    cache  = FakeCache()
    symbol = "NSE:FOO-EQ"

    # ── seed three 15m candles on 2026-01-06 ─────────────────────────────
    sig_dts = [
        "2026-01-06 09:15:00",
        "2026-01-06 09:30:00",
        "2026-01-06 09:45:00",
    ]
    cache.add(symbol, "15", sig_dts, [100.0, 99.5, 101.0], [40.0, 35.0, 50.0])

    # Two daily bars (2026-01-05 close → 2026-01-06 close)
    cache.add(symbol, "D",
              ["2026-01-05 09:15:00", "2026-01-06 09:15:00"],
              [98.0, 101.5],
              [60.0, 65.5])

    # One weekly bar covering the week of 2026-01-05 (Mon)
    cache.add(symbol, "W",
              ["2026-01-05 09:15:00"],
              [101.5],
              [58.0])

    engine = RSIEngine(period=14)
    eng_replay = ReplayEngine(data_store=None, rsi_period=14)

    captured = []

    def cb(candle, tf, _eng, d_rsi, w_rsi):
        captured.append((tf, candle["datetime"], candle["close"], d_rsi, w_rsi))

    result = eng_replay.fast_replay_from_cache(
        symbol          = symbol,
        signal_tf       = "15",
        from_dt         = IST.localize(datetime(2026, 1, 6, 9, 15)),
        to_dt           = IST.localize(datetime(2026, 1, 6, 15, 30)),
        callback        = cb,
        rsi_cache       = cache,
        external_engine = engine,
    )

    assert result["candles"] == 3, f"FAIL: callback fired {result['candles']}× (want 3)"
    print(f"  ✅  callback fired for all 3 sig-TF candles in window")

    # The 09:30 candle CLOSES at 09:45 (open 09:30 + 15m). It comes AFTER
    # the D=2026-01-05 candle close (15:30 yesterday) and BEFORE the
    # D=2026-01-06 candle close (15:30 today). So d_rsi at that moment
    # must be 60.0 (yesterday's value), not 65.5 (today's).
    second = captured[1]
    assert second[3] == 60.0, (
        f"FAIL: d_rsi at 09:30 candle was {second[3]} (want 60.0 — yesterday's D)"
    )
    print(f"  ✅  point-in-time D-RSI honoured (got {second[3]}, not today's 65.5)")

    # ── window filter: limit to first two candles ────────────────────────
    captured.clear()
    eng2 = RSIEngine(period=14)
    eng_replay.fast_replay_from_cache(
        symbol          = symbol,
        signal_tf       = "15",
        from_dt         = IST.localize(datetime(2026, 1, 6, 9, 15)),
        to_dt           = IST.localize(datetime(2026, 1, 6, 9, 45)),
        callback        = cb,
        rsi_cache       = cache,
        external_engine = eng2,
    )
    assert len(captured) == 2, (
        f"FAIL: window filter let through {len(captured)} candles (want 2)"
    )
    print(f"  ✅  window filter respected (got 2 callbacks)")

    # ── empty cache returns 0 candles, no crash ─────────────────────────
    empty = FakeCache()
    r = eng_replay.fast_replay_from_cache(
        symbol          = "NSE:NOPE-EQ",
        signal_tf       = "15",
        from_dt         = IST.localize(datetime(2026, 1, 6, 9, 15)),
        to_dt           = None,
        callback        = lambda *a: None,
        rsi_cache       = empty,
        external_engine = RSIEngine(period=14),
    )
    assert r["candles"] == 0
    print(f"  ✅  empty cache returns 0 candles without crashing")

    print("\n  ✅ All fast_replay_from_cache regression checks passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(run())
