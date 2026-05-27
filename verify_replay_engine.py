"""
verify_replay_engine.py
Sanity-test the new ReplayEngine in isolation.

Two checks:
  1. Final 5m / 15m / 60m / D / W RSI after replay matches the direct RSI
     computation from verify_rsi_accuracy.py (they should be identical).
  2. D/W RSI values reported during intraday candle callback events advance
     only at day/week boundaries (not on every candle).
"""

import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict
import pytz

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.data_store     import DataStore
from core.rsi_engine     import RSICalculator
from core.replay_engine  import ReplayEngine

IST = pytz.timezone("Asia/Kolkata")


SAMPLES = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ"]


# ─── Direct (independent) RSI compute for comparison ─────────────────────
def _direct_load(ds, sym, tf):
    if tf == "D":
        c = ds.load_daily_from_5m(sym)
        if not c:
            c = ds.load_candles(sym, "D")
        return c
    if tf == "W":
        daily = ds.load_candles(sym, "D")
        return ds.load_weekly_from_daily(daily) if daily else []
    raw = ds.load_candles(sym, tf)
    seen, clean = set(), []
    for c in raw:
        if c["datetime"] in seen:
            continue
        seen.add(c["datetime"])
        h, m = c["datetime"].hour, c["datetime"].minute
        if not ((h == 9 and m >= 15) or (10 <= h <= 14) or (h == 15 and m < 30)):
            continue
        clean.append(c)
    return clean


def direct_final_rsi(ds, sym, tf, as_of: datetime = None):
    """Compute final RSI for (sym, tf) directly from CSV - reference truth.
    If `as_of` is given, only includes candles whose CLOSE time is <= as_of
    (i.e., bars that would have been fully closed at that moment)."""
    candles = _direct_load(ds, sym, tf)
    if as_of is not None:
        if tf == "D":
            candles = [c for c in candles
                       if c["datetime"].replace(hour=15, minute=30) <= as_of]
        elif tf == "W":
            def _week_close(c):
                d = c["datetime"]
                days_to_fri = (4 - d.weekday()) % 7
                return (d + timedelta(days=days_to_fri)).replace(
                    hour=15, minute=30, second=0, microsecond=0)
            candles = [c for c in candles if _week_close(c) <= as_of]
        else:
            tf_min = {"5":5,"10":10,"15":15,"30":30,"60":60}.get(tf, 1)
            candles = [c for c in candles
                       if c["datetime"] + timedelta(minutes=tf_min) <= as_of]
    if len(candles) < 16:
        return None
    calc = RSICalculator(period=14)
    for c in candles:
        calc.update(c["close"])
    return calc.get_rsi()


# ─── Replay verification ─────────────────────────────────────────────────
def main():
    ds = DataStore()
    eng = ReplayEngine(ds)

    # Replay the last 60 days for each symbol
    to_dt   = datetime.now(IST)
    from_dt = to_dt - timedelta(days=60)

    for sym in SAMPLES:
        print("=" * 70)
        print(f"SYMBOL: {sym}")
        print("=" * 70)

        # Track per-day D-RSI changes observed during callback
        d_rsi_per_day  = defaultdict(set)
        w_rsi_per_week = defaultdict(set)
        first_5m_d_rsi = None
        last_5m_d_rsi  = None

        def cb(candle, tf, rsi_engine, d_rsi, w_rsi):
            nonlocal first_5m_d_rsi, last_5m_d_rsi
            if tf == "5":
                if first_5m_d_rsi is None and d_rsi is not None:
                    first_5m_d_rsi = (candle["datetime"], d_rsi)
                if d_rsi is not None:
                    last_5m_d_rsi = (candle["datetime"], d_rsi)
                d_rsi_per_day[candle["datetime"].date()].add(d_rsi)
                if w_rsi is not None:
                    # Group by ISO week of the candle's open time PLUS a
                    # half-week flag (pre-Fri-15:30 vs post). W-RSI advances
                    # at Fri 15:30, so it's expected to differ between halves
                    # within the same ISO week.
                    iso = candle["datetime"].isocalendar()[:2]
                    fri_close = candle["datetime"] + timedelta(
                        days=((4 - candle["datetime"].weekday()) % 7))
                    fri_close = fri_close.replace(hour=15, minute=30)
                    half = "pre" if candle["datetime"] < fri_close else "post"
                    w_rsi_per_week[(iso, half)].add(w_rsi)

        result = eng.replay(
            symbol  = sym,
            tfs     = ["5", "15", "60"],
            from_dt = from_dt,
            to_dt   = to_dt,
            callback = cb,
            seed_lookback_days = 60,
        )

        engine = result["engine"]
        replay_5m  = engine.get_rsi(sym, "5")
        replay_15m = engine.get_rsi(sym, "15")
        replay_60m = engine.get_rsi(sym, "60")
        replay_d   = engine.get_rsi(sym, "D")
        replay_w   = engine.get_rsi(sym, "W")

        direct_5m  = direct_final_rsi(ds, sym, "5",  as_of=to_dt)
        direct_15m = direct_final_rsi(ds, sym, "15", as_of=to_dt)
        direct_60m = direct_final_rsi(ds, sym, "60", as_of=to_dt)
        direct_d   = direct_final_rsi(ds, sym, "D",  as_of=to_dt)
        direct_w   = direct_final_rsi(ds, sym, "W",  as_of=to_dt)

        print(f"\nFinal RSI comparison (replay vs direct):")
        print(f"  TF     replay     direct     match")
        for label, r, d in [("5m", replay_5m, direct_5m),
                            ("15m", replay_15m, direct_15m),
                            ("60m", replay_60m, direct_60m),
                            ("D",   replay_d,   direct_d),
                            ("W",   replay_w,   direct_w)]:
            r_str = f"{r:.4f}" if r is not None else "  -"
            d_str = f"{d:.4f}" if d is not None else "  -"
            match = "OK" if r is not None and d is not None and abs(r - d) < 0.01 else "MISMATCH"
            print(f"  {label:<5}  {r_str:>9}  {d_str:>9}    {match}")

        # Verify D-RSI is constant within a single day
        days_with_multiple_d_rsi = {d: v for d, v in d_rsi_per_day.items() if len(v) > 1}
        if days_with_multiple_d_rsi:
            print(f"\n  [FAIL] D-RSI changed mid-day on {len(days_with_multiple_d_rsi)} days")
            for d in list(days_with_multiple_d_rsi.keys())[:3]:
                print(f"     {d}: {sorted(days_with_multiple_d_rsi[d])}")
        else:
            print(f"\n  [ok] D-RSI constant within each day "
                  f"({len(d_rsi_per_day)} days observed)")

        # Verify W-RSI is constant within each ISO-week half (pre/post Fri 15:30).
        # Allow ONE bucket with two values (first weekly close boundary at start
        # of seed window can briefly straddle None -> value transition).
        weeks_with_multiple_w_rsi = {w: v for w, v in w_rsi_per_week.items() if len(v) > 1}
        if len(weeks_with_multiple_w_rsi) > 1:
            print(f"  [FAIL] W-RSI changed within ISO-week-half on "
                  f"{len(weeks_with_multiple_w_rsi)} buckets")
            for k in list(weeks_with_multiple_w_rsi.keys())[:3]:
                print(f"     {k}: {sorted(weeks_with_multiple_w_rsi[k])}")
        else:
            print(f"  [ok] W-RSI constant within each ISO-week-half "
                  f"({len(w_rsi_per_week)} buckets observed)")

        if first_5m_d_rsi and last_5m_d_rsi:
            print(f"\n  D-RSI at first 5m callback: {first_5m_d_rsi[1]:.2f} @ "
                  f"{first_5m_d_rsi[0].strftime('%Y-%m-%d %H:%M')}")
            print(f"  D-RSI at last 5m callback : {last_5m_d_rsi[1]:.2f} @ "
                  f"{last_5m_d_rsi[0].strftime('%Y-%m-%d %H:%M')}")

        print(f"\n  Total callback invocations: {result['candles']}")
        print()


if __name__ == "__main__":
    main()
