"""Verify each of today's S1 BUY entries against current settings.
Trace the 5m close + RSI sequence between watch_time and buy_time."""
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

print(f"Settings: rsi_entry<{S1['rsi_entry_threshold']}, drop>={S1['drop_percent']}, "
      f"reset>{S1['rsi_reset_threshold']}, D>{S1['daily_rsi_threshold']}, W>{S1['weekly_rsi_threshold']}\n")

ds  = DataStore()
eng = ReplayEngine(ds, rsi_period=14)

# Trace cases
cases = [
    ("NSE:DIVISLAB-EQ",   "2026-05-25 09:15", "2026-05-25 13:15", 6750.00),
    ("NSE:WELSPUNLIV-EQ", "2026-05-22 10:40", "2026-05-25 09:35", 138.17),
]

for sym, watch_dt, buy_dt, ref in cases:
    watch_t = IST.localize(datetime.strptime(watch_dt, "%Y-%m-%d %H:%M"))
    buy_t   = IST.localize(datetime.strptime(buy_dt,   "%Y-%m-%d %H:%M"))

    print("=" * 80)
    print(f"{sym}: watch={watch_dt} ref={ref}  buy={buy_dt}")
    print("=" * 80)

    rows = []
    def cb(candle, tf, rsi_engine, d_rsi, w_rsi, sym=sym):
        if tf != "5":
            return
        when = candle["datetime"] + timedelta(minutes=5)
        if when < watch_t or when > buy_t + timedelta(minutes=5):
            return
        rsi = rsi_engine.get_rsi(sym, "5")
        rows.append((when, candle["close"], rsi, d_rsi, w_rsi))

    eng.replay(
        symbol=sym, tfs=["5"],
        from_dt=watch_t - timedelta(days=1),
        to_dt  =buy_t + timedelta(minutes=10),
        callback=cb, seed_lookback_days=60,
    )

    rsi_entry = float(S1["rsi_entry_threshold"])
    rsi_reset = float(S1["rsi_reset_threshold"])
    drop_pct  = float(S1["drop_percent"])

    # Walk and find any reset (rsi > reset_threshold) and re-watch points
    state    = "WATCHED"
    cur_ref  = ref
    print(f"{'time':<20} {'close':>10} {'rsi':>7} {'drop%':>7}   note")
    print("-" * 80)
    for when, close, rsi, d, w in rows:
        if state == "WATCHED":
            drop = ((cur_ref - close) / cur_ref * 100) if cur_ref else 0
            note = ""
            if rsi is not None and rsi > rsi_reset:
                note = f"RSI {rsi:.2f} > {rsi_reset} -> RESET to GENERAL"
                state = "GENERAL"
                cur_ref = None
            elif drop >= drop_pct:
                note = f"drop {drop:.3f}% >= {drop_pct} -> BUY"
            rsi_s = f"{rsi:.2f}" if rsi is not None else "  -"
            print(f"{when.strftime('%Y-%m-%d %H:%M'):<20} {close:>10.2f} "
                  f"{rsi_s:>7} {drop:>7.3f}   {note}")
            if note.startswith("drop"):
                break
        elif state == "GENERAL":
            note = ""
            if rsi is not None and rsi < rsi_entry:
                note = f"RSI {rsi:.2f} < {rsi_entry} -> NEW WATCH at close {close:.2f}"
                state = "WATCHED"
                cur_ref = close
            rsi_s = f"{rsi:.2f}" if rsi is not None else "  -"
            print(f"{when.strftime('%Y-%m-%d %H:%M'):<20} {close:>10.2f} "
                  f"{rsi_s:>7} {'  -':>7}   {note}")
    print()
