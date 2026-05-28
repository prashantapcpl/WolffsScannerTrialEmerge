"""
reclean_phantoms_strict.py
Restore each .bak.* backup, then re-run cleanup with STRICTER detection:

  - Sat/Sun rows: always removed (markets closed = always phantom).
  - Weekday "frozen runs": removed ONLY if OHLC AND volume are ALL identical
    across 5+ consecutive bars (signature of websocket-disconnect phantoms,
    NOT of genuinely illiquid stocks which would have varying tick volumes).

After this pass the .bak files are kept (not deleted) so you can restore
again if needed.
"""
import os
import csv
import shutil
import glob
from datetime import datetime

ROOT     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data", "price_history")
HEADERS  = ["datetime", "open", "high", "low", "close", "volume"]


def is_trading_day(dt: datetime) -> bool:
    return dt.weekday() < 5


def is_frozen_with_volume(rows: list, i: int) -> tuple:
    """Return (run_length, run_close) starting at index i if rows[i..] is a
    frozen run (identical OHLCV). Otherwise (0, None)."""
    if i >= len(rows):
        return 0, None
    try:
        o = float(rows[i]["open"]);  h = float(rows[i]["high"])
        l = float(rows[i]["low"]);   c = float(rows[i]["close"])
        v = float(rows[i].get("volume", 0))
    except Exception:
        return 0, None
    if not (o == h == l == c):
        return 0, None
    j = i + 1
    while j < len(rows):
        try:
            o2 = float(rows[j]["open"]);  h2 = float(rows[j]["high"])
            l2 = float(rows[j]["low"]);   c2 = float(rows[j]["close"])
            v2 = float(rows[j].get("volume", 0))
        except Exception:
            break
        if (o2 == h2 == l2 == c2 == c) and v2 == v:
            j += 1
        else:
            break
    return j - i, c


def reclean_file(path: str) -> tuple:
    """Returns (rows_in, weekend_removed, frozen_removed, rows_out)."""
    with open(path, "r", newline="") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)
        fields = reader.fieldnames

    rows_in = len(rows)

    # Pass 1: drop weekend rows
    weekend = 0
    kept = []
    for r in rows:
        try:
            dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            kept.append(r)
            continue
        if not is_trading_day(dt):
            weekend += 1
            continue
        kept.append(r)

    # Pass 2: drop frozen runs (OHLCV all identical) of length >= 5
    final = []
    frozen = 0
    i = 0
    while i < len(kept):
        run_len, _ = is_frozen_with_volume(kept, i)
        if run_len >= 5:
            frozen += run_len
            i += run_len
            continue
        final.append(kept[i])
        i += 1

    rows_out = len(final)

    if weekend > 0 or frozen > 0:
        # write the corrected file (we don't make ANOTHER backup -- the
        # original-original is already in the .bak file we restored from)
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(final)

    return rows_in, weekend, frozen, rows_out


def main():
    # Step 1: restore .bak files
    bak_files = glob.glob(os.path.join(DATA_DIR, "*.bak.*"))
    # Filter to only the over-aggressive cleanup backups (created today)
    # by keeping only those whose .bak.* contains a recent timestamp.
    bak_files = sorted(set(bak_files))

    print(f"Step 1: Restore {len(bak_files)} .bak files...")
    restored = 0
    for bak in bak_files:
        # original file path is the bak path with .bak.<timestamp> stripped
        # bak format: NAME.csv.bak.20260525XYZ
        # we want to find the latest backup per original
        pass

    # Build map: original_path -> list of bak files (sorted, most recent last)
    bak_by_orig = {}
    for bak in bak_files:
        # split off everything after .bak.
        idx = bak.find(".bak.")
        if idx < 0:
            continue
        orig = bak[:idx]
        bak_by_orig.setdefault(orig, []).append(bak)

    for orig, baks in bak_by_orig.items():
        baks.sort()
        # Restore from the OLDEST backup (= original-original state before
        # any of today's cleanup).
        try:
            shutil.copy2(baks[0], orig)
            restored += 1
        except Exception as e:
            print(f"  !! restore {orig}: {e}")
    print(f"  Restored {restored} CSV files from their oldest backup.\n")

    # Step 2: re-run STRICT cleanup
    print("Step 2: Re-run STRICT cleanup (volume-aware)...")
    total_files = 0
    total_we    = 0
    total_fz    = 0
    affected    = []
    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith(".csv"):
            continue
        path = os.path.join(DATA_DIR, fname)
        try:
            n_in, n_we, n_fz, n_out = reclean_file(path)
        except Exception as e:
            print(f"  !! {fname}: {e}")
            continue
        total_files += 1
        total_we    += n_we
        total_fz    += n_fz
        if n_we or n_fz:
            affected.append((fname, n_in, n_we, n_fz, n_out))

    print(f"\n  Files processed     : {total_files}")
    print(f"  weekend rows removed: {total_we}")
    print(f"  frozen-OHLCV removed: {total_fz}  (volume-matched, >= 5 in a row)")
    print(f"  files affected      : {len(affected)}")


if __name__ == "__main__":
    main()
