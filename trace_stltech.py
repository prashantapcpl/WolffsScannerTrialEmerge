"""Trace STLTECH 5m candles around 2026-05-12 13:25-13:45 + S2 condition check."""
import os, sys, json
from datetime import datetime, timedelta
import pytz
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from core.data_store    import DataStore
from core.replay_engine import ReplayEngine

IST = pytz.timezone("Asia/Kolkata")
SYM = "NSE:STLTECH-EQ"

# S2 settings
with open(os.path.join(ROOT, "config.json")) as f:
    S2 = json.load(f)["scanners"]["scanner_2"]["settings"]

print("S2 settings:")
print(f"  rsi_entry_threshold : {S2['rsi_entry_threshold']}")
print(f"  rsi_reset_threshold : {S2['rsi_reset_threshold']}")
print(f"  drop_percent        : {S2['drop_percent']}")
print(f"  daily_rsi_threshold : {S2['daily_rsi_threshold']}")
print(f"  weekly_rsi_threshold: {S2['weekly_rsi_threshold']}")
print()

# Raw CSV around 12-May 13:00-14:00
ds = DataStore()
candles = ds.load_candles(SYM, "5")
print(f"All 5m candles between 12-May 13:00 and 14:00:")
print(f"{'datetime':<20} {'open':>9} {'high':>9} {'low':>9} {'close':>9} {'volume':>12}")
print("-" * 75)
for c in candles:
    if c["datetime"].date() != datetime(2026,5,12).date():
        continue
    if not (c["datetime"].hour == 13 and 0 <= c["datetime"].minute <= 59):
        continue
    print(f"{c['datetime'].strftime('%Y-%m-%d %H:%M'):<20} "
          f"{c['open']:>9.2f} {c['high']:>9.2f} {c['low']:>9.2f} "
          f"{c['close']:>9.2f} {c['volume']:>12}")

# Replay with RSI
print()
print("RSI series around 12-May 13:25-13:45 (replay):")
eng = ReplayEngine(ds, rsi_period=14)
from_dt = IST.localize(datetime(2026, 5, 10, 9, 15))
to_dt   = IST.localize(datetime(2026, 5, 12, 15, 30))

rows = []
def cb(candle, tf, rsi_engine, d_rsi, w_rsi):
    if tf != "5":
        return
    when = candle["datetime"] + timedelta(minutes=5)
    if when.date() != datetime(2026, 5, 12).date():
        return
    if not (when.hour == 13 and 20 <= when.minute <= 50):
        return
    rsi = rsi_engine.get_rsi(SYM, "5")
    rows.append((when, candle["close"], rsi, d_rsi, w_rsi))

eng.replay(symbol=SYM, tfs=["5"], from_dt=from_dt, to_dt=to_dt,
           callback=cb, seed_lookback_days=60)

print(f"{'5m close time':<20} {'close':>9} {'5m-RSI':>8} {'D-RSI':>8} {'W-RSI':>8}")
print("-" * 60)
for when, close, rsi, d, w in rows:
    rsi_s = f"{rsi:.2f}" if rsi is not None else "  -"
    d_s   = f"{d:.2f}"   if d   is not None else "  -"
    w_s   = f"{w:.2f}"   if w   is not None else "  -"
    print(f"{when.strftime('%Y-%m-%d %H:%M'):<20} {close:>9.2f} "
          f"{rsi_s:>8} {d_s:>8} {w_s:>8}")

# State file lookup
print()
with open(os.path.join(ROOT, "data", "scanner_2_state.json")) as f:
    state = json.load(f)
rec = state["records"].get(SYM)
if rec:
    print(f"State file for {SYM}:")
    for k in ["state","side","watched_at","reference_price","reference_time",
             "rsi_at_watch","buy_time","buy_price","drop_pct_at_buy","gap_fill"]:
        print(f"  {k:<18} = {rec.get(k)}")
