"""
diagnose_rsi.py — 3-way RSI parity diagnostic for ONE stock on ONE day.

Goal
----
Find the EXACT bar where the scanner's RSI starts disagreeing with what
Fyers' chart shows. Once that bar is known, the bug class is obvious:

  - Diverges immediately at 09:15  → seeding / warmup is wrong
  - Diverges on a specific candle  → that candle's OHLC differs
                                     from Fyers' authoritative bar
                                     (tick-built candle drift)
  - Aligned all day but RSI off    → daily aggregation / closing-auction
                                     handling

Compares per candle:
  A. Scanner RSI : recomputed from the local CSV in data/price_history/
                   (this is what your live engine actually sees).
  B. Fyers RSI   : recomputed from a fresh Fyers /history REST call
                   over the same window (broker-authoritative OHLC).
  C. Chart RSI   : you eyeball one timestamp on Fyers/TradingView chart
                   and pass it via --chart-rsi to sanity-check B.

Usage
-----
    python tools/diagnose_rsi.py --symbol NSE:AEQUS-EQ --date 2026-05-22 --tf 5
    python tools/diagnose_rsi.py --symbol NSE:AEQUS-EQ --date 2026-05-22 --tf 15 --chart-rsi 47.8 --chart-time 13:45

Outputs
-------
  - Prints side-by-side table to stdout
  - Saves CSV: data/diagnostics/diag_<symbol>_<date>_<tf>.csv
  - Prints summary: first divergent bar, OHLC diff at that bar, recommendation

Requires
--------
  - config.json with valid Fyers credentials (paid plan helps for rate)
  - data/access_token.json from a recent login (LOGIN.bat)
  - Existing data/price_history/<sym>_<tf>m.csv (scanner's stored candles)

Safe to run during market hours; uses ONE Fyers history call per run.
"""

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timedelta

import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

IST = pytz.timezone("Asia/Kolkata")

from core.rsi_engine import RSICalculator   # noqa: E402
from core.data_store import DataStore       # noqa: E402
from core.fyers_auth import get_fyers_client  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", required=True,
                   help="Fyers symbol, e.g. NSE:AEQUS-EQ")
    p.add_argument("--date", required=True,
                   help="Target trading date YYYY-MM-DD")
    p.add_argument("--tf", default="5",
                   choices=["1", "2", "5", "15", "30", "60", "D", "W"],
                   help="Timeframe (default: 5)")
    p.add_argument("--warmup-days", type=int, default=60,
                   help="History days before --date used to seed RSI (default: 60). "
                        "Need >= ~30 for stable Wilder smoothing on 5m.")
    p.add_argument("--chart-rsi", type=float, default=None,
                   help="Optional: RSI value you see on Fyers/TradingView chart")
    p.add_argument("--chart-time", default=None,
                   help="Optional: HH:MM IST timestamp the --chart-rsi was read at")
    p.add_argument("--tolerance", type=float, default=0.10,
                   help="RSI diff tolerance to flag divergence (default: 0.10)")
    p.add_argument("--no-fyers", action="store_true",
                   help="Skip Fyers REST fetch (only show scanner column)")
    return p.parse_args()


def fetch_fyers_candles(client, symbol: str, tf: str,
                        start: datetime, end: datetime) -> list:
    """Fetch authoritative OHLC from Fyers /history REST endpoint."""
    data = {
        "symbol":      symbol,
        "resolution":  tf,
        "date_format": "1",
        "range_from":  start.strftime("%Y-%m-%d"),
        "range_to":    end.strftime("%Y-%m-%d"),
        "cont_flag":   "1",
    }
    resp = client.history(data=data)
    if resp.get("code") != 200 or "candles" not in resp:
        raise RuntimeError(f"Fyers /history failed: {resp}")
    out = []
    for c in resp["candles"]:
        ts = datetime.fromtimestamp(c[0], tz=IST)
        out.append({
            "datetime": ts,
            "open":  float(c[1]),
            "high":  float(c[2]),
            "low":   float(c[3]),
            "close": float(c[4]),
            "volume": int(c[5]),
        })
    return out


def compute_rsi_series(candles: list, period: int = 14) -> list:
    """Run Wilder-RSI over candles, return list of (dt, close, rsi)."""
    calc = RSICalculator(period=period)
    out = []
    for c in candles:
        rsi = calc.update(c["close"])
        out.append((c["datetime"], c["close"], rsi))
    return out


def filter_to_day(rows: list, day: datetime) -> list:
    return [r for r in rows if r[0].date() == day.date()]


def find_candle(candles: list, ts) -> dict | None:
    for c in candles:
        if c["datetime"] == ts:
            return c
    return None


# ─────────────────────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    target_day = IST.localize(datetime.strptime(args.date, "%Y-%m-%d"))
    start_day  = target_day - timedelta(days=args.warmup_days)
    end_day    = target_day + timedelta(days=1)

    print(f"\n{'='*72}")
    print(f"  RSI DIAGNOSTIC  —  {args.symbol}  {args.date}  TF={args.tf}m")
    print(f"  Warmup: {args.warmup_days}d  |  Tolerance: ±{args.tolerance}")
    print(f"{'='*72}\n")

    # ─── A. Scanner side: load from local CSV, recompute RSI ────────────────
    ds = DataStore()
    scanner_candles_all = ds.load_candles(args.symbol, args.tf,
                                          from_date=start_day,
                                          to_date=end_day)
    if not scanner_candles_all:
        print(f"❌ No local CSV for {args.symbol} TF={args.tf}.")
        print(f"   Expected at: data/price_history/{args.symbol.replace(':','_')}_{args.tf}m.csv")
        sys.exit(2)
    scanner_series = compute_rsi_series(scanner_candles_all)
    scanner_day    = filter_to_day(scanner_series, target_day)
    print(f"  [A] Scanner CSV: {len(scanner_candles_all)} candles loaded "
          f"({len(scanner_day)} on {args.date}).")

    # ─── B. Fyers side: fresh REST fetch, recompute RSI ─────────────────────
    fyers_day = []
    fyers_candles_all = []
    if not args.no_fyers:
        try:
            client = get_fyers_client()
            fyers_candles_all = fetch_fyers_candles(
                client, args.symbol, args.tf, start_day, end_day)
            fyers_series = compute_rsi_series(fyers_candles_all)
            fyers_day    = filter_to_day(fyers_series, target_day)
            print(f"  [B] Fyers REST:  {len(fyers_candles_all)} candles fetched "
                  f"({len(fyers_day)} on {args.date}).")
        except Exception as e:
            print(f"  ⚠️  Fyers fetch failed ({e}); only [A] will be shown.")
            fyers_day = []

    # ─── Side-by-side table ─────────────────────────────────────────────────
    print(f"\n{'Time':<8} {'A close':>10} {'A RSI':>8} | "
          f"{'B close':>10} {'B RSI':>8} | {'Δclose':>8} {'ΔRSI':>8}  Flag")
    print("-" * 80)

    # Build a lookup of fyers bars by timestamp for fast align
    fyers_by_ts = {r[0]: r for r in fyers_day}

    first_div_ts = None
    diff_rows = []
    for ts, a_close, a_rsi in scanner_day:
        b = fyers_by_ts.get(ts)
        if b:
            _, b_close, b_rsi = b
        else:
            b_close = b_rsi = None

        # Format and flag
        d_close = (a_close - b_close) if (b_close is not None) else None
        d_rsi   = ((a_rsi or 0) - (b_rsi or 0)) if (a_rsi and b_rsi) else None

        flag = ""
        if d_rsi is not None and abs(d_rsi) > args.tolerance:
            flag = "★ DIVERGENT"
            if first_div_ts is None:
                first_div_ts = ts

        a_rsi_s = f"{a_rsi:>8.2f}" if a_rsi is not None else f"{'-':>8}"
        b_rsi_s = f"{b_rsi:>8.2f}" if b_rsi is not None else f"{'-':>8}"
        b_close_s = f"{b_close:>10.2f}" if b_close is not None else f"{'-':>10}"
        d_close_s = f"{d_close:>+8.2f}" if d_close is not None else f"{'-':>8}"
        d_rsi_s   = f"{d_rsi:>+8.2f}"   if d_rsi   is not None else f"{'-':>8}"

        print(f"{ts.strftime('%H:%M'):<8} {a_close:>10.2f} {a_rsi_s} | "
              f"{b_close_s} {b_rsi_s} | {d_close_s} {d_rsi_s}  {flag}")
        diff_rows.append({
            "time": ts.strftime("%Y-%m-%d %H:%M"),
            "scanner_close": a_close,
            "scanner_rsi":   a_rsi,
            "fyers_close":   b_close,
            "fyers_rsi":     b_rsi,
            "d_close":       d_close,
            "d_rsi":         d_rsi,
            "divergent":     bool(flag),
        })

    # ─── Save CSV ───────────────────────────────────────────────────────────
    out_dir = os.path.join(ROOT, "data", "diagnostics")
    os.makedirs(out_dir, exist_ok=True)
    safe_sym = args.symbol.replace(":", "_")
    out_path = os.path.join(out_dir, f"diag_{safe_sym}_{args.date}_{args.tf}.csv")
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(diff_rows[0].keys()) if diff_rows else ["time"])
        w.writeheader()
        for r in diff_rows:
            w.writerow(r)
    print(f"\n  📄 Saved: {out_path}")

    # ─── Verdict ────────────────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    if not fyers_day:
        print("  Verdict: Fyers data unavailable. Only column A shown.")
    elif first_div_ts is None:
        print(f"  ✅ Verdict: NO divergence beyond ±{args.tolerance} RSI all day.")
        print("     Scanner CSV agrees with Fyers REST → RSI pipeline is clean.")
        print("     If your chart still differs, the issue is downstream "
              "(rule/threshold/webhook).")
    else:
        a_c = next(c for c in scanner_candles_all if c["datetime"] == first_div_ts)
        b_c = find_candle(fyers_candles_all, first_div_ts)
        print(f"  ❌ Verdict: First divergence at {first_div_ts.strftime('%H:%M')}.")
        print(f"     Scanner candle: O={a_c['open']:.2f}  H={a_c['high']:.2f}  "
              f"L={a_c['low']:.2f}  C={a_c['close']:.2f}  V={a_c['volume']}")
        if b_c:
            print(f"     Fyers candle:   O={b_c['open']:.2f}  H={b_c['high']:.2f}  "
                  f"L={b_c['low']:.2f}  C={b_c['close']:.2f}  V={b_c['volume']}")
            ohlc_match = (round(a_c['open'], 2) == round(b_c['open'], 2) and
                          round(a_c['close'], 2) == round(b_c['close'], 2))
            if ohlc_match:
                print("\n     → OHLC matches but RSI differs.")
                print("       Cause is upstream: warmup/seed window or a "
                      "poisoned earlier bar before today.")
                print(f"       Fix: rebuild RSI cache from longer history "
                      f"(--warmup-days {args.warmup_days * 2}).")
            else:
                print("\n     → OHLC ITSELF DIFFERS.")
                print("       Cause is the candle pipeline (tick-built bar wrong).")
                print("       Most likely: TickSanityValidator dropped a tick OR "
                      "a websocket gap was not gap-filled.")
                print("       Fix: set TICK_SANITY_JUMP_CHECK_ENABLED=false "
                      "and re-run a session; or run HistoryManager gap-fill "
                      "for this symbol/day.")
        else:
            print("     Fyers REST returned NO candle at this timestamp.")
            print("     → Your CSV has a candle Fyers doesn't acknowledge "
                  "(phantom row). Run a data-integrity sweep.")

    # ─── Chart sanity (column C) ────────────────────────────────────────────
    if args.chart_rsi is not None and args.chart_time:
        try:
            ct = IST.localize(datetime.strptime(
                f"{args.date} {args.chart_time}", "%Y-%m-%d %H:%M"))
        except Exception:
            print(f"\n  ⚠️  Could not parse --chart-time '{args.chart_time}'.")
            ct = None
        if ct is not None:
            a_row = next((r for r in scanner_day if r[0] == ct), None)
            b_row = fyers_by_ts.get(ct)
            print(f"\n  [C] Chart RSI at {args.chart_time}: {args.chart_rsi}")
            if a_row: 
                print(f"      A scanner RSI: {a_row[2]:.2f}")
            if b_row: 
                print(f"      B fyers RSI:   {b_row[2]:.2f}")
            print("      → If C matches B and A is off, the scanner CSV is the problem.")
            print("        If C matches A and B is off, your warmup is wrong.")
            print("        If C matches neither, your chart settings differ "
                  "(Wilder vs EMA, period != 14, etc).")

    print(f"{'─'*72}\n")


if __name__ == "__main__":
    main()
