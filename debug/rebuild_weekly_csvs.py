"""
rebuild_weekly_csvs.py
Rebuild every weekly CSV from its corresponding daily CSV using ISO-week
aggregation. Fixes corrupted weekly files (e.g. TCS had daily-dated rows
inside the weekly CSV poisoning Wilder RSI).

Backs up each original to .bak.YYYYMMDDHHMMSS before overwriting.
"""
import os
import csv
import shutil
from datetime import datetime
from collections import defaultdict

ROOT     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data", "price_history")
HEADERS  = ["datetime", "open", "high", "low", "close", "volume"]


def load_daily(path):
    """Load daily CSV as list of dicts with parsed datetime."""
    out = []
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            try:
                dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if dt.weekday() >= 5:
                continue   # safety: drop Sat/Sun
            out.append({
                "datetime": dt,
                "open":     float(row["open"]),
                "high":     float(row["high"]),
                "low":      float(row["low"]),
                "close":    float(row["close"]),
                "volume":   int(float(row.get("volume", 0))),
            })
    return out


def aggregate_to_weekly(daily):
    """Group daily candles by ISO week (year, week) and aggregate."""
    weeks = defaultdict(list)
    for c in daily:
        iso_year, iso_week, _ = c["datetime"].isocalendar()
        weeks[(iso_year, iso_week)].append(c)

    out = []
    for key in sorted(weeks.keys()):
        wc = sorted(weeks[key], key=lambda c: c["datetime"])
        out.append({
            "datetime": wc[0]["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
            "open":     wc[0]["open"],
            "high":     max(c["high"] for c in wc),
            "low":      min(c["low"]  for c in wc),
            "close":    wc[-1]["close"],
            "volume":   sum(c["volume"] for c in wc),
        })
    return out


def main():
    if not os.path.isdir(DATA_DIR):
        print(f"No data dir at {DATA_DIR}")
        return

    rebuilt = 0
    skipped = 0
    errors  = 0
    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith("_Wm.csv"):
            continue
        weekly_path = os.path.join(DATA_DIR, fname)
        daily_path  = weekly_path.replace("_Wm.csv", "_Dm.csv")
        if not os.path.exists(daily_path):
            skipped += 1
            continue
        try:
            daily  = load_daily(daily_path)
            if len(daily) < 5:
                skipped += 1
                continue
            weekly = aggregate_to_weekly(daily)
            # Backup
            bak = weekly_path + ".bak." + datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copy2(weekly_path, bak)
            # Write
            with open(weekly_path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=HEADERS)
                w.writeheader()
                for r in weekly:
                    w.writerow({
                        "datetime": r["datetime"],
                        "open":     round(r["open"],  2),
                        "high":     round(r["high"],  2),
                        "low":      round(r["low"],   2),
                        "close":    round(r["close"], 2),
                        "volume":   int(r["volume"]),
                    })
            rebuilt += 1
        except Exception as e:
            errors += 1
            print(f"  !! {fname}: {e}")

    print(f"Rebuilt   : {rebuilt}")
    print(f"Skipped   : {skipped} (no daily, or insufficient data)")
    print(f"Errors    : {errors}")


if __name__ == "__main__":
    main()
