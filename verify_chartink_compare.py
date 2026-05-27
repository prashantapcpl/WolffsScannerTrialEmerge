"""Compare Chartink picks (MTARTECH, ENRIN) vs our app picks (YATHARTH,
INGERRAND) against current S1 conditions: rsi_5m<18, D>60, W>60."""
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
d_thr     = float(S1["daily_rsi_threshold"])
w_thr     = float(S1["weekly_rsi_threshold"])

print(f"S1 conditions: rsi_5m<{rsi_entry} AND D>{d_thr} AND W>{w_thr}\n")

cases = [
    ("NSE:MTARTECH-EQ", "09:15", "09:30"),
    ("NSE:ENRIN-EQ",    "09:15", "09:30"),
    ("NSE:YATHARTH-EQ", "14:15", "14:30"),
    ("NSE:INGERRAND-EQ","13:00", "13:30"),
]

ds  = DataStore()
eng = ReplayEngine(ds, rsi_period=14)

for sym, t_from, t_to in cases:
    print("=" * 78)
    print(f"{sym}  (5m candles closing between {t_from} and {t_to} on 25-May)")
    print("=" * 78)
    from_dt = IST.localize(datetime(2026, 5, 23, 9, 15))
    h_from, m_from = int(t_from.split(":")[0]), int(t_from.split(":")[1])
    h_to,   m_to   = int(t_to.split(":")[0]),   int(t_to.split(":")[1])
    to_dt   = IST.localize(datetime(2026, 5, 25, 15, 30))

    rows = []
    def cb(candle, tf, rsi_engine, d_rsi, w_rsi, sym=sym, h_from=h_from, m_from=m_from, h_to=h_to, m_to=m_to):
        if tf != "5":
            return
        when = candle["datetime"] + timedelta(minutes=5)
        if when.date() != datetime(2026, 5, 25).date():
            return
        if not ((when.hour > h_from or (when.hour == h_from and when.minute >= m_from))
                and (when.hour < h_to or (when.hour == h_to and when.minute <= m_to))):
            return
        rsi = rsi_engine.get_rsi(sym, "5")
        rows.append((when, candle["close"], rsi, d_rsi, w_rsi))

    try:
        eng.replay(symbol=sym, tfs=["5"], from_dt=from_dt, to_dt=to_dt,
                   callback=cb, seed_lookback_days=60)
    except Exception as e:
        print(f"  ERROR: {e}")
        continue

    if not rows:
        print("  No 5m candles in window")
        continue

    print(f"{'time':<18} {'close':>10} {'5m-RSI':>8} {'D-RSI':>8} {'W-RSI':>8}   verdict")
    print("-" * 90)
    for when, close, rsi, d, w in rows:
        rsi_s = f"{rsi:.2f}" if rsi is not None else "  -"
        d_s   = f"{d:.2f}"   if d   is not None else "  -"
        w_s   = f"{w:.2f}"   if w   is not None else "  -"
        verdict = ""
        if rsi is not None and d is not None and w is not None:
            cond_r = rsi < rsi_entry
            cond_d = d > d_thr
            cond_w = w > w_thr
            if cond_r and cond_d and cond_w:
                verdict = "PASS all 3"
            else:
                fails = []
                if not cond_r: fails.append(f"rsi {rsi:.2f} !<{rsi_entry}")
                if not cond_d: fails.append(f"D {d:.2f} !>{d_thr}")
                if not cond_w: fails.append(f"W {w:.2f} !>{w_thr}")
                verdict = "FAIL: " + ", ".join(fails)
        print(f"{when.strftime('%Y-%m-%d %H:%M'):<18} {close:>10.2f} "
              f"{rsi_s:>8} {d_s:>8} {w_s:>8}   {verdict}")
    print()
