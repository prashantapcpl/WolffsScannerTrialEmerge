"""
clean_phantom_candles.py
Remove non-trading-day rows from ALL price-history CSV files.

For Indian equity markets:
  - Saturday (weekday 5) and Sunday (weekday 6) are closed.
  - This script does NOT know about exchange holidays. Those would still leak
    through if the scanner was running on a holiday.

Also detects "frozen identical OHLCV blocks" within a trading day (Mon-Fri),
which usually indicates the websocket was disconnected and the candle engine
was emitting placeholder bars from stale ticks. These are also removed.

Backs up each CSV to .bak.YYYYMMDDHHMMSS before modifying.

Usage:
  python clean_phantom_candles.py            # dry run, prints what would be removed
  python clean_phantom_candles.py --apply    # actually modify the files
"""
import os
import sys
import csv
import shutil
from datetime import datetime

ROOT     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data", "price_history")

DRY_RUN  = "--apply" not in sys.argv

# Trading-day check: Mon=0 ... Sun=6.  Treat Sat/Sun as non-trading.
def is_trading_day(dt: datetime) -> bool:
    return dt.weekday() < 5

# A row is "phantom-frozen" if open == high == low == close (zero range).
# Real Indian-market 5m bars almost never have zero range on a normal trading
# day -- bid-ask spread alone usually produces a few ticks of movement.
def is_frozen(row: dict) -> bool:
    try:
        o = float(row["open"]); h = float(row["high"])
        l = float(row["low"]);  c = float(row["close"])
        return o == h == l == c
    except Exception:
        return False


def clean_file(path: str) -> tuple:
    """Returns (rows_in, weekend_removed, frozen_removed, rows_out)."""
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)
        fields = reader.fieldnames

    rows_in = len(rows)
    weekend = 0
    frozen  = 0
    kept    = []

    # Detect runs of CONSECUTIVE frozen rows -- a single zero-range bar in
    # the middle of a normal day might be a real illiquid candle; a run of
    # 5+ consecutive identical rows is almost certainly phantom.
    run_start = None
    run_close = None
    for i, r in enumerate(rows):
        try:
            dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            kept.append(r)
            continue

        if not is_trading_day(dt):
            weekend += 1
            continue

        kept.append(r)

    # Second pass: detect frozen runs in the kept set
    final = []
    i = 0
    while i < len(kept):
        if not is_frozen(kept[i]):
            final.append(kept[i])
            i += 1
            continue
        # We're at a frozen row -- look ahead to see if it's part of a run
        j = i
        while j < len(kept) and is_frozen(kept[j]):
            try:
                if float(kept[j]["close"]) != float(kept[i]["close"]):
                    break
            except Exception:
                break
            j += 1
        run_len = j - i
        if run_len >= 5:
            frozen += run_len
        else:
            final.extend(kept[i:j])
        i = j

    rows_out = len(final)

    if not DRY_RUN and (weekend > 0 or frozen > 0):
        bak = path + ".bak." + datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, bak)
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(final)

    return rows_in, weekend, frozen, rows_out


def main():
    if not os.path.isdir(DATA_DIR):
        print(f"No data dir at {DATA_DIR}")
        return

    print(f"{'mode:':<6} {'DRY-RUN (no changes)' if DRY_RUN else 'APPLY (will modify)'}\n")

    total_files = 0
    total_in    = 0
    total_we    = 0
    total_fz    = 0
    affected    = []

    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith(".csv"):
            continue
        path = os.path.join(DATA_DIR, fname)
        try:
            n_in, n_we, n_fz, n_out = clean_file(path)
        except Exception as e:
            print(f"  !! {fname}: {e}")
            continue
        total_files += 1
        total_in    += n_in
        total_we    += n_we
        total_fz    += n_fz
        if n_we or n_fz:
            affected.append((fname, n_in, n_we, n_fz, n_out))

    print(f"Scanned {total_files} CSV files")
    print(f"  total rows in   : {total_in:>10}")
    print(f"  weekend removed : {total_we:>10}")
    print(f"  frozen removed  : {total_fz:>10}")
    print(f"  affected files  : {len(affected):>10}\n")

    print("Top 15 most-affected files:")
    affected.sort(key=lambda x: x[2] + x[3], reverse=True)
    for fname, n_in, n_we, n_fz, n_out in affected[:15]:
        print(f"  {fname:<35} rows {n_in}->{n_out}  (we={n_we} fz={n_fz})")

    if DRY_RUN:
        print("\n*** DRY RUN. To actually clean: python clean_phantom_candles.py --apply")


if __name__ == "__main__":
    main()
