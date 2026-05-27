import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.data_store import DataStore
from datetime import date

ds = DataStore()
candles = ds.load_candles('NSE:ICICIAMC-EQ', '5')
today = date(2026, 5, 27)
todays = [c for c in candles if c['datetime'].date() == today]
print(f'Today total 5m candles: {len(todays)}')

lo = min(c['close'] for c in todays)
hi = max(c['close'] for c in todays)
print(f'Lowest close today : Rs.{lo:.2f}')
print(f'Highest close today: Rs.{hi:.2f}')

threshold = 3306 * 0.99
print(f'\nBUY trigger threshold (3306 * 0.99): Rs.{threshold:.2f}')
below = [c for c in todays if c['close'] < threshold]
print(f'Candles closing below threshold: {len(below)}')
for c in below[:5]:
    print(f'  {c["datetime"].strftime("%H:%M")}  close={c["close"]:.2f}')

print(f'\n5 lowest 5m closes today:')
for c in sorted(todays, key=lambda c: c['close'])[:5]:
    print(f'  {c["datetime"].strftime("%H:%M")}  close={c["close"]:.2f}  '
          f'drop from 3306 = {(3306-c["close"])/3306*100:.2f}%')

# Check S2 signal log for ICICIAMC today
import json
with open(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                       'data', 'scanner_2_state.json')) as f:
    state = json.load(f)
sig = [s for s in state.get('signal_log', [])
       if s.get('symbol') == 'NSE:ICICIAMC-EQ' or s.get('plain_name') == 'ICICIAMC']
print(f'\nScanner 2 signal log entries for ICICIAMC: {len(sig)}')
for s in sig[-5:]:
    print(f'  {s.get("time")}  {s.get("type","?")}  price={s.get("price")}')
