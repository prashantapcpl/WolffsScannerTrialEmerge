"""Verify ENRIN 5m RSI on 22-May 10:40 (Fyers reports 24.92).
Plus INGERRAND D/W RSI at 13:15 today."""
import os, sys, json
from datetime import datetime, timedelta
import pytz
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from core.data_store    import DataStore
from core.replay_engine import ReplayEngine

IST = pytz.timezone("Asia/Kolkata")
ds  = DataStore()
eng = ReplayEngine(ds, rsi_period=14)

# ENRIN around 22-May 10:40
print("=" * 78)
print("ENRIN  22-May 10:30 - 10:50  (Fyers chart says RSI ~24.92 at 10:40)")
print("=" * 78)
rows = []
def cb1(candle, tf, rsi_engine, d_rsi, w_rsi):
    if tf != "5":
        return
    when = candle["datetime"] + timedelta(minutes=5)
    if when.date() != datetime(2026, 5, 22).date():
        return
    if not (when.hour == 10 and 30 <= when.minute <= 55):
        return
    rsi = rsi_engine.get_rsi("NSE:ENRIN-EQ", "5")
    rows.append((when, candle["close"], rsi, d_rsi, w_rsi))

eng.replay(symbol="NSE:ENRIN-EQ", tfs=["5"],
           from_dt=IST.localize(datetime(2026, 5, 20, 9, 15)),
           to_dt  =IST.localize(datetime(2026, 5, 22, 15, 30)),
           callback=cb1, seed_lookback_days=60)

print(f"{'close time':<20} {'close':>9} {'5m-RSI':>8} {'D-RSI':>8} {'W-RSI':>8}")
print("-" * 60)
for when, close, rsi, d, w in rows:
    print(f"{when.strftime('%Y-%m-%d %H:%M'):<20} {close:>9.2f} "
          f"{rsi:>8.2f} {(d if d is not None else 0):>8.2f} {(w if w is not None else 0):>8.2f}")

# ENRIN around 25-May 09:15
print()
print("=" * 78)
print("ENRIN  25-May 09:15 - 09:30  (Fyers chart says RSI 16.18 at 09:15)")
print("=" * 78)
rows = []
def cb2(candle, tf, rsi_engine, d_rsi, w_rsi):
    if tf != "5":
        return
    when = candle["datetime"] + timedelta(minutes=5)
    if when.date() != datetime(2026, 5, 25).date():
        return
    if not (when.hour == 9 and 15 <= when.minute <= 35):
        return
    rsi = rsi_engine.get_rsi("NSE:ENRIN-EQ", "5")
    rows.append((when, candle["close"], rsi, d_rsi, w_rsi))

eng.replay(symbol="NSE:ENRIN-EQ", tfs=["5"],
           from_dt=IST.localize(datetime(2026, 5, 23, 9, 15)),
           to_dt  =IST.localize(datetime(2026, 5, 25, 15, 30)),
           callback=cb2, seed_lookback_days=60)

print(f"{'close time':<20} {'close':>9} {'5m-RSI':>8} {'D-RSI':>8} {'W-RSI':>8}")
print("-" * 60)
for when, close, rsi, d, w in rows:
    print(f"{when.strftime('%Y-%m-%d %H:%M'):<20} {close:>9.2f} "
          f"{rsi:>8.2f} {(d if d is not None else 0):>8.2f} {(w if w is not None else 0):>8.2f}")

# INGERRAND at 13:15 today (already known but confirm)
print()
print("=" * 78)
print("INGERRAND  25-May 13:00 - 13:20  (BUY fired at 13:15)")
print("=" * 78)
rows = []
def cb3(candle, tf, rsi_engine, d_rsi, w_rsi):
    if tf != "5":
        return
    when = candle["datetime"] + timedelta(minutes=5)
    if when.date() != datetime(2026, 5, 25).date():
        return
    if not (when.hour == 13 and 0 <= when.minute <= 25):
        return
    rsi = rsi_engine.get_rsi("NSE:INGERRAND-EQ", "5")
    rows.append((when, candle["close"], rsi, d_rsi, w_rsi))

eng.replay(symbol="NSE:INGERRAND-EQ", tfs=["5"],
           from_dt=IST.localize(datetime(2026, 5, 23, 9, 15)),
           to_dt  =IST.localize(datetime(2026, 5, 25, 15, 30)),
           callback=cb3, seed_lookback_days=60)

print(f"{'close time':<20} {'close':>9} {'5m-RSI':>8} {'D-RSI':>8} {'W-RSI':>8}")
print("-" * 60)
for when, close, rsi, d, w in rows:
    print(f"{when.strftime('%Y-%m-%d %H:%M'):<20} {close:>9.2f} "
          f"{rsi:>8.2f} {(d if d is not None else 0):>8.2f} {(w if w is not None else 0):>8.2f}")
