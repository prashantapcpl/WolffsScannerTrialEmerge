"""Show TATACOMM's 15m closes + RSIs around 20-May 2026."""
import os
import sys
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.data_store    import DataStore
from core.replay_engine import ReplayEngine

IST = pytz.timezone("Asia/Kolkata")

ds  = DataStore()
eng = ReplayEngine(ds, rsi_period=14)

rows = []
def cb(candle, tf, rsi_engine, d_rsi, w_rsi):
    if tf != "15":
        return
    when = candle["datetime"] + timedelta(minutes=15)
    if when.date() < datetime(2026, 5, 19).date():
        return
    if when.date() > datetime(2026, 5, 22).date():
        return
    rsi = rsi_engine.get_rsi("NSE:TATACOMM-EQ", "15")
    rows.append((when, candle["close"], rsi, d_rsi, w_rsi))

eng.replay(
    symbol  = "NSE:TATACOMM-EQ",
    tfs     = ["15"],
    from_dt = datetime(2026, 4, 1, 9, 15, tzinfo=IST),
    to_dt   = datetime(2026, 5, 23, 15, 30, tzinfo=IST),
    callback = cb,
    seed_lookback_days = 60,
)

print(f"\nTATACOMM 15m candles, 19-May through 22-May:")
print(f"{'datetime':<20} {'close':>9} {'15m-RSI':>9} {'D-RSI':>8} {'W-RSI':>8}")
print("-" * 60)
for when, close, rsi, d, w in rows:
    rsi_s = f"{rsi:.2f}" if rsi is not None else "  -"
    d_s   = f"{d:.2f}"   if d   is not None else "  -"
    w_s   = f"{w:.2f}"   if w   is not None else "  -"
    print(f"{when.strftime('%Y-%m-%d %H:%M'):<20} {close:>9.2f} "
          f"{rsi_s:>9} {d_s:>8} {w_s:>8}")
