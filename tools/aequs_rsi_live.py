"""Compute AEQUS RSI three ways:
  1. Last completed 5m candle RSI (what trace tool shows)
  2. Live Daily RSI extrapolating one Wilder step with today's running close
     (what Fyers chart shows during the day)
  3. Live Weekly RSI extrapolating with this week's running close
"""
import os, sys
from datetime import datetime
import pytz
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.data_store import DataStore
from core.rsi_engine import RSICalculator

IST = pytz.timezone('Asia/Kolkata')
SYM = 'NSE:AEQUS-EQ'

ds = DataStore()

# --- 1. 5m series RSI at last candle ---
c5 = ds.load_candles(SYM, '5')
calc = RSICalculator(period=14)
for c in c5:
    calc.update(c['close'])
last_5m = c5[-1]
print(f"5m last candle    : {last_5m['datetime']}  close={last_5m['close']}")
print(f"   5m RSI(14)     : {calc.get_rsi()}")

# --- 2. Daily RSI - completed vs live ---
daily = ds.load_daily_from_5m(SYM)
# Strip today (incomplete daily candle)
today = datetime.now(IST).date()
daily_completed = [d for d in daily if d['datetime'].date() < today]
calc_d = RSICalculator(period=14)
for d in daily_completed:
    calc_d.update(d['close'])
last_d_close = daily_completed[-1]['close']
last_d_date  = daily_completed[-1]['datetime'].date()
completed_d_rsi = calc_d.get_rsi()
print()
print(f"Daily last COMPLETED: {last_d_date}  close={last_d_close}  D-RSI={completed_d_rsi}")

# Live: one Wilder step using today's current close
running_close = last_5m['close']
diff = running_close - last_d_close
gain = max(0, diff); loss = max(0, -diff)
p = calc_d.period
ag = (calc_d.avg_gain * (p-1) + gain) / p
al = (calc_d.avg_loss * (p-1) + loss) / p
live_d_rsi = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag/al)
print(f"Live D-RSI         : {round(live_d_rsi, 2)}  "
      f"(using today's running close={running_close})")

# --- 3. Weekly: similar treatment ---
weekly = ds.load_weekly_from_daily(ds.load_candles(SYM, 'D'))
# Strip current week
iso_now = datetime.now(IST).isocalendar()
weekly_completed = [w for w in weekly
                    if w['datetime'].isocalendar()[:2] != iso_now[:2]]
calc_w = RSICalculator(period=14)
for w in weekly_completed:
    calc_w.update(w['close'])
last_w_close = weekly_completed[-1]['close']
last_w_date  = weekly_completed[-1]['datetime'].date()
completed_w_rsi = calc_w.get_rsi()
print()
print(f"Weekly last COMPLETED: {last_w_date}  close={last_w_close}  W-RSI={completed_w_rsi}")
diff = running_close - last_w_close
gain = max(0, diff); loss = max(0, -diff)
p = calc_w.period
ag = (calc_w.avg_gain * (p-1) + gain) / p
al = (calc_w.avg_loss * (p-1) + loss) / p
live_w_rsi = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag/al)
print(f"Live W-RSI         : {round(live_w_rsi, 2)}  "
      f"(using today's running close={running_close})")
