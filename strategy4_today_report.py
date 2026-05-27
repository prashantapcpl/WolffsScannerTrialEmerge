"""
strategy4_today_report.py — Standalone report tool

Runs Strategy 4's state machine over today's 15m candles offline and writes:
  - data/strategy4_today_signals.csv      (every signal fired today, time-sorted)
  - data/strategy4_today_positions.csv    (stocks that would be active right now)

Does NOT touch the live scanner state file (data/scanner_4_state.json).
Safe to run while main.py is also running.
"""

import os
import sys
import csv
import json
from datetime import datetime
import pytz

# Force UTF-8 stdout on Windows so the existing modules' emoji prints don't
# crash with cp1252 UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.symbol_map import SymbolMapper
from core.data_store import DataStore
from core.rsi_engine import RSIEngine
from strategies.strategy4_momentum import (
    Strategy4Momentum, S4StateStore, S4WebhookSender,
)

IST = pytz.timezone("Asia/Kolkata")


def main():
    print("=" * 70)
    print("  STRATEGY 4 — TODAY'S SIGNALS REPORT")
    print("=" * 70)

    # ── Load config + dependencies ────────────────────────────────────────
    with open(os.path.join(ROOT, "config.json"), "r") as f:
        config = json.load(f)
    s4_cfg   = config.get("scanners", {}).get("scanner_4", {})
    settings = s4_cfg.get("settings", {})
    sig_tf   = str(settings.get("signal_timeframe", "15"))

    mapper      = SymbolMapper()
    data_store  = DataStore()
    all_symbols = mapper.get_all_fyers_symbols()

    print(f"  Stocks         : {len(all_symbols)}")
    print(f"  Signal TF      : {sig_tf}m")
    print(f"  Today (IST)    : {datetime.now(IST).strftime('%Y-%m-%d %H:%M')}")

    # ── Build RSI engine by FRESH REPLAY from CSV (NOT from rsi_cache) ────
    #
    # Why not the cache?  rsi_cache.pkl is rebuilt today at 15:31 after
    # market close.  After that, its engine state already contains today's
    # candles.  Seeding from it and then replaying today's candles
    # double-counts and produces garbage RSI values.  Fresh-replay from
    # the CSV is the only way to compute today's intraday RSI correctly.
    today_start = datetime.now(IST).replace(hour=9, minute=15,
                                            second=0, microsecond=0)
    rsi_engine  = RSIEngine(period=14)

    print("📊 Fresh-seeding RSI engine from CSV (pre-today candles only)...")
    seeded = 0
    for sym in all_symbols:
        # 15m: load every candle BEFORE today, seed engine from that
        pre = data_store.load_candles(sym, sig_tf, to_date=today_start)
        pre_closes = [c["close"] for c in pre if c["datetime"] < today_start]
        if len(pre_closes) > 14:
            rsi_engine.seed(sym, sig_tf, pre_closes[-200:])  # last 200 is plenty
            seeded += 1
        # Daily: from 5m aggregation (matches Fyers chart RSI better)
        try:
            d_candles = data_store.load_daily_from_5m(sym)
            d_pre = [c for c in d_candles
                     if c["datetime"].date() < today_start.date()]
            d_closes = [c["close"] for c in d_pre]
            if len(d_closes) > 14:
                rsi_engine.seed(sym, "D", d_closes[-200:])
        except Exception:
            pass
        # Weekly: use whatever's in the CSV
        try:
            w_candles = data_store.load_candles(sym, "W")
            w_pre = [c for c in w_candles
                     if c["datetime"].date() < today_start.date()]
            w_closes = [c["close"] for c in w_pre]
            if len(w_closes) > 14:
                rsi_engine.seed(sym, "W", w_closes[-200:])
        except Exception:
            pass
    print(f"   Seeded {seeded}/{len(all_symbols)} symbols on {sig_tf}m.")

    # ── Use a TEMP state file so we don't clobber the live scanner ────────
    tmp_state_file = "data/scanner_4_replay_state.json"
    tmp_state_path = os.path.join(ROOT, tmp_state_file)
    if os.path.exists(tmp_state_path):
        os.remove(tmp_state_path)
    state_store    = S4StateStore(tmp_state_file)

    # Webhook sender that never sends (silent mode is also enforced below)
    webhook_sender = S4WebhookSender()

    strategy = Strategy4Momentum(
        config         = settings,
        state_store    = state_store,
        webhook_sender = webhook_sender,
        rsi_engine     = rsi_engine,
        mapper         = mapper,
    )

    # ── Run carry-forward over today's candles ────────────────────────────
    today_open = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    print(f"\n🔄 Replaying 15m candles from {today_open.strftime('%H:%M')} ...")
    n_signals = strategy.run_carry_forward(
        data_store  = data_store,
        all_symbols = all_symbols,
        from_time   = today_open,
        silent      = True,
        quiet       = True,
    )

    log       = state_store.signal_log()
    positions = state_store.get_buy_active() + state_store.get_sell_active()
    flagged   = state_store.get_flagged()
    cooling   = state_store.get_cooling()

    print(f"\n✅ Replay complete: {n_signals} signal(s) fired today.\n")
    print(f"   🟢 Buy active   : {len(state_store.get_buy_active())}")
    print(f"   🔴 Sell active  : {len(state_store.get_sell_active())}")
    print(f"   🚩 Flagged      : {len(flagged)}")
    print(f"   ❄️ Cooling      : {len(cooling)}")

    # ── Print the signal table to console ─────────────────────────────────
    log_sorted = sorted(log, key=lambda e: e.get("time") or "")
    if log_sorted:
        print("\n" + "─" * 92)
        print(f"  {'TIME':<17} {'STOCK':<14} {'SIGNAL':<26} "
              f"{'PRICE':>10} {'RSI15':>7} {'RSId':>7} {'RSIw':>7}")
        print("─" * 92)
        for e in log_sorted:
            t  = (e.get("time") or "")[:16].replace("T", " ")
            sg = e.get("type", "")
            st = e.get("stock", "")
            pr = e.get("price")
            r1 = e.get("rsi_15m")
            rd = e.get("daily_rsi")
            rw = e.get("weekly_rsi")
            pr_s = f"₹{pr:>8.2f}" if isinstance(pr, (int, float)) else "-"
            r1_s = f"{r1:>6.2f}" if isinstance(r1, (int, float)) else "  -  "
            rd_s = f"{rd:>6.2f}" if isinstance(rd, (int, float)) else "  -  "
            rw_s = f"{rw:>6.2f}" if isinstance(rw, (int, float)) else "  -  "
            print(f"  {t:<17} {st:<14} {sg:<26} {pr_s:>10} {r1_s:>7} {rd_s:>7} {rw_s:>7}")
        print("─" * 92)
    else:
        print("\n(no signals today)")

    # ── Write CSV reports ─────────────────────────────────────────────────
    sig_csv = os.path.join(ROOT, "data", "strategy4_today_signals.csv")
    os.makedirs(os.path.dirname(sig_csv), exist_ok=True)
    with open(sig_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "stock", "symbol", "signal_type", "label",
                    "price", "rsi_15m", "daily_rsi", "weekly_rsi",
                    "target_price", "entry_price", "entry_level",
                    "pnl_pct", "state"])
        for e in log_sorted:
            w.writerow([
                e.get("time"),
                e.get("stock"),
                e.get("symbol"),
                e.get("type"),
                e.get("label"),
                e.get("price"),
                e.get("rsi_15m"),
                e.get("daily_rsi"),
                e.get("weekly_rsi"),
                e.get("target_price"),
                e.get("entry_price"),
                e.get("entry_level"),
                e.get("pnl_pct"),
                e.get("state"),
            ])
    print(f"\n📄 Signals CSV → {sig_csv}")

    pos_csv = os.path.join(ROOT, "data", "strategy4_today_positions.csv")
    with open(pos_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stock", "symbol", "side", "state", "entry_level",
                    "entry_price", "entry_time", "current_price",
                    "target_price", "pnl_pct"])
        for rec in positions:
            level = rec.entry_level
            if rec.state.startswith("BUY"):
                tp_pct = float(settings.get("buy1_target_pct", 5.0)) if level == 1 \
                         else float(settings.get("buy23_target_pct", 6.0))
                target = round((rec.entry_price or 0) * (1.0 + tp_pct / 100.0), 2)
                pnl    = round(((rec.current_price or 0) - (rec.entry_price or 0))
                               / (rec.entry_price or 1) * 100.0, 2) \
                         if rec.entry_price else 0
            else:
                tp_pct = float(settings.get("sell1_target_pct", 5.0)) if level == 1 \
                         else float(settings.get("sell23_target_pct", 6.0))
                target = round((rec.entry_price or 0) * (1.0 - tp_pct / 100.0), 2)
                pnl    = round(((rec.entry_price or 0) - (rec.current_price or 0))
                               / (rec.entry_price or 1) * 100.0, 2) \
                         if rec.entry_price else 0
            w.writerow([
                rec.plain_name, rec.symbol, rec.side, rec.state,
                level, rec.entry_price,
                rec.entry_time.isoformat() if rec.entry_time else None,
                rec.current_price, target, pnl,
            ])
    print(f"📄 Positions CSV → {pos_csv}")

    # Cleanup temp state
    try:
        os.remove(tmp_state_path)
    except Exception:
        pass

    print("\n✅ Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
