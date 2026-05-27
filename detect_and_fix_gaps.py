"""
detect_and_fix_gaps.py
Scan every intraday CSV for trading days with INCOMPLETE row counts.
On --apply: for every (symbol, tf) with ANY gap, force-fetch a single
30-day range from Fyers and merge into CSV (dedup by datetime).

Batching strategy: one fetch per (symbol, tf) -- not per day. That drops
the API call count from ~2700 to ~1500 unique combos.

Rate-limit: 0.25s spacing, plus exponential backoff on HTTP 429.
"""
import os, sys, csv, time
from datetime import datetime, timedelta, date
from collections import defaultdict
import pytz

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

IST       = pytz.timezone("Asia/Kolkata")
DATA_DIR  = os.path.join(ROOT, "data", "price_history")
APPLY     = "--apply" in sys.argv
LOOKBACK_DAYS = 30
SLEEP_OK  = 0.25      # spacing between successful calls
SLEEP_429 = 2.0       # initial backoff on rate-limit

EXPECTED  = {"5": 75, "10": 38, "15": 25, "30": 13, "60": 7}
THRESHOLD = 0.93


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5


def scan_file(path: str, since: date) -> dict:
    by_day = defaultdict(int)
    if not os.path.exists(path):
        return by_day
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            d = dt.date()
            if d >= since and is_trading_day(d):
                by_day[d] += 1
    return by_day


def main():
    today = datetime.now(IST).date()
    since = today - timedelta(days=LOOKBACK_DAYS)

    print(f"Scanning intraday CSVs for {since} -> {today}")
    print(f"Threshold: rows >= EXPECTED * {THRESHOLD}\n")

    # gap_combos[(symbol, tf)] = set of dates with gaps
    gap_combos = defaultdict(set)
    files_scanned = 0

    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith(".csv") or ".bak." in fname:
            continue
        if   "_5m.csv"  in fname: tf = "5"
        elif "_10m.csv" in fname: tf = "10"
        elif "_15m.csv" in fname: tf = "15"
        elif "_30m.csv" in fname: tf = "30"
        elif "_60m.csv" in fname: tf = "60"
        else: continue

        stem   = fname.replace(f"_{tf}m.csv", "")
        symbol = stem.replace("_", ":", 1)
        path   = os.path.join(DATA_DIR, fname)
        files_scanned += 1

        by_day    = scan_file(path, since)
        expected  = EXPECTED[tf]
        threshold = int(expected * THRESHOLD)
        for d, n in by_day.items():
            if n < threshold:
                gap_combos[(symbol, tf)].add(d)

    total_gap_days = sum(len(v) for v in gap_combos.values())
    print(f"Files scanned          : {files_scanned}")
    print(f"Unique (symbol,tf) gaps: {len(gap_combos)}")
    print(f"Total gap stock-days   : {total_gap_days}\n")

    # Per-tf summary
    by_tf_count = defaultdict(int)
    for (s, tf), days in gap_combos.items():
        by_tf_count[tf] += 1
    print("Unique (symbol, tf) combos by TF:")
    for tf in sorted(by_tf_count):
        print(f"  TF {tf:>3}: {by_tf_count[tf]}")

    if not APPLY:
        print(f"\n*** DRY RUN. Pass --apply to fetch (estimated "
              f"{len(gap_combos)} API calls @ {SLEEP_OK}s = "
              f"{int(len(gap_combos) * SLEEP_OK / 60)} min wall clock).")
        return

    # --- Apply ---
    print(f"\nFetching {len(gap_combos)} unique (symbol, tf) combos...")
    print(f"Each fetch covers full {LOOKBACK_DAYS}-day range.\n")

    from core.fyers_auth    import get_fyers_client
    from core.history_manager import HistoryManager
    fyers = get_fyers_client()
    if not fyers:
        print("!! No Fyers client (token expired?). Cannot proceed.")
        return
    hm = HistoryManager(fyers)

    range_start = IST.localize(datetime(since.year, since.month, since.day, 9, 15))
    range_end   = IST.localize(datetime(today.year, today.month, today.day, 15, 30))

    fixed         = 0
    errors        = 0
    rate_limited  = 0
    combos        = sorted(gap_combos.keys())

    for i, (symbol, tf) in enumerate(combos):
        try:
            candles = None
            for attempt in range(3):
                try:
                    candles = hm._fetch_range(symbol, tf, range_start, range_end)
                    break
                except Exception as e:
                    msg = str(e).lower()
                    if "429" in msg or "rate" in msg or "limit" in msg:
                        rate_limited += 1
                        backoff = SLEEP_429 * (2 ** attempt)
                        print(f"   rate-limited, sleeping {backoff:.1f}s...")
                        time.sleep(backoff)
                    else:
                        raise

            if candles:
                hm.store.save_candles_merged(symbol, tf, candles)
                fixed += 1
            time.sleep(SLEEP_OK)
        except Exception as e:
            errors += 1
            if errors <= 10:
                print(f"  !! {symbol} {tf}: {e}")

        if (i + 1) % 100 == 0:
            print(f"  ... {i+1}/{len(combos)}  fixed={fixed} errors={errors} "
                  f"rate_limited={rate_limited}")

    print(f"\nDone. fixed={fixed}  errors={errors}  rate_limited_total={rate_limited}")
    print("Next: delete data/rsi_cache.pkl and restart scanner.")


if __name__ == "__main__":
    main()
