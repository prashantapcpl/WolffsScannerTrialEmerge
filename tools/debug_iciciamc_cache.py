import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.rsi_cache import get_rsi_cache

cache = get_rsi_cache()
print(f"Loading cache...")
cache.load()
print(f"built_at: {cache._built_at}")
print(f"built_date: {cache._built_date}")

sym = "NSE:ICICIAMC-EQ"
series = cache.get_rsi_series(sym, "5")
dates  = cache.get_datetimes(sym, "5")
closes = cache.get_closes(sym, "5")
print(f"\n{sym} 5m cache:")
print(f"  total bars: {len(series)}")
if dates:
    print(f"  first: {dates[0]}")
    print(f"  last : {dates[-1]}")

# Look for today
today_str = "2026-05-27"
matches = [(i, dates[i], closes[i], series[i])
           for i in range(len(dates)) if dates[i].startswith(today_str)]
print(f"\n  bars from {today_str} in cache: {len(matches)}")
if matches:
    # Find any with RSI < 20
    low_rsi = [m for m in matches if m[3] is not None and m[3] < 20]
    print(f"  bars with RSI < 20: {len(low_rsi)}")
    for m in low_rsi[:5]:
        print(f"    idx={m[0]}  {m[1]}  close={m[2]}  rsi={m[3]:.2f}")
    print(f"\n  ALL today's bars (showing first 10 + last 5):")
    for m in matches[:10]:
        print(f"    {m[1]}  close={m[2]}  rsi={m[3]}")
    if len(matches) > 10:
        print(f"    ...")
        for m in matches[-5:]:
            print(f"    {m[1]}  close={m[2]}  rsi={m[3]}")
