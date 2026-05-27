"""Debug why AVALON's 13:25 watch wasn't restored by carry-forward."""
import os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.rsi_cache  import get_rsi_cache

SYM = "NSE:AVALON-EQ"
cache = get_rsi_cache()
print(f"Loading rsi_cache...")
loaded = cache.load()
print(f"loaded={loaded}, built_at={cache._built_at}, built_date={cache._built_date}")
print()

# Check if AVALON 5m series in cache has 13:25 candle
series = cache.get_rsi_series(SYM, "5")
dates  = cache.get_datetimes(SYM, "5")
closes = cache.get_closes(SYM, "5")

print(f"AVALON 5m series:")
print(f"  total bars: {len(series)}")
if dates:
    print(f"  first date: {dates[0]}")
    print(f"  last date : {dates[-1]}")

# Find 13:25 today (26-May)
target = "2026-05-26 13:25"
print(f"\nLooking for {target}:")
found_idx = None
for i, d in enumerate(dates):
    if d.startswith(target):
        found_idx = i
        break
if found_idx is None:
    print(f"  NOT FOUND IN CACHE")
    # show context: last 5 entries
    print(f"  Last 5 cache entries for {SYM}:")
    for i in range(max(0, len(dates)-5), len(dates)):
        print(f"    [{i}] date={dates[i]} close={closes[i]} rsi={series[i]}")
else:
    print(f"  Found at index {found_idx}")
    print(f"  close={closes[found_idx]}, rsi={series[found_idx]}")
    # Show context
    for i in range(max(0, found_idx-3), min(len(dates), found_idx+5)):
        marker = " <-- TARGET" if i == found_idx else ""
        print(f"    [{i}] {dates[i]} close={closes[i]} rsi={series[i]}{marker}")
