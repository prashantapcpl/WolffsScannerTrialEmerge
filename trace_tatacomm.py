"""
trace_tatacomm.py
Walk TATACOMM through Scanner 4's CURRENT settings using the same replay
engine the live scanner uses, and print every state transition that should
fire over the last 30 days. Shows RSI values (15m signal RSI + point-in-time
daily/weekly RSI) at the moment of each transition.
"""

import os
import sys
import json
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.data_store     import DataStore
from core.rsi_engine     import RSIEngine
from core.replay_engine  import ReplayEngine

IST    = pytz.timezone("Asia/Kolkata")
SYMBOL = "NSE:TATACOMM-EQ"


def load_s4_settings():
    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)
    return cfg["scanners"]["scanner_4"]["settings"]


def main():
    cfg = load_s4_settings()
    print("=" * 78)
    print("TATACOMM trace - Scanner 4 settings:")
    print(f"  signal_timeframe        : {cfg['signal_timeframe']}m")
    print(f"  buy1 RSI band           : ({cfg['buy1_rsi_lower']}, {cfg['buy1_rsi_upper']})")
    print(f"  buy1 cooldown RSI       : {cfg['buy1_cooldown_rsi']}")
    print(f"  buy2 RSI trigger        : {cfg['buy2_rsi_trigger']}")
    print(f"  buy3 drop %             : {cfg['buy3_drop_pct']}")
    print(f"  daily_rsi_buy_filter    : {cfg['daily_rsi_buy_filter']}")
    print(f"  weekly_rsi_buy_filter   : {cfg['weekly_rsi_buy_filter']}")
    print(f"  daily_rsi_stoploss_buy  : {cfg['daily_rsi_stoploss_buy']}")
    print(f"  buy1 target %           : {cfg['buy1_target_pct']}")
    print(f"  buy2/3/4 target %       : {cfg['buy23_target_pct']}")
    print(f"  cooling_rsi_buy         : {cfg['cooling_rsi_buy']}")
    print(f"  stoploss_cooling_rsi_buy: {cfg['stoploss_cooling_rsi_buy']}")
    print("=" * 78)

    sig_tf  = cfg["signal_timeframe"]
    b1_lo   = float(cfg["buy1_rsi_lower"])
    b1_hi   = float(cfg["buy1_rsi_upper"])
    b1_cd   = float(cfg["buy1_cooldown_rsi"])
    b2_trig = float(cfg["buy2_rsi_trigger"])
    b3_drop = float(cfg["buy3_drop_pct"])
    d_buy   = float(cfg["daily_rsi_buy_filter"])
    w_buy   = float(cfg["weekly_rsi_buy_filter"])
    sl_buy  = float(cfg["daily_rsi_stoploss_buy"])
    t1_pct  = float(cfg["buy1_target_pct"])
    t23_pct = float(cfg["buy23_target_pct"])
    cool_rsi = float(cfg["cooling_rsi_buy"])
    sl_cool  = float(cfg["stoploss_cooling_rsi_buy"])

    ds  = DataStore()
    eng = ReplayEngine(ds, rsi_period=14)
    rsi_engine = RSIEngine(period=14)

    # Pre-seed the engine fully from CSV history (D, W, and 15m all pre-30days)
    to_dt   = datetime.now(IST)
    from_dt = (to_dt - timedelta(days=35)).replace(hour=9, minute=15,
                                                    second=0, microsecond=0)

    # Use replay-engine internal seeding (no external_engine) for a clean start
    state         = "GENERAL"
    entry_price   = None
    entry_time    = None
    entry_level   = 0
    last_state    = "GENERAL"
    last_state_at = None
    events        = []

    def fire(label, when, rsi, d_rsi, w_rsi, close, extra=""):
        events.append({
            "when":  when,
            "label": label,
            "close": close,
            "rsi":   rsi,
            "d":     d_rsi,
            "w":     w_rsi,
            "extra": extra,
        })

    def _live_d_rsi(rsi_engine, sym, price):
        d_calcs = rsi_engine._calculators.get(sym)
        if not d_calcs: return None
        calc = d_calcs.get("D")
        if calc is None or not calc.initialized or calc.rsi is None: return None
        closes = list(calc.closes)
        if not closes: return None
        diff = price - closes[-1]
        gain = max(0.0,  diff); loss = max(0.0, -diff)
        p = calc.period
        ag = (calc.avg_gain * (p - 1) + gain) / p
        al = (calc.avg_loss * (p - 1) + loss) / p
        if al == 0: return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    seen_count = [0]
    no_rsi_count = [0]
    sample_rsi = []

    def cb(candle, tf, rsi_engine, d_rsi, w_rsi):
        nonlocal state, entry_price, entry_time, entry_level
        if tf != sig_tf:
            return
        seen_count[0] += 1
        close = candle["close"]
        when  = candle["datetime"] + timedelta(minutes=int(sig_tf))
        rsi   = rsi_engine.get_rsi(SYMBOL, sig_tf)
        if rsi is None or d_rsi is None or w_rsi is None:
            no_rsi_count[0] += 1
            if len(sample_rsi) < 5:
                sample_rsi.append((when, close, rsi, d_rsi, w_rsi))
            return
        # Sample every 100th candle to see what's happening
        if seen_count[0] % 100 == 0 and len(sample_rsi) < 12:
            sample_rsi.append((when, close, rsi, d_rsi, w_rsi))

        # ── GENERAL ────────────────────────────────────────────────────
        if state == "GENERAL":
            # Path A: direct entry in band
            if b1_lo < rsi < b1_hi and d_rsi > d_buy and w_rsi > w_buy:
                state, entry_price, entry_time, entry_level = "BUY1_ACTIVE", close, when, 1
                fire("BUY 1 (direct)", when, rsi, d_rsi, w_rsi, close)
                return
            # Path B: flagged when RSI > b1_hi
            if rsi > b1_hi and d_rsi > d_buy and w_rsi > w_buy:
                state = "FLAGGED_BUY"
                fire("FLAGGED_BUY", when, rsi, d_rsi, w_rsi, close,
                     f"RSI {rsi:.2f} > {b1_hi}")
                return

        # ── FLAGGED_BUY ────────────────────────────────────────────────
        elif state == "FLAGGED_BUY":
            if rsi < b1_cd:
                state = "COOLING_BUY"
                fire("COOLING_BUY (from flagged)", when, rsi, d_rsi, w_rsi, close,
                     f"RSI {rsi:.2f} < cooldown {b1_cd}")
                return
            if b1_lo < rsi < b1_hi and d_rsi > d_buy and w_rsi > w_buy:
                state, entry_price, entry_time, entry_level = "BUY1_ACTIVE", close, when, 1
                fire("BUY 1 (from flagged)", when, rsi, d_rsi, w_rsi, close)
                return

        # ── BUY1_ACTIVE..BUY4_ACTIVE ───────────────────────────────────
        elif state.startswith("BUY") and state.endswith("_ACTIVE"):
            # Stoploss check using live D-RSI (matches new live code)
            live_d = _live_d_rsi(rsi_engine, SYMBOL, close)
            check_d = live_d if live_d is not None else d_rsi
            if check_d < sl_buy:
                fire(f"STOPLOSS EXIT {state}", when, rsi, check_d, w_rsi, close,
                     f"live D-RSI {check_d:.2f} < {sl_buy}")
                state = "COOLING_BUY_STOPLOSS"
                entry_price = None
                entry_time = None
                entry_level = 0
                return
            # Target check
            tgt_pct = t1_pct if entry_level == 1 else t23_pct
            target  = entry_price * (1.0 + tgt_pct / 100.0)
            if close >= target:
                fire(f"TARGET EXIT {state}", when, rsi, d_rsi, w_rsi, close,
                     f"close {close:.2f} >= target {target:.2f}")
                state = "COOLING_BUY"
                entry_price = None
                entry_time = None
                entry_level = 0
                return
            # Next level triggers
            if entry_level < 4:
                if entry_level == 1 and rsi < b2_trig and d_rsi > d_buy:
                    entry_level = 2; entry_price = close; entry_time = when
                    state = "BUY2_ACTIVE"
                    fire("BUY 2", when, rsi, d_rsi, w_rsi, close)
                elif entry_level >= 2:
                    drop = ((entry_price - close) / entry_price) * 100.0
                    if drop >= b3_drop and d_rsi > d_buy:
                        entry_level += 1; entry_price = close; entry_time = when
                        state = f"BUY{entry_level}_ACTIVE"
                        fire(f"BUY {entry_level}", when, rsi, d_rsi, w_rsi, close,
                             f"drop {drop:.2f}%")

        # ── COOLING_BUY ────────────────────────────────────────────────
        elif state == "COOLING_BUY":
            if rsi < cool_rsi:
                state = "GENERAL"
                fire("RELEASED COOLING -> GENERAL", when, rsi, d_rsi, w_rsi, close)

        # ── COOLING_BUY_STOPLOSS ───────────────────────────────────────
        elif state == "COOLING_BUY_STOPLOSS":
            if rsi > sl_cool:
                state = "GENERAL"
                fire("RELEASED SL-COOLING -> GENERAL", when, rsi, d_rsi, w_rsi, close)

    eng.replay(
        symbol             = SYMBOL,
        tfs                = [sig_tf],
        from_dt            = from_dt,
        to_dt              = to_dt,
        callback           = cb,
        seed_lookback_days = 60,
    )

    print()
    print(f"Candles processed in callback: {seen_count[0]}")
    print(f"  ... of which had a None RSI/D/W:  {no_rsi_count[0]}")
    print(f"  ... sample (None or every-100th):")
    for s in sample_rsi:
        when, close, rsi, d, w = s
        when_s = when.strftime("%Y-%m-%d %H:%M")
        rsi_s  = f"{rsi:.2f}" if rsi is not None else "None"
        d_s    = f"{d:.2f}"   if d   is not None else "None"
        w_s    = f"{w:.2f}"   if w   is not None else "None"
        print(f"    {when_s}  close={close:>9.2f}  rsi={rsi_s:>6}  d={d_s:>6}  w={w_s:>6}")
    print()
    print(f"Total transitions: {len(events)}")
    print()
    print(f"{'when':<20} {'event':<32} {'close':>9} {'15m-RSI':>8} "
          f"{'D-RSI':>8} {'W-RSI':>8}   notes")
    print("-" * 105)
    for e in events:
        when_s = e['when'].strftime('%Y-%m-%d %H:%M')
        print(f"{when_s:<20} {e['label']:<32} {e['close']:>9.2f} "
              f"{e['rsi']:>8.2f} {e['d']:>8.2f} {e['w']:>8.2f}   {e['extra']}")
    print()
    print(f"FINAL STATE: {state}")
    if entry_price:
        print(f"  Last entry: level={entry_level} price={entry_price:.2f} "
              f"time={entry_time}")


if __name__ == "__main__":
    main()
