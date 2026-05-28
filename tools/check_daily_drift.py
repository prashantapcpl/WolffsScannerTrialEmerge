"""
check_daily_drift.py — startup drift sanity check.

For a sample of N stocks, compare YESTERDAY's daily close as derived from
the scanner's local 5m CSV (via DataStore.load_daily_from_5m) against
Fyers' authoritative daily candle (REST /history).

Output: one log line per sample + a summary. Run at startup (or any time)
so we can MEASURE drift between LTP-built bars and Fyers' truth instead
of guessing it's small.

Usage
-----
    python tools/check_daily_drift.py                    # auto-samples 10 stocks
    python tools/check_daily_drift.py --n 25
    python tools/check_daily_drift.py --threshold-pct 0.1
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta

import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

IST = pytz.timezone("Asia/Kolkata")

from core.data_store    import DataStore       # noqa: E402
from core.fyers_auth    import get_fyers_client  # noqa: E402
from core.symbol_map    import SymbolMapper    # noqa: E402


def previous_trading_day(now: datetime) -> datetime:
    """Walk back until we land on a weekday. Doesn't honour holidays
    (good enough for a sanity check; if it lands on a holiday Fyers
    returns no candle and we just skip that symbol)."""
    d = now - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fyers_daily(client, symbol: str, day: datetime) -> dict | None:
    data = {
        "symbol":     symbol, "resolution": "D", "date_format": "1",
        "range_from": day.strftime("%Y-%m-%d"),
        "range_to":   day.strftime("%Y-%m-%d"),
        "cont_flag":  "1",
    }
    try:
        resp = client.history(data=data)
        if resp.get("code") != 200 or not resp.get("candles"):
            return None
        c = resp["candles"][0]
        return {"close": float(c[4]), "open": float(c[1]),
                "high":  float(c[2]), "low":   float(c[3])}
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=10, help="sample size (default 10)")
    ap.add_argument("--threshold-pct", type=float, default=0.10,
                    help="flag any drift > this %% (default 0.10)")
    ap.add_argument("--seed", type=int, default=None,
                    help="random seed for reproducible sample")
    args = ap.parse_args()

    mapper  = SymbolMapper()
    symbols = mapper.get_all_fyers_symbols()
    if args.seed is not None:
        random.seed(args.seed)
    sample = random.sample(symbols, min(args.n, len(symbols)))

    target_day = previous_trading_day(datetime.now(IST))
    print(f"\n  DAILY-DRIFT CHECK — sample={len(sample)}  day={target_day.strftime('%Y-%m-%d')}\n")

    ds       = DataStore()
    client   = get_fyers_client()
    flagged  = []
    skipped  = 0

    for sym in sample:
        derived = ds.load_daily_from_5m(sym)
        derived_close = None
        for r in derived:
            if r["datetime"].date() == target_day.date():
                derived_close = r["close"]
                break
        if derived_close is None:
            print(f"  {sym:<28}  no derived daily — SKIP")
            skipped += 1
            continue
        fy = fyers_daily(client, sym, target_day)
        if fy is None:
            print(f"  {sym:<28}  no Fyers daily — SKIP")
            skipped += 1
            continue
        diff_pct = 100.0 * (derived_close - fy["close"]) / fy["close"]
        tag = "★" if abs(diff_pct) > args.threshold_pct else " "
        print(f"  {sym:<28}  derived={derived_close:>9.2f}  "
              f"fyers={fy['close']:>9.2f}  Δ={diff_pct:+.3f}% {tag}")
        if abs(diff_pct) > args.threshold_pct:
            flagged.append((sym, diff_pct))

    print("\n  ── Summary ──")
    print(f"  Checked: {len(sample) - skipped}    Skipped: {skipped}")
    print(f"  Flagged (|Δ| > {args.threshold_pct}%): {len(flagged)}")
    if flagged:
        for s, d in sorted(flagged, key=lambda x: -abs(x[1]))[:10]:
            print(f"    {s}: {d:+.3f}%")
        print("\n  → Drift detected. Tick-built candles disagree with Fyers' truth.")
        print(f"    Next step: run `python tools/diagnose_rsi.py "
              f"--symbol {flagged[0][0]} --date {target_day.strftime('%Y-%m-%d')} --tf 5`")
    else:
        print("\n  ✅ All sampled stocks within tolerance.")
    print()


if __name__ == "__main__":
    main()
