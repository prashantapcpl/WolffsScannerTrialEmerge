"""Diagnose SOLARINDS 5m RSI at 09:20 25-May.
Show last 20 5m closes leading up to the entry candle and compute RSI step by step.
"""
import os, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from core.data_store import DataStore
from core.rsi_engine import RSICalculator

ds = DataStore()
candles = ds.load_candles("NSE:SOLARINDS-EQ", "5")
print(f"Total 5m candles loaded: {len(candles)}")
print(f"First: {candles[0]['datetime']}  close={candles[0]['close']}")
print(f"Last : {candles[-1]['datetime']}  close={candles[-1]['close']}")

# Find the 09:15 candle of 25-May
target_idx = None
for i, c in enumerate(candles):
    if c["datetime"].strftime("%Y-%m-%d %H:%M") == "2026-05-25 09:15":
        target_idx = i
        break

if target_idx is None:
    print("Could not find 25-May 09:15 candle!")
    sys.exit(1)

print(f"\n25-May 09:15 candle is at index {target_idx}")
print(f"\nLast 20 closes leading up to and including 09:15:")
for i in range(max(0, target_idx - 19), target_idx + 1):
    c = candles[i]
    marker = "  <-- 09:15 TARGET" if i == target_idx else ""
    print(f"  [{i}] {c['datetime']}  close={c['close']:>10.2f}{marker}")

# Now compute RSI using ONLY the data up to (and including) 09:15
calc = RSICalculator(period=14)
last_rsi = None
for c in candles[:target_idx + 1]:
    last_rsi = calc.update(c["close"])

print(f"\nRSI at the close of 09:15 candle (close time 09:20): {last_rsi}")
print(f"Engine state: avg_gain={calc.avg_gain}, avg_loss={calc.avg_loss}")
print(f"Last close in engine: {list(calc.closes)[-1] if calc.closes else 'none'}")
