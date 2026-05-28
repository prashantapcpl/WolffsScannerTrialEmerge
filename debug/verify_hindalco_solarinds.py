"""Verify whether HINDALCO and SOLARINDS satisfied S1 watch+buy conditions
on 25-May-2026 09:15-09:30."""
import os, sys, json
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.data_store    import DataStore
from core.replay_engine import ReplayEngine

IST = pytz.timezone("Asia/Kolkata")

with open(os.path.join(ROOT, "config.json")) as f:
    S1 = json.load(f)["scanners"]["scanner_1"]["settings"]

rsi_entry = float(S1["rsi_entry_threshold"])
drop_pct  = float(S1["drop_percent"])
d_thr     = float(S1["daily_rsi_threshold"])
w_thr     = float(S1["weekly_rsi_threshold"])
scan_tf   = S1["scan_timeframe"]

print("=" * 78)
print("Scanner 1 BUY-side conditions for WATCH then BUY:")
print(f"  scan_timeframe          : {scan_tf}m")
print(f"  rsi_entry_threshold     : RSI({scan_tf}m) < {rsi_entry}")
print(f"  daily_rsi_threshold     : D-RSI > {d_thr}")
print(f"  weekly_rsi_threshold    : W-RSI > {w_thr}")
print(f"  drop_percent (BUY trig.): close drop from ref >= {drop_pct}%")
print("=" * 78)

ds  = DataStore()
eng = ReplayEngine(ds, rsi_period=14)

# Window: 24-May (pre-data for D/W seed) through 25-May 09:30
to_dt   = IST.localize(datetime(2026, 5, 25, 9, 35))
from_dt = IST.localize(datetime(2026, 5, 23, 9, 15))

for sym in ["NSE:HINDALCO-EQ", "NSE:SOLARINDS-EQ"]:
    print(f"\n{'=' * 78}")
    print(f"SYMBOL: {sym}")
    print(f"{'=' * 78}")
    rows = []
    def cb(candle, tf, rsi_engine, d_rsi, w_rsi, sym=sym):
        if tf != scan_tf:
            return
        when_close = candle["datetime"] + timedelta(minutes=int(scan_tf))
        if when_close.date() != datetime(2026, 5, 25).date():
            return
        if when_close.hour > 9 or (when_close.hour == 9 and when_close.minute > 30):
            return
        rsi = rsi_engine.get_rsi(sym, scan_tf)
        rows.append((when_close, candle["close"], rsi, d_rsi, w_rsi))
    eng.replay(symbol=sym, tfs=[scan_tf],
               from_dt=from_dt, to_dt=to_dt,
               callback=cb, seed_lookback_days=60)

    print(f"{'5m close time':<20} {'close':>10} {'5m-RSI':>8} "
          f"{'D-RSI':>8} {'W-RSI':>8}   verdict")
    print("-" * 90)
    for when, close, rsi, d, w in rows:
        rsi_s = f"{rsi:.2f}" if rsi is not None else "  -"
        d_s   = f"{d:.2f}"   if d   is not None else "  -"
        w_s   = f"{w:.2f}"   if w   is not None else "  -"
        verdict = ""
        if rsi is not None and d is not None and w is not None:
            cond_rsi = rsi < rsi_entry
            cond_d   = d   > d_thr
            cond_w   = w   > w_thr
            if cond_rsi and cond_d and cond_w:
                verdict = "WATCH OK"
            else:
                fail = []
                if not cond_rsi: fail.append(f"rsi {rsi:.2f} NOT < {rsi_entry}")
                if not cond_d:   fail.append(f"d {d:.2f} NOT > {d_thr}")
                if not cond_w:   fail.append(f"w {w:.2f} NOT > {w_thr}")
                verdict = "watch fail: " + ", ".join(fail)
        print(f"{when.strftime('%Y-%m-%d %H:%M'):<20} {close:>10.2f} "
              f"{rsi_s:>8} {d_s:>8} {w_s:>8}   {verdict}")
