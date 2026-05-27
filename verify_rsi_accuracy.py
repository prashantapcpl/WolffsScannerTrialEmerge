"""
verify_rsi_accuracy.py
Mirrors the live RSI cache build logic exactly. RSI values printed here
are byte-identical to what the live scanner uses internally.

Comparison target: Fyers chart RSI(14). Acceptable diff: <= 0.5.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.data_store import DataStore
from core.rsi_engine import RSICalculator


SAMPLES = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ"]
PERIOD  = 14
TAIL    = 15


def _load_candles_for_tf(ds: DataStore, sym: str, tf: str) -> list:
    """Per-TF source selection for best Fyers-chart parity."""
    if tf == "D":
        c = ds.load_daily_from_5m(sym)
        if not c:
            c = ds.load_candles(sym, "D")
        return c
    if tf == "W":
        # Build weekly from daily CSV (3 years, closing-auction prices —
        # matches how Fyers chart constructs weekly RSI).
        daily = ds.load_candles(sym, "D")
        return ds.load_weekly_from_daily(daily) if daily else []
    return ds.load_candles(sym, tf)


def _clean(candles: list, tf: str) -> list:
    """Same dedupe + market-hours filter as rsi_cache.build()."""
    seen = set()
    out  = []
    for c in candles:
        key = c["datetime"]
        if key in seen:
            continue
        seen.add(key)
        if tf not in ("D", "W"):
            h, m = key.hour, key.minute
            if not ((h == 9 and m >= 15) or
                    (10 <= h <= 14) or
                    (h == 15 and m < 30)):
                continue
        out.append(c)
    return out


def rsi_series(closes: list) -> list:
    calc = RSICalculator(period=PERIOD)
    return [calc.update(c) for c in closes]


def fmt(v):
    return f"{v:.2f}" if v is not None else "    -"


def show(ds: DataStore, sym: str, tf: str, label: str):
    candles = _clean(_load_candles_for_tf(ds, sym, tf), tf)
    if len(candles) < PERIOD + 2:
        print(f"\n  --- {label} --- (insufficient: {len(candles)} bars)")
        return
    closes = [c["close"] for c in candles]
    rsi    = rsi_series(closes)
    print(f"\n  --- {label}  (total bars: {len(candles)}, showing last {TAIL}) ---")
    if tf in ("D", "W"):
        date_fmt = "%Y-%m-%d"
    else:
        date_fmt = "%Y-%m-%d %H:%M"
    print(f"  {'datetime':<20} {'close':>10} {'RSI':>8}")
    for c, r in list(zip(candles, rsi))[-TAIL:]:
        ts = c["datetime"].strftime(date_fmt)
        print(f"  {ts:<20} {c['close']:>10.2f} {fmt(r):>8}")


def main():
    ds = DataStore()
    for sym in SAMPLES:
        print("=" * 70)
        print(f"SYMBOL: {sym}")
        print("=" * 70)
        show(ds, sym, "5",  "5m")
        show(ds, sym, "15", "15m")
        show(ds, sym, "60", "60m (1h)")
        show(ds, sym, "D",  "Daily (from 5m aggregation)")
        show(ds, sym, "W",  "Weekly (from W CSV — 3 years)")
        print()


if __name__ == "__main__":
    main()
