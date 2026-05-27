"""
trace_signal.py - End-to-end forensic for a specific (symbol, date, time)
against a specific scanner's current settings.

Usage:
  python tools/trace_signal.py SYMBOL DATE [TIME] [SCANNER_ID]

  SYMBOL      e.g.  TATACOMM | NSE:TATACOMM-EQ
  DATE        e.g.  2026-05-22
  TIME        e.g.  15:30   (optional; default scans the whole day)
  SCANNER_ID  e.g.  scanner_1 | scanner_2 | scanner_4   (default scanner_1)

Output sections:
  1. Scanner settings in use
  2. Raw CSV rows around the time
  3. Computed point-in-time RSI for each TF (5m/15m/60m/D/W)
  4. Condition matrix: each filter PASS/FAIL with actual numbers
  5. State file lookup: what's currently stored for this stock
  6. Discrepancy analysis: does state match what conditions imply?
"""
import os
import sys
import json
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.data_store    import DataStore
from core.replay_engine import ReplayEngine

IST = pytz.timezone("Asia/Kolkata")


def normalize_symbol(s: str) -> str:
    s = s.upper().strip()
    if s.startswith("NSE:"):
        return s
    if not s.endswith("-EQ"):
        s = s + "-EQ"
    return "NSE:" + s


def load_scanner_settings(scanner_id: str) -> dict:
    with open(os.path.join(ROOT, "config.json")) as f:
        cfg = json.load(f)
    sc = cfg.get("scanners", {}).get(scanner_id, {})
    return sc.get("settings", {}), sc.get("name", scanner_id)


def fmt_or_dash(v, fmt=".2f"):
    if v is None:
        return "  -"
    try:
        return format(float(v), fmt)
    except Exception:
        return str(v)


def print_section(title):
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    symbol_arg  = sys.argv[1]
    date_str    = sys.argv[2]
    # Smart 3rd arg: if it looks like HH:MM use as time, else as scanner_id
    time_str    = None
    scanner_id  = "scanner_1"
    if len(sys.argv) > 3:
        a3 = sys.argv[3]
        if a3.startswith("scanner_"):
            scanner_id = a3
        elif ":" in a3:
            time_str = a3
        else:
            scanner_id = a3   # bare "1"/"2"/"3"/"4" or full id
    if len(sys.argv) > 4:
        a4 = sys.argv[4]
        if a4.startswith("scanner_") or a4.isdigit():
            scanner_id = a4 if a4.startswith("scanner_") else f"scanner_{a4}"
        elif ":" in a4 and time_str is None:
            time_str = a4
    # Normalize plain "2" → "scanner_2"
    if scanner_id.isdigit():
        scanner_id = f"scanner_{scanner_id}"

    symbol = normalize_symbol(symbol_arg)
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        print(f"Bad date: {date_str}. Use YYYY-MM-DD.")
        sys.exit(1)

    target_time = None
    if time_str:
        try:
            t = datetime.strptime(time_str, "%H:%M").time()
            target_time = IST.localize(datetime.combine(target_date, t))
        except Exception:
            print(f"Bad time: {time_str}. Use HH:MM.")
            sys.exit(1)

    # ── Section 1: Settings ───────────────────────────────────────────
    settings, scanner_name = load_scanner_settings(scanner_id)
    print_section(f"1. Settings for {scanner_id} ({scanner_name})")
    if not settings:
        print(f"  No settings found for {scanner_id}")
        sys.exit(1)
    important_keys = [
        "scan_timeframe", "trigger_timeframe", "exit_timeframe",
        "signal_timeframe",
        "rsi_entry_threshold", "rsi_reset_threshold", "rsi_exit_threshold",
        "drop_percent",
        "rsi_entry_sell_threshold", "rsi_reset_sell_threshold",
        "rsi_exit_sell_threshold", "rise_percent",
        "daily_rsi_threshold", "weekly_rsi_threshold",
        "daily_rsi_threshold_sell", "weekly_rsi_threshold_sell",
        "daily_rsi_stoploss_buy", "daily_rsi_stoploss_sell",
        "buy1_rsi_lower", "buy1_rsi_upper", "buy1_cooldown_rsi",
        "buy2_rsi_trigger", "buy3_drop_pct",
        "daily_rsi_buy_filter", "weekly_rsi_buy_filter",
        "sell1_rsi_lower", "sell1_rsi_upper", "sell1_cooldown_rsi",
        "sell2_rsi_trigger", "sell3_rise_pct",
        "daily_rsi_sell_filter", "weekly_rsi_sell_filter",
    ]
    for k in important_keys:
        if k in settings:
            print(f"  {k:<28} = {settings[k]}")

    # Decide which scan_tf to use for the trace
    scan_tf = str(settings.get("scan_timeframe")
                  or settings.get("signal_timeframe")
                  or "5")

    # ── Section 2: Raw CSV around the time ────────────────────────────
    print_section(f"2. Raw 5m CSV rows for {symbol} on {target_date}")
    ds = DataStore()
    candles = ds.load_candles(symbol, "5")
    same_day = [c for c in candles if c["datetime"].date() == target_date]
    if not same_day:
        print(f"  No 5m candles found on {target_date}")
    else:
        if target_time:
            # show 30 min window around target
            window_start = target_time - timedelta(minutes=15)
            window_end   = target_time + timedelta(minutes=20)
            shown = [c for c in same_day
                     if window_start <= IST.localize(
                         c["datetime"].replace(tzinfo=None)) <= window_end]
        else:
            shown = same_day
        print(f"  ({len(shown)} of {len(same_day)} rows shown)")
        print(f"  {'time':<20} {'open':>9} {'high':>9} {'low':>9} "
              f"{'close':>9} {'volume':>12}")
        for c in shown[-25:]:
            print(f"  {c['datetime'].strftime('%Y-%m-%d %H:%M'):<20} "
                  f"{c['open']:>9.2f} {c['high']:>9.2f} {c['low']:>9.2f} "
                  f"{c['close']:>9.2f} {int(c['volume']):>12}")

    # ── Section 3: Compute RSI via ReplayEngine ───────────────────────
    print_section(f"3. Computed RSI series around target")
    eng = ReplayEngine(ds, rsi_period=14)

    # Replay window
    from_dt = IST.localize(datetime.combine(target_date - timedelta(days=2),
                                             datetime.min.time()).replace(hour=9, minute=15))
    to_dt   = IST.localize(datetime.combine(target_date + timedelta(days=1),
                                             datetime.min.time()).replace(hour=9, minute=15))

    rows = []
    def cb(candle, tf, rsi_engine, d_rsi, w_rsi, scan_tf=scan_tf):
        if tf != scan_tf:
            return
        when = candle["datetime"] + timedelta(minutes=int(scan_tf))
        if when.date() != target_date:
            return
        rsi = rsi_engine.get_rsi(symbol, scan_tf)
        rows.append((when, candle["close"], rsi, d_rsi, w_rsi))

    eng.replay(symbol=symbol, tfs=[scan_tf], from_dt=from_dt, to_dt=to_dt,
               callback=cb, seed_lookback_days=60)

    if not rows:
        print(f"  No replay data produced for {target_date}")
    else:
        if target_time:
            window_start = target_time - timedelta(minutes=15)
            window_end   = target_time + timedelta(minutes=20)
            shown = [r for r in rows
                     if window_start <= IST.localize(
                         r[0].replace(tzinfo=None)) <= window_end]
        else:
            shown = rows[-25:]
        print(f"  {'close time':<20} {'close':>9} {'RSI_'+scan_tf+'m':>9} "
              f"{'D-RSI':>8} {'W-RSI':>8}")
        for when, close, rsi, d, w in shown:
            print(f"  {when.strftime('%Y-%m-%d %H:%M'):<20} "
                  f"{close:>9.2f} {fmt_or_dash(rsi):>9} "
                  f"{fmt_or_dash(d):>8} {fmt_or_dash(w):>8}")

    # ── Section 4: Condition matrix at target_time ────────────────────
    print_section("4. Condition matrix at target time")
    target_row = None
    if rows and target_time:
        # find closest row to target_time
        for r in rows:
            if r[0].replace(tzinfo=None) == target_time.replace(tzinfo=None):
                target_row = r
                break
        if target_row is None:
            # fallback: any row within ±5min
            for r in rows:
                if abs((r[0].replace(tzinfo=None) - target_time.replace(tzinfo=None)).total_seconds()) <= 300:
                    target_row = r
                    break
    elif rows and not target_time:
        # use last row of day
        target_row = rows[-1] if rows else None

    if target_row:
        when, close, rsi, d_rsi, w_rsi = target_row
        print(f"  At {when.strftime('%Y-%m-%d %H:%M')}: close=Rs.{close:.2f}")
        print(f"  RSI({scan_tf}m)={fmt_or_dash(rsi)}  D-RSI={fmt_or_dash(d_rsi)}  "
              f"W-RSI={fmt_or_dash(w_rsi)}")
        print()
        # Buy-side conditions
        if scanner_id in ("scanner_1", "scanner_2"):
            entry  = settings.get("rsi_entry_threshold")
            d_thr  = settings.get("daily_rsi_threshold")
            w_thr  = settings.get("weekly_rsi_threshold")
            drop_p = settings.get("drop_percent")
            print("  BUY-side conditions (Scanner 1/2):")
            if entry is not None and rsi is not None:
                ok = float(rsi) < float(entry)
                print(f"    RSI({scan_tf}m) {rsi:.2f} < {entry} ? "
                      f"{'PASS' if ok else 'FAIL'}")
            if d_thr is not None and d_rsi is not None:
                ok = float(d_rsi) > float(d_thr)
                print(f"    D-RSI {d_rsi:.2f} > {d_thr} ? "
                      f"{'PASS' if ok else 'FAIL'}")
            if w_thr is not None and w_rsi is not None:
                ok = float(w_rsi) > float(w_thr)
                print(f"    W-RSI {w_rsi:.2f} > {w_thr} ? "
                      f"{'PASS' if ok else 'FAIL'}")
        elif scanner_id == "scanner_4":
            b1lo = settings.get("buy1_rsi_lower")
            b1hi = settings.get("buy1_rsi_upper")
            d_thr= settings.get("daily_rsi_buy_filter")
            w_thr= settings.get("weekly_rsi_buy_filter")
            print("  BUY-side conditions (Scanner 4):")
            if b1lo is not None and b1hi is not None and rsi is not None:
                ok = float(b1lo) < float(rsi) < float(b1hi)
                print(f"    {b1lo} < RSI({scan_tf}m) {rsi:.2f} < {b1hi} ? "
                      f"{'PASS' if ok else 'FAIL (Path A direct entry)'}")
                ok2 = float(rsi) > float(b1hi)
                print(f"    RSI({scan_tf}m) {rsi:.2f} > {b1hi} ? "
                      f"{'PASS (Path B flag)' if ok2 else 'FAIL (no flag)'}")
            if d_thr is not None and d_rsi is not None:
                ok = float(d_rsi) > float(d_thr)
                print(f"    D-RSI {d_rsi:.2f} > {d_thr} ? "
                      f"{'PASS' if ok else 'FAIL'}")
            if w_thr is not None and w_rsi is not None:
                ok = float(w_rsi) > float(w_thr)
                print(f"    W-RSI {w_rsi:.2f} > {w_thr} ? "
                      f"{'PASS' if ok else 'FAIL'}")
    else:
        print("  No replay row found near target time")

    # ── Section 5: State file lookup ──────────────────────────────────
    print_section(f"5. State file lookup ({scanner_id})")
    state_path = os.path.join(ROOT, "data", f"{scanner_id}_state.json")
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        rec = state.get("records", {}).get(symbol)
        if rec:
            for k in ["state", "side", "watched_at", "reference_price",
                      "reference_time", "rsi_at_watch", "buy_time",
                      "buy_price", "drop_pct_at_buy", "current_price",
                      "exit_time", "exit_price", "gap_fill"]:
                v = rec.get(k)
                if v is not None and v != []:
                    print(f"  {k:<18} = {v}")
        else:
            print(f"  No record for {symbol} in {scanner_id} state file")

        # Signal log entries for this symbol
        sig = [s for s in state.get("signal_log", [])
               if s.get("symbol") == symbol
               or s.get("plain_name") == symbol.replace("NSE:","").replace("-EQ","")]
        if sig:
            print(f"\n  Signal log entries ({len(sig)} total, showing last 5):")
            for s in sig[-5:]:
                print(f"    {s.get('time')}  {s.get('type','?'):<10}  "
                      f"price={s.get('price') or s.get('buy_price')}")
    else:
        print(f"  State file not found: {state_path}")

    # ── Section 6: Discrepancy summary ────────────────────────────────
    print_section("6. Discrepancy analysis")
    if target_row and os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
        rec = state.get("records", {}).get(symbol)
        if rec:
            when, close, rsi, d_rsi, w_rsi = target_row
            stored_rsi = rec.get("rsi_at_watch")
            stored_ref = rec.get("reference_price")
            if stored_rsi is not None and rsi is not None:
                if abs(float(stored_rsi) - float(rsi)) > 1.0:
                    print(f"  [WARN] state rsi_at_watch={stored_rsi} but clean "
                          f"replay says RSI={rsi:.2f} (diff {abs(float(stored_rsi)-float(rsi)):.2f})")
                    print(f"         → state was likely set against corrupted data")
                else:
                    print(f"  rsi_at_watch matches replay within 1 point [ok]")
            if stored_ref is not None and abs(float(stored_ref) - close) > close * 0.05:
                print(f"  [WARN] state reference_price={stored_ref} but CSV close "
                      f"at that time was {close:.2f} ({abs(float(stored_ref)-close)/close*100:.1f}% diff)")
        else:
            print(f"  No state record. If clean replay shows conditions PASS, "
                  f"a restart with current settings should create the position.")
    print()


if __name__ == "__main__":
    main()
