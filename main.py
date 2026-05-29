"""
main.py — Fyers RSI Scanner (Multi-Scanner)

Runs multiple independent scanners simultaneously.
Each scanner has its own settings, state and webhooks.
"""

import os
import sys

# ── Force UTF-8 on stdout/stderr (Windows cp1252 chokes on emoji prints
#    when output is captured or redirected). Idempotent on Linux/macOS.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.fyers_auth      import get_fyers_client
from core.symbol_map      import SymbolMapper
from core.candle_engine   import CandleEngine
from core.data_store      import DataStore
from core.data_feed       import DataFeed
from core.history_manager import HistoryManager
from core.rsi_cache       import get_rsi_cache
from core.price_store     import get_price_store
from core.stock_groups    import get_stock_groups
from scanner_manager      import ScannerManager
from core.rsi_engine      import RSIEngine
from strategies.strategy3_webhook import (
    Strategy3WebhookMirror, S3StateStore, S3WebhookSender
)
from strategies.strategy4_momentum import (
    Strategy4Momentum, S4StateStore, S4WebhookSender
)
from signals.webhook_server import WebhookServer

IST         = pytz.timezone("Asia/Kolkata")
RESCAN_FLAG = os.path.join(ROOT, "data", "rescan.flag")
RESET_FLAG_GLOB = "reset_*.flag"   # under data/
HISTORY_TFS = ["5", "10", "15", "30", "60", "D", "W"]
TIMEFRAMES  = ["1", "2", "5", "10", "15", "30", "60", "D", "W"]


def load_config():
    with open(os.path.join(ROOT, "config.json"), "r") as f:
        return json.load(f)


# ─── Per-symbol last-candle tracker (used by gap healer) ────────────────────────
# symbol → timeframe → last candle close_time (tz-aware IST)
# Populated at startup from CSVs and updated live on every candle close.
_last_candle_time: dict = {}
# symbol → last heal-attempt POSIX timestamp (throttle repeated API calls)
_heal_last_attempt: dict = {}

# ─── Global instances ───────────────────────────────────────────────────────────
rsi_cache   = get_rsi_cache()
data_store  = DataStore()
mapper      = SymbolMapper()
price_store = get_price_store()
all_symbols= mapper.get_all_fyers_symbols()

print(f"\n{'='*60}")
print("  FYERS RSI SCANNER — Multi-Scanner")
print(f"  Stocks: {len(all_symbols)}")
print(f"{'='*60}\n")

# Global feed + scanner manager — set in main() / startup_pipeline()
data_feed  = None
scan_mgr   = None

# Strategy 3 — Webhook Mirror (independent of scan_mgr)
strategy3       = None
_webhook_server = None

# Strategy 4 — RSI Momentum (independent of scan_mgr)
strategy4       = None


# ─── Build RSI cache ────────────────────────────────────────────────────────────
def build_rsi_cache():
    print("\n🔧 Building RSI cache...")
    rsi_cache.build(
        symbols    = all_symbols,
        timeframes = HISTORY_TFS,
        data_store = data_store,
        rsi_period = 14
    )
    print("✅ RSI cache built.\n")


# Global runtime monitors (set up in main())
heartbeat            = None
tick_freshness_mon   = None
tick_sanity_validator = None
subscription_tracker = None


# ─── Startup pipeline ───────────────────────────────────────────────────────────
def startup_pipeline(fyers_client):
    global scan_mgr

    # Step 0: Environment / system checks — fail fast on critical issues
    from core.env_checks import run_startup_checks
    env = run_startup_checks(verbose=True)
    if not env["instance_lock"]:
        print("❌ Another scanner instance is already running. Exiting.")
        sys.exit(1)
    if not env["disk_writable"]:
        print("❌ data/ directory is not writable. Cannot continue.")
        sys.exit(1)
    if not env["token"]["ok"]:
        print("⚠️  Fyers token expired or missing — live feed will fail. "
              "Run login flow.")
        # Don't exit; allow historical work to continue
    if not env["calendar"]["ok"]:
        print(f"⚠️  NSE holiday calendar only extends "
              f"{env['calendar']['days_ahead']} days ahead. "
              f"Add upcoming year's holidays to core/market_calendar.py.")

    hist_mgr = HistoryManager(fyers_client)

    # Step 1: Load RSI cache before history fetch (needed for rebuild decision).
    cache_loaded = rsi_cache.load()

    # Step 2: Fetch / gap-fill history.
    # update_all() is fast after close: it reads only the last CSV line per symbol
    # and skips API calls for symbols whose data is already complete (last candle
    # >= 15:10 today). Symbols with gaps (e.g. KARURVYSYA 09:15–14:45 missing)
    # still get fetched and merged. Never skip this — gaps must be healed every run.
    print("📥 Checking for history gaps...")
    try:
        update_result = hist_mgr.update_all(
            symbols    = all_symbols,
            timeframes = HISTORY_TFS,
            force_full = False
        )
        new_candles = update_result.get("new_candles", 0) if update_result else 0
    except Exception as e:
        print(f"   ⚠️  History update error: {e}")
        new_candles = 0
    if new_candles:
        print(f"   History update complete: {new_candles} new candles added.")

    # Step 2b: Checker bot — catches anything update_all() skipped due to
    # timing heuristics (before_open guard, same-day threshold, etc.).
    # Runs a cohort-based staleness check and force-fetches stale symbols.
    checker_fixed = 0
    try:
        checker_result = hist_mgr.run_checker_bot(
            symbols    = all_symbols,
            timeframes = HISTORY_TFS,
        )
        checker_fixed = checker_result.get("fixed", 0)
    except Exception as e:
        print(f"   ⚠️  Checker bot error: {e}")

    # Step 2c1: Extended diagnostic validators -- comprehensive pass that
    # reports every category of data anomaly (frozen OHLC, volume spikes,
    # state-file inconsistencies, settings conflicts, etc.). Diagnostic
    # only; the auto-fix happens in the integrity pass below.
    try:
        from core.validators_extended import run_all_validators
        run_all_validators(all_symbols, lookback_days=30, verbose=True)
    except Exception as e:
        print(f"   ⚠️  Extended validator error: {e}")

    # Step 2c: DATA INTEGRITY PASS -- complement to the checker bot.
    # Checker bot only validates "latest date is recent"; this validates
    # ROW COUNT per trading day per (symbol, tf). Catches mid-series gaps
    # like the 22-May 09:15-10:30 morning hole that broke ENRIN's RSI.
    # Also removes OHLCV-frozen phantom runs left by websocket disconnects.
    #
    # SKIP CACHE: this pass is expensive (~30 min) and rarely changes
    # within the same trading day. Set DATA_INTEGRITY_MAX_AGE_HOURS to a
    # positive number to skip on quick same-day restarts where the
    # previous run completed cleanly (final_unfixable == 0).
    from core.run_cache import RunCache
    _di_cache    = RunCache("data_integrity")
    _di_max_age  = float(os.environ.get("DATA_INTEGRITY_MAX_AGE_HOURS", "0"))
    _di_fresh, _di_prev = _di_cache.is_fresh(_di_max_age)
    _di_prev_clean = (_di_prev or {}).get("payload", {}).get("final_unfixable", -1) == 0
    if _di_fresh and _di_prev_clean:
        prev_pl = _di_prev["payload"]
        print(f"⚡ Data integrity pass cached — ran {_di_prev['ran_at']} "
              f"(fetched {prev_pl.get('fetched', 0)} candles, "
              f"0 unfixable). Skipping.")
        print("   To force a re-run: clear data/run_cache/data_integrity.json "
              "or unset DATA_INTEGRITY_MAX_AGE_HOURS.\n")
    else:
        try:
            from core.data_integrity import run_full_integrity_pass
            integrity = run_full_integrity_pass(
                symbols         = all_symbols,
                history_manager = hist_mgr,
                data_store      = data_store,
                lookback_days   = 30,
            )
            if integrity.get("fetched", 0) > 0:
                # Treat the gap-fill candles as "new candles" so the rsi_cache
                # rebuild branch fires below -- otherwise the cache stays stale.
                new_candles += integrity["fetched"]
            # Only cache if the pass left no unfixable gaps; otherwise we
            # want next startup to retry.
            if integrity.get("final_unfixable", 0) == 0:
                _di_cache.mark_success({
                    "lookback_days":    30,
                    "fetched":          integrity.get("fetched", 0),
                    "phantoms_removed": integrity.get("phantoms_removed", 0),
                    "final_unfixable":  0,
                })
        except Exception as e:
            import traceback
            print(f"   ⚠️  Data integrity pass error: {e}")
            traceback.print_exc()

    # Step 3: Load or build RSI cache.
    # If the cache was built today AFTER market close (15:31+), it already
    # contains the full day's data. Skip rebuild even if a few incremental
    # candles trickled in — those are settlement candles or low-liquidity
    # fills that don't affect carry-forward or live-feed RSI seeding.
    cache_is_post_close = False
    if rsi_cache._built_at:
        try:
            built = datetime.fromisoformat(rsi_cache._built_at)
            cache_is_post_close = built.hour > 15 or (built.hour == 15 and built.minute >= 31)
        except Exception:
            pass

    if not cache_loaded:
        reason = "no cache on disk"
    elif not rsi_cache.is_fresh():
        reason = "cache is from a previous date"
    elif new_candles > 0 and not cache_is_post_close:
        reason = f"{new_candles} new candles fetched today"
    elif checker_fixed > 0:
        reason = f"checker bot recovered data for {checker_fixed} symbol×TF combos"
    else:
        reason = None

    if reason:
        print(f"\n📊 Building RSI cache ({reason})...")
        build_rsi_cache()
    else:
        print("✅ RSI cache up-to-date — skipping rebuild.\n")

    # Step 3: Initialize scanner manager
    print("📊 Initializing scanners...")
    scan_mgr = ScannerManager(rsi_cache, mapper, all_symbols)

    # Step 4: Seed RSI for all active scanners
    scan_mgr.seed_all_rsi(HISTORY_TFS)

    # Step 4b: Reset ALL records to GENERAL across active scanners so the
    # subsequent carry-forward re-derives state from clean RSI history
    # under CURRENT settings. Without this, pre-existing ACTIVE positions
    # from prior runs (potentially derived from corrupted/phantom data)
    # survive untouched. Signal log is untouched (immutable audit trail).
    #
    # SAFETY: this is the historically-risky step. If carry-forward
    # doesn't perfectly re-derive what was reset, active positions vanish
    # silently. We now (a) back up every scanner's state file BEFORE any
    # mutation, (b) count positions pre/post, (c) warn loudly if there's a
    # large drop, (d) honour CARRY_FORWARD_DRY_RUN=true to skip mutation
    # for inspection runs.
    from core.state_store import StockState

    _cf_dry_run = os.environ.get(
        "CARRY_FORWARD_DRY_RUN", "").lower() in ("true", "1", "yes")
    _cf_backup_dir = os.path.join(
        ROOT, "data", "state_backups",
        datetime.now(IST).strftime("%Y%m%d_%H%M%S"))

    # Backup every active scanner's state BEFORE touching anything.
    _backed_up = []
    for scanner in scan_mgr.get_active():
        try:
            path = scanner.state_store.backup_to(_cf_backup_dir)
            if path:
                _backed_up.append((scanner.name, path))
        except Exception as e:
            print(f"   ⚠️  Could not back up {scanner.name} state: {e}")
    if _backed_up:
        print("   📦 State backed up for carry-forward safety:")
        for nm, p in _backed_up:
            print(f"      [{nm}]  {p}")
    else:
        print("   ℹ️  No prior state files to back up (first run).")

    # Snapshot pre-reset position counts (active + watched, both sides).
    def _live_count(ss):
        s = ss.summary()
        return s["active"] + s["watched"] + s["active_sell"] + s["watched_sell"]
    _pre_counts = {sc.name: _live_count(sc.state_store)
                   for sc in scan_mgr.get_active()}
    _pre_total  = sum(_pre_counts.values())

    if _cf_dry_run:
        print("\n   🧪 CARRY_FORWARD_DRY_RUN=true → counting what WOULD be reset, "
              "no mutation.")
        for scanner in scan_mgr.get_active():
            would_reset = sum(1 for r in scanner.state_store.all_records()
                              if r.state != StockState.GENERAL)
            print(f"      [{scanner.name}] would reset {would_reset} non-GENERAL "
                  f"records; currently has {_pre_counts[scanner.name]} live positions.")
        print(f"      Total live positions across active scanners: {_pre_total}\n")
        print("   Exiting (dry-run). Unset CARRY_FORWARD_DRY_RUN to apply.\n")
        sys.exit(0)

    for scanner in scan_mgr.get_active():
        reset_n = 0
        for rec in scanner.state_store.all_records():
            if rec.state == StockState.GENERAL:
                continue
            rec.state           = StockState.GENERAL
            rec.side            = None
            rec.watched_at      = None
            rec.reference_price = None
            rec.reference_time  = None
            rec.rsi_at_watch    = None
            rec.buy_signal_at   = None
            rec.buy_price       = None
            rec.buy_time        = None
            rec.drop_pct_at_buy = None
            rec.avg_entries     = []
            rec.last_avg_price  = None
            rec.last_avg_time   = None
            rec.avg_count       = 0
            rec.exit_signal_at  = None
            rec.exit_price      = None
            rec.exit_time       = None
            rec.rsi_at_exit     = None
            rec.gap_fill        = False
            reset_n += 1
        scanner.state_store.save()
        print(f"   [{scanner.name}] Reset {reset_n} records to GENERAL "
              f"for clean carry-forward.")

    # Step 5: Carry-forward for all active scanners
    scan_mgr.run_all_carry_forward()

    # Safety check: did positions vanish? Warn loudly so the operator can
    # decide to restore from backup.
    _post_counts = {sc.name: _live_count(sc.state_store)
                    for sc in scan_mgr.get_active()}
    _post_total  = sum(_post_counts.values())
    _drop        = _pre_total - _post_total
    if _pre_total > 0 and _drop > 0:
        msg_level = "⚠️" if _drop > _pre_total * 0.5 else "ℹ️"
        print(f"\n   {msg_level}  Carry-forward position-count diff: "
              f"{_pre_total} → {_post_total} ({_drop:+d}).")
        for nm in _pre_counts:
            d = _pre_counts[nm] - _post_counts[nm]
            if d:
                print(f"      [{nm}]  {_pre_counts[nm]} → {_post_counts[nm]} "
                      f"({d:+d})")
        if _drop > _pre_total * 0.5:
            print("\n   ⚠️  More than 50% of positions vanished. If this looks wrong:")
            print("      1) Stop the scanner (Ctrl+C).")
            print(f"      2) Restore: python tools/restore_state.py "
                  f"--from {_cf_backup_dir}")
            print("      3) Re-run with CARRY_FORWARD_DRY_RUN=true to inspect.\n")

    # Step 6: Reset previous-day exits.
    # check_market_events() pre-seeds last_open_date=today when startup is after 9:15,
    # so on_market_open never fires for that day — EXITED stocks from yesterday would
    # stay stuck and never become WATCHED again. Fix: reset them here directly.
    _now   = datetime.now(IST)
    _today = _now.date()
    if _now.hour > 9 or (_now.hour == 9 and _now.minute >= 15):
        for scanner in scan_mgr.get_active():
            _reset_prev = 0
            for _rec in list(scanner.state_store.get_exited()):
                if _rec.exit_time is None or _rec.exit_time.date() < _today:
                    scanner.state_store.reset_to_general(
                        _rec.symbol,
                        reason="Previous day exit — startup after market open")
                    _reset_prev += 1
            if _reset_prev:
                print(f"   🌅 {scanner.name}: {_reset_prev} previous-day exits reset")

    # Seed the gap-healer tracker from the just-loaded CSVs.
    # Only fills in symbols/TFs that haven't received a live candle yet.
    _init_candle_tracker()

    # Strategy 4 — RSI Momentum (needs rsi_cache built first)
    start_strategy4()

    # Process any reset flags dropped while the scanner was off
    _process_reset_flags()

    print("\n✅ Startup pipeline complete.\n")
    print(f"   Active scanners: {[s.name for s in scan_mgr.get_active()]}")


# ─── Candle tracker init ────────────────────────────────────────────────────────
def _init_candle_tracker():
    """
    Seed _last_candle_time from CSV last-stored dates after history is loaded.
    Skips any symbol/tf already updated by a live candle that arrived during startup.
    """
    intraday = [tf for tf in HISTORY_TFS if tf not in ("D", "W")]
    for sym in all_symbols:
        _last_candle_time.setdefault(sym, {})
        for tf in intraday:
            if tf not in _last_candle_time[sym]:
                last = data_store.get_last_stored_date(sym, tf)
                if last:
                    _last_candle_time[sym][tf] = last


# ─── Candle close handler ───────────────────────────────────────────────────────
def on_candle_close(candle):
    timeframe   = candle.timeframe
    close_price = candle.close

    # Save to local store — always, even if scan_mgr not ready yet
    if timeframe in HISTORY_TFS:
        now = candle.open_time
        if now:
            h = now.hour
            m = now.minute
            is_market = (
                (h == 9 and m >= 15) or
                (10 <= h <= 14) or
                (h == 15 and m < 30)   # m<30: exclude closing-auction candle (open@15:30)
            )
            if is_market:
                data_store.save_candles(candle.symbol, timeframe, [{
                    "datetime": candle.open_time,
                    "open":  candle.open,
                    "high":  candle.high,
                    "low":   candle.low,
                    "close": candle.close,
                    "volume":candle.volume,
                }], append=True)

    # Track last candle close per symbol/tf for gap healer
    if timeframe not in ("D", "W") and candle.close_time:
        _last_candle_time.setdefault(candle.symbol, {})[timeframe] = candle.close_time

    # Scanner not ready yet — skip signal routing
    if scan_mgr is None:
        return

    # Route to all active scanners — intraday candles only during market hours
    # Use candle.close_time (not datetime.now) so catch-up/buffered pre-market
    # candles are always rejected regardless of when the scanner processes them.
    if timeframe not in ("D", "W"):
        h, m = candle.close_time.hour, candle.close_time.minute
        if not ((h == 9 and m >= 15) or (10 <= h <= 14) or (h == 15 and m <= 30)):
            return

    scan_mgr.on_candle_close(candle, timeframe)

    # Strategy 3 — independent routing
    if strategy3:
        strategy3.on_candle_close(candle, timeframe)

    # Strategy 4 — independent routing
    if strategy4:
        try:
            strategy4.on_candle_close(candle, timeframe)
        except Exception as e:
            print(f"⚠️  [scanner_4] {candle.symbol}: {e}")


candle_engine = CandleEngine(
    timeframes      = TIMEFRAMES,
    on_candle_close = on_candle_close
)


# ─── Tick handler ───────────────────────────────────────────────────────────────
def on_tick(symbol: str, price: float, volume: int, tick_time):
    # Runtime sanity: reject bad ticks before they enter the candle engine
    if tick_sanity_validator is not None:
        ok, reason = tick_sanity_validator.validate(symbol, price, tick_time)
        if not ok:
            # Rejected; don't aggregate. Counter is in the validator.
            return
    # Track freshness + subscription delivery
    if tick_freshness_mon is not None:
        tick_freshness_mon.on_tick(symbol)
    if subscription_tracker is not None:
        subscription_tracker.on_tick(symbol)

    candle_engine.process_tick(symbol, price, volume, tick_time)
    price_store.update(symbol, price)
    if scan_mgr:
        scan_mgr.on_tick(symbol, price)
    if strategy3:
        strategy3.on_tick(symbol, price)
    if strategy4:
        strategy4.on_tick(symbol, price)


# ─── Market events ──────────────────────────────────────────────────────────────
def check_market_events():
    # Pre-seed dates to today if we're already past each trigger time.
    # Prevents redundant market-open/close/cache events firing on startup.
    _now   = datetime.now(IST)
    _today = _now.date()
    last_open_date  = _today if (_now.hour > 9  or (_now.hour == 9  and _now.minute >= 15)) else None
    last_close_date = _today if (_now.hour > 15 or (_now.hour == 15 and _now.minute >= 30)) else None
    last_cache_date = _today if (_now.hour > 15 or (_now.hour == 15 and _now.minute >= 31)) else None

    while True:
        now        = datetime.now(IST)
        today      = now.date()
        weekday    = now.weekday() <= 4
        open_time  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
        close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
        cache_time = now.replace(hour=15, minute=31, second=0, microsecond=0)

        if weekday and now >= open_time and last_open_date != today:
            last_open_date = today
            print(f"\n🌅 MARKET OPEN — {now.strftime('%H:%M:%S')}")
            if scan_mgr:
                scan_mgr.on_market_open()

        if weekday and now >= close_time and last_close_date != today:
            last_close_date = today
            print(f"\n🔔 MARKET CLOSED — {now.strftime('%H:%M:%S')}")
            if scan_mgr:
                scan_mgr.on_market_close()
                print(f"   Summary: {scan_mgr.summary()}")

        if weekday and now >= cache_time and last_cache_date != today:
            last_cache_date = today
            # End-of-day routine (15:31 IST):
            #   1. Data-integrity pass (heal any intraday gaps formed during
            #      the day -- network blips, restart windows, etc.)
            #   2. Rebuild RSI cache from the healed CSVs
            #   3. RECONCILE SCANNER STATE: reset all records to GENERAL +
            #      re-run carry-forward against the rebuilt cache. This
            #      catches any signals the live path missed (e.g. AVALON
            #      13:25 dropped to RSI 4.65 today but watch never fired
            #      because the scanner was restarting at that moment).
            print("\n🔧 End-of-day: integrity + cache rebuild + state reconcile...")
            def _eod_routine():
                # Step 1: integrity pass
                try:
                    from core.data_integrity import run_full_integrity_pass
                    from core.fyers_auth     import get_fyers_client
                    from core.history_manager import HistoryManager
                    client = get_fyers_client()
                    if client:
                        hm = HistoryManager(client)
                        run_full_integrity_pass(
                            symbols         = all_symbols,
                            history_manager = hm,
                            data_store      = data_store,
                            lookback_days   = 3,
                        )
                    else:
                        print("   ⚠️  No Fyers client for EOD integrity pass")
                except Exception as e:
                    import traceback
                    print(f"   ⚠️  EOD integrity error: {e}")
                    traceback.print_exc()

                # Step 2: rebuild RSI cache from healed CSVs
                try:
                    build_rsi_cache()
                except Exception as e:
                    import traceback
                    print(f"   ⚠️  EOD cache rebuild error: {e}")
                    traceback.print_exc()

                # Step 3: reconcile state across all active scanners
                try:
                    if scan_mgr:
                        from core.state_store import StockState
                        print("🔄 EOD state reconcile: reset + carry-forward...")
                        for scanner in scan_mgr.get_active():
                            n = 0
                            for rec in scanner.state_store.all_records():
                                if rec.state == StockState.GENERAL:
                                    continue
                                rec.state           = StockState.GENERAL
                                rec.side            = None
                                rec.watched_at      = None
                                rec.reference_price = None
                                rec.reference_time  = None
                                rec.rsi_at_watch    = None
                                rec.buy_signal_at   = None
                                rec.buy_price       = None
                                rec.buy_time        = None
                                rec.drop_pct_at_buy = None
                                rec.avg_entries     = []
                                rec.last_avg_price  = None
                                rec.last_avg_time   = None
                                rec.avg_count       = 0
                                rec.exit_signal_at  = None
                                rec.exit_price      = None
                                rec.exit_time       = None
                                rec.rsi_at_exit     = None
                                rec.gap_fill        = False
                                n += 1
                            scanner.state_store.save()
                            print(f"   [{scanner.name}] reset {n} records.")
                        # Seed engines from the rebuilt cache, then carry-forward
                        scan_mgr.seed_all_rsi(HISTORY_TFS)
                        scan_mgr.run_all_carry_forward()
                        # Scanner 4 reconcile
                        if strategy4:
                            try:
                                _rescan_scanner_4(load_config())
                            except Exception as e:
                                print(f"   ⚠️  S4 EOD reconcile error: {e}")
                        print("✅ EOD state reconcile complete.\n")
                except Exception as e:
                    import traceback
                    print(f"   ⚠️  EOD state reconcile error: {e}")
                    traceback.print_exc()
            threading.Thread(target=_eod_routine, daemon=True).start()

        time.sleep(10)


# ─── Reset-flag processor ───────────────────────────────────────────────────────
def _process_reset_flag(flag_path: str):
    """
    Reset flag files are named reset_<scanner_id>_<side>.flag.
    - <side> = "all"  → clear every non-GENERAL record (Scanner 1 / 2)
    - <side> = "buy"  → Scanner 4: clear all BUY-side states (active, flagged,
                       cooling, stoploss-cooling)
    - <side> = "sell" → Scanner 4: mirror
    Signal log is preserved.
    """
    import glob
    import re
    fname = os.path.basename(flag_path)
    m = re.match(r"reset_(scanner_\d+)_(buy|sell|all)\.flag$", fname)
    if not m:
        return
    scanner_id, side = m.group(1), m.group(2)

    print(f"\n🔁 [reset] flag detected for {scanner_id} side={side}")

    # ── Scanner 4 (independent of scan_mgr) ───────────────────────────────
    if scanner_id == "scanner_4":
        if strategy4 is None:
            print("   ⚠️  Strategy 4 not initialised — flag will be retried "
                  "on next loop iteration.")
            return
        records = strategy4.state_store.all_records()
        buy_states  = ("BUY1_ACTIVE","BUY2_ACTIVE","BUY3_ACTIVE","BUY4_ACTIVE",
                       "FLAGGED_BUY","COOLING_BUY","COOLING_BUY_STOPLOSS")
        sell_states = ("SELL1_ACTIVE","SELL2_ACTIVE","SELL3_ACTIVE","SELL4_ACTIVE",
                       "FLAGGED_SELL","COOLING_SELL","COOLING_SELL_STOPLOSS")
        n = 0
        for rec in records:
            if side == "buy" and rec.state in buy_states:
                rec.reset(); n += 1
            elif side == "sell" and rec.state in sell_states:
                rec.reset(); n += 1
            elif side == "all" and rec.state != "GENERAL":
                rec.reset(); n += 1
        strategy4.state_store.save()
        print(f"   ✅ Strategy 4: {n} record(s) reset to GENERAL.")
        try: os.remove(flag_path)
        except Exception: pass
        return

    # ── Scanner 1 / 2 (via scan_mgr) ──────────────────────────────────────
    if scan_mgr is None:
        print("   ⚠️  scan_mgr not initialised — flag will be retried later.")
        return
    scanner = scan_mgr.get(scanner_id)
    if scanner is None or not scanner.active:
        print(f"   ⚠️  {scanner_id} not active — clearing flag.")
        try: os.remove(flag_path)
        except Exception: pass
        return
    n = 0
    for rec in list(scanner.state_store.all_records()):
        if rec.state != "GENERAL":
            scanner.state_store.reset_to_general(
                rec.symbol, reason="Dashboard reset")
            n += 1
    scanner.state_store.save()
    print(f"   ✅ {scanner.name}: {n} record(s) reset to GENERAL.")
    try: os.remove(flag_path)
    except Exception: pass


def _process_reset_flags():
    import glob
    for fp in glob.glob(os.path.join(ROOT, "data", RESET_FLAG_GLOB)):
        try:
            _process_reset_flag(fp)
        except Exception as e:
            print(f"   ⚠️  reset flag {fp}: {e}")


# ─── Config hot-reload + rescan ─────────────────────────────────────────────────
def config_reload_thread():
    import traceback
    last_mod = None
    config_path = os.path.join(ROOT, "config.json")

    while True:
        time.sleep(10)
        try:
            # Process any reset flags written by the dashboard
            try:
                _process_reset_flags()
            except Exception:
                print("⚠️  _process_reset_flags ERROR:")
                traceback.print_exc()

            # Check for rescan flag
            if os.path.exists(RESCAN_FLAG):
                scanner_id = None
                with open(RESCAN_FLAG, "r") as f:
                    content = f.read().strip()
                    # Format: "scanner_id:timestamp" or just timestamp
                    if ":" in content and content.split(":")[0].startswith("scanner_"):
                        scanner_id = content.split(":")[0]
                os.remove(RESCAN_FLAG)
                print(f"📥 Rescan flag detected: scanner_id={scanner_id}")
                try:
                    run_rescan(scanner_id)
                except Exception:
                    print(f"❌ run_rescan({scanner_id}) CRASHED:")
                    traceback.print_exc()
                continue

            # Check if config changed
            mod_time = os.path.getmtime(config_path)
            if last_mod and mod_time != last_mod:
                if scan_mgr:
                    scan_mgr.reload_configs()
                    print("🔧 Configs reloaded from dashboard.")
                if strategy4:
                    try:
                        cfg = load_config()
                        s4 = cfg.get("scanners", {}).get("scanner_4", {}).get("settings", {})
                        if s4:
                            strategy4.update_config(s4)
                    except Exception:
                        print("⚠️  S4 update_config ERROR:")
                        traceback.print_exc()
            last_mod = mod_time

        except Exception:
            print("⚠️  config_reload_thread OUTER ERROR:")
            traceback.print_exc()


def _rescan_scanner_4(config):
    """Re-run S4's full N-day replay under the latest config.

    Reset non-active stocks back to GENERAL so the replay can re-derive
    their state from each one's last GENERAL transition under the new
    thresholds. ACTIVE positions are preserved (don't disturb open trades
    on every threshold tweak). silent=True so webhooks fire forward-only.
    """
    global strategy4
    if strategy4 is None:
        return

    print("\n🔁 Rescan: Scanner 4 (RSI Momentum)...")
    s4_cfg   = config.get("scanners", {}).get("scanner_4", {}).get("settings", {})
    if not s4_cfg:
        print("   ⚠️  No scanner_4 config found.")
        return
    strategy4.update_config(s4_cfg)

    # Reset EVERY record to GENERAL so the replay re-derives current state
    # from each stock's history under the new settings. ACTIVE positions are
    # not preserved here -- the user explicitly wants state to reflect "what
    # would the state currently be under these settings", which requires a
    # full re-derive. Signal log is preserved (immutable audit trail).
    reset_count = 0
    for rec in strategy4.state_store.all_records():
        if rec.state != "GENERAL":
            rec.reset()
            reset_count += 1
    strategy4.state_store.save()
    print(f"   Reset {reset_count} records to GENERAL for clean replay.")

    lookback_days = int(s4_cfg.get("replay_lookback_days", 30))
    replay_from   = (datetime.now(IST) - timedelta(days=lookback_days)).replace(
                       hour=9, minute=15, second=0, microsecond=0)

    print(f"   Fresh-seeding RSI engine (pre-{replay_from.strftime('%Y-%m-%d')})...")
    try:
        strategy4.fresh_seed_from_csv(data_store, all_symbols, replay_from)
    except Exception as e:
        print(f"   ⚠️  fresh_seed_from_csv error: {e}")
        return

    print(f"   Replaying {lookback_days} days with new settings...")
    try:
        n = strategy4.run_carry_forward(
            data_store  = data_store,
            all_symbols = all_symbols,
            from_time   = replay_from,
            silent      = True,
            quiet       = True,
        )
        print(f"✅ Rescan complete: scanner_4 ({n} signals reproduced).\n")
    except Exception as e:
        print(f"   ⚠️  Replay error: {e}")


def run_rescan(scanner_id: str = None):
    """Run rescan for one scanner or all scanners."""
    config = load_config()

    # Handle Scanner 4 separately (not in scan_mgr) — full N-day replay
    # with current settings restores state from each stock's last GENERAL
    # transition. silent=True so webhooks fire forward-only.
    if scanner_id == "scanner_4" or scanner_id is None:
        _rescan_scanner_4(config)
        if scanner_id == "scanner_4":
            return   # done

    if scan_mgr is None:
        return

    scanners_to_rescan = []

    if scanner_id and scanner_id in scan_mgr.scanners:
        scanners_to_rescan = [scan_mgr.scanners[scanner_id]]
    else:
        scanners_to_rescan = scan_mgr.get_active()

    for scanner in scanners_to_rescan:
        print(f"\n🔁 Rescan: {scanner.name}...")
        scanner_cfg = config.get("scanners", {}).get(scanner.scanner_id, {})
        new_settings = scanner_cfg.get("settings", {})
        scanner.update_config(new_settings)

        # Reset EVERY record to GENERAL so the carry-forward re-derives
        # current state from each stock's history under the NEW settings.
        # (Matches S4 rescan behaviour and the user's explicit intent:
        # "show results according to the setting".) Signal log is the
        # immutable audit trail and is not touched here.
        from core.state_store import StockState
        reset_n = 0
        for rec in scanner.state_store.all_records():
            if rec.state == StockState.GENERAL:
                continue
            rec.state           = StockState.GENERAL
            rec.side            = None
            rec.watched_at      = None
            rec.reference_price = None
            rec.reference_time  = None
            rec.rsi_at_watch    = None
            rec.buy_signal_at   = None
            rec.buy_price       = None
            rec.buy_time        = None
            rec.drop_pct_at_buy = None
            rec.avg_entries     = []
            rec.last_avg_price  = None
            rec.last_avg_time   = None
            rec.avg_count       = 0
            rec.exit_signal_at  = None
            rec.exit_price      = None
            rec.exit_time       = None
            rec.rsi_at_exit     = None
            rec.gap_fill        = False
            reset_n += 1
        scanner.state_store.save()
        print(f"   Reset {reset_n} records to GENERAL for clean replay.")

        # Re-run carry-forward
        from core.carry_forward import CarryForwardEngine
        carry = CarryForwardEngine(
            rsi_cache       = rsi_cache,
            state_store     = scanner.state_store,
            strategy_config = new_settings
        )
        carry.run(all_symbols, mapper)
        carry.seed_prev_rsi(all_symbols)

        # Check current prices against new drop% for watched stocks
        # Only fire live-price BUY signals during market hours (09:15–15:30).
        # Pre-open ticks (09:00–09:15) can make current_price stale/auction prices
        # that should never trigger a signal.
        _now_check = datetime.now(IST)
        _hh, _mm   = _now_check.hour, _now_check.minute
        _in_market = (((_hh == 9 and _mm >= 15) or (10 <= _hh <= 14) or
                       (_hh == 15 and _mm <= 30)))
        if not _in_market:
            scanner.state_store.save()
            print(f"   ⏸  Skipping live-price BUY check — outside market hours ({_now_check.strftime('%H:%M')})")
            continue

        new_drop    = float(new_settings.get("drop_percent", 2.0))
        trigger_tf  = str(new_settings.get("trigger_timeframe", "1"))
        _TF_MIN_MAP = {"1":1,"2":2,"5":5,"10":10,"15":15,"30":30,"60":60}
        _tf_min     = _TF_MIN_MAP.get(trigger_tf, 1)
        # Compute the close time of the CURRENT trigger-tf candle so that
        # rescan BUY signals are timestamped like a real candle-close event,
        # not with the arbitrary wall-clock time at which rescan happened.
        _wall       = datetime.now(IST)
        _total_m    = _wall.hour * 60 + _wall.minute
        _floored    = (_total_m // _tf_min) * _tf_min
        _candle_open = _wall.replace(
            hour=_floored // 60, minute=_floored % 60,
            second=0, microsecond=0
        )
        candle_close_ts = _candle_open + timedelta(minutes=_tf_min)

        for rec in scanner.state_store.get_watched():
            if rec.reference_price and rec.current_price:
                drop = ((rec.reference_price - rec.current_price)
                        / rec.reference_price) * 100
                if drop >= new_drop:
                    scanner.state_store.move_to_active_buy(
                        symbol    = rec.symbol,
                        buy_price = rec.current_price,
                        drop_pct  = round(drop, 2),
                        now       = candle_close_ts
                    )
                    scanner.webhook_sender.send_buy(
                        symbol          = rec.symbol,
                        plain_name      = rec.plain_name,
                        company_name    = rec.company_name,
                        buy_price       = rec.current_price,
                        reference_price = rec.reference_price,
                        drop_pct        = round(drop, 2),
                        rsi_at_watch    = rec.rsi_at_watch or 0
                    )

        scanner.state_store.save()
        print(f"✅ Rescan complete: {scanner.name}\n")


# ─── Gap-fill after feed reconnect ─────────────────────────────────────────────
def run_full_gap_fill(gap_start: datetime) -> int:
    """
    Fetch missed candles for ALL symbols during the outage window.
    Uses a thread pool to parallelize Fyers API calls.
    Returns total candles filled (0 if network is down or gap too short).
    """
    if data_feed is None or gap_start is None:
        return 0

    gap_end      = datetime.now(IST)
    duration_min = int((gap_end - gap_start).total_seconds() / 60)
    if duration_min < 1:
        return 0

    # Only fetch TFs where at least 1 candle could close during the gap
    relevant_tfs = [tf for tf in HISTORY_TFS
                    if tf not in ("D", "W") and int(tf) <= duration_min]
    if not relevant_tfs:
        print(f"   Gap too short ({duration_min}m) — no complete candles to fill")
        return 0

    print(f"\n📥 Full gap-fill: {len(all_symbols)} symbols | "
          f"TFs: {relevant_tfs} | "
          f"gap {duration_min}m ({gap_start.strftime('%H:%M')}→{gap_end.strftime('%H:%M')})")

    total_filled = 0
    fill_lock    = threading.Lock()

    def fetch_symbol(symbol):
        count = 0
        for tf in relevant_tfs:
            try:
                resp = data_feed.client.history(data={
                    "symbol":      symbol,
                    "resolution":  tf,
                    "date_format": "1",
                    "range_from":  gap_start.strftime("%Y-%m-%d"),
                    "range_to":    gap_end.strftime("%Y-%m-%d"),
                    "cont_flag":   "1"
                })
                if resp.get("code") != 200:
                    continue
                candles = []
                for c in resp.get("candles", []):
                    ts = datetime.fromtimestamp(c[0], tz=IST)
                    h2, m2 = ts.hour, ts.minute
                    in_gap    = gap_start < ts <= gap_end
                    in_market = ((h2 == 9 and m2 >= 15) or
                                 (10 <= h2 <= 14) or
                                 (h2 == 15 and m2 <= 30))
                    if in_gap and in_market:
                        candles.append({
                            "datetime": ts,
                            "open": c[1], "high": c[2],
                            "low":  c[3], "close":c[4], "volume":c[5]
                        })
                if candles:
                    data_store.save_candles_merged(symbol, tf, candles)
                    count += len(candles)
            except Exception:
                pass
        return count

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_symbol, sym): sym for sym in all_symbols}
        for fut in as_completed(futures):
            try:
                n = fut.result()
                if n > 0:
                    with fill_lock:
                        total_filled += n
            except Exception:
                pass

    print(f"✅ Full gap-fill complete: {total_filled} candles "
          f"across {len(all_symbols)} symbols\n")
    return total_filled


def post_gap_recovery(gap_start: datetime) -> int:
    """
    Full recovery after a feed outage:
      1. Fetch missing candles for ALL symbols (thread-pooled)
      2. Rebuild RSI cache in memory from updated CSVs
      3. Re-seed scanner RSI engines
      4. Reset WATCHED stocks to GENERAL so carry-forward re-checks buy triggers
      5. Re-run carry-forward — restores states and fires MISSED-tagged webhooks
    Returns candles filled (0 means network still down — caller should retry).
    """
    filled = run_full_gap_fill(gap_start)
    if filled == 0:
        return 0

    if scan_mgr is None:
        return filled

    # Rebuild RSI cache in memory from updated CSVs
    print("🔧 Rebuilding in-memory RSI cache from gap candles...")
    rsi_period = 14
    for scanner in scan_mgr.get_active():
        p = scanner.config.get("settings", {}).get("rsi_period", 14)
        if isinstance(p, int):
            rsi_period = p
            break
    updated = rsi_cache.update_symbols(all_symbols, HISTORY_TFS, data_store, rsi_period)
    print(f"✅ RSI cache updated: {updated} combinations\n")

    # Re-seed all scanner RSI engines from updated cache
    scan_mgr.seed_all_rsi(HISTORY_TFS)

    # Reset WATCHED stocks to GENERAL so the full carry-forward replay
    # can check whether any buy trigger fired during the gap
    from core.state_store import StockState
    for scanner in scan_mgr.get_active():
        reset_count = 0
        for rec in list(scanner.state_store.get_watched()):
            scanner.state_store.reset_to_general(
                rec.symbol, reason="Gap recovery replay")
            reset_count += 1
        if reset_count:
            print(f"   ↩️  {scanner.name}: {reset_count} WATCHED stocks reset for replay")

    # Re-run carry-forward — restores correct states and fires MISSED webhooks
    # fire_webhooks=True only here (genuine mid-session gap), never on startup
    print("🔄 Re-running carry-forward after gap recovery...")
    scan_mgr.run_all_carry_forward(fire_webhooks=True)
    print("✅ Gap recovery complete — all states restored.\n")

    return filled


# ─── Feed watchdog ──────────────────────────────────────────────────────────────
# Tracks the original start of the current outage so gap-fill covers the full window.
_gap_start_time:       datetime = None
_gap_start_tick_count: int      = 0   # tick_count when outage was first detected


def _fill_gap_with_retry(gap_start: datetime):
    """Run full gap recovery, retrying up to 3 times if network is still recovering."""
    for attempt in range(1, 4):
        time.sleep(15 * attempt)   # 15s, 30s, 45s between retries
        filled = post_gap_recovery(gap_start)
        if filled > 0:
            return
        if attempt < 3:
            print(f"   ⚠️  Gap recovery got 0 candles "
                  f"(attempt {attempt}/3) — retrying...")
    print("   ⚠️  Gap recovery exhausted retries (network may still be down)")


def feed_watchdog():
    """Restart WebSocket feed if no ticks during market hours for 2+ min.
    Handles BOTH cases:
      A. Feed was working then stopped (last_tick_time stale)
      B. Feed NEVER started (last_tick_time is None) -- this is the
         cold-start failure mode that previously required manual restart.
    Uses tick_count (not last_tick_time) to detect real recovery."""
    global _gap_start_time, _gap_start_tick_count
    market_open_seen_at = None
    cold_start_restart_attempts = 0
    while True:
        time.sleep(60)
        try:
            if data_feed is None or scan_mgr is None:
                continue
            now = datetime.now(IST)
            h, m = now.hour, now.minute
            in_market = ((h == 9 and m >= 15) or (10 <= h <= 14) or
                         (h == 15 and m <= 30))
            if not in_market:
                _gap_start_time = None
                market_open_seen_at = None
                cold_start_restart_attempts = 0
                continue

            last = data_feed.last_tick_time

            # ── COLD-START failure: no tick ever received this session ──
            if last is None:
                if market_open_seen_at is None:
                    market_open_seen_at = now
                elapsed_open = (now - market_open_seen_at).total_seconds()
                # Wait 5 minutes after market open before declaring cold-start
                # dead (matches dashboard STARTING window so user sees
                # consistent state: dashboard "STARTING" + no auto-restart yet).
                if elapsed_open > 300:
                    cold_start_restart_attempts += 1
                    print(f"\n🚨 WATCHDOG: COLD-START FAILURE — market has been "
                          f"open {int(elapsed_open)}s, zero ticks received. "
                          f"Restarting feed (attempt {cold_start_restart_attempts})...")
                    try:
                        data_feed.restart()
                    except Exception as e:
                        print(f"   restart() error: {e}")
                    # Backoff: don't hammer if restart keeps failing
                    time.sleep(min(30 * cold_start_restart_attempts, 300))
                continue

            # ── Normal path: feed was working, now stale ──
            market_open_seen_at = None   # we got at least one tick; clear cold-start tracker
            cold_start_restart_attempts = 0
            elapsed = (now - last).total_seconds()

            if elapsed > 300:
                # Feed is dead — remember the FIRST moment of this outage
                if _gap_start_time is None:
                    _gap_start_time       = last   # real last-tick time, not now()
                    _gap_start_tick_count = data_feed.tick_count
                print(f"\n⚠️  WATCHDOG: No ticks for "
                      f"{int(elapsed//60)}m{int(elapsed%60)}s — restarting feed...")
                data_feed.restart()

            elif _gap_start_time is not None:
                # Only declare recovery when NEW ticks have actually arrived
                # (tick_count increased), not just because restart() reset last_tick_time
                if data_feed.tick_count > _gap_start_tick_count:
                    gap_start             = _gap_start_time
                    _gap_start_time       = None
                    _gap_start_tick_count = 0
                    print(f"\n✅ WATCHDOG: Feed recovered — filling gap from "
                          f"{gap_start.strftime('%H:%M')}...")
                    threading.Thread(
                        target=_fill_gap_with_retry,
                        args=(gap_start,),
                        daemon=True
                    ).start()
                else:
                    print("   ⏳ WATCHDOG: Waiting for live ticks "
                          "(reconnected but no ticks yet)...")

        except Exception as e:
            print(f"⚠️  Watchdog error: {e}")


# ─── Strategy 3 initialisation ─────────────────────────────────────────────────
def start_strategy3():
    """
    Initialise Strategy 3 (Webhook Mirror) if enabled in config.
    Called once from main() after startup_pipeline thread is launched.
    Runs completely independently of scanner_1 / scanner_2.
    """
    global strategy3, _webhook_server
    config  = load_config()
    s3_cfg  = config.get("scanners", {}).get("scanner_3", {})

    if not s3_cfg.get("active", False):
        return
    if s3_cfg.get("strategy_type") != "webhook_mirror":
        return

    settings = s3_cfg.get("settings", {})
    port     = int(s3_cfg.get("incoming_webhooks", {}).get("server_port", 5001))

    print("\n🔌 Starting Strategy 3 — Webhook Mirror...")

    # Strategy 3 rebuilt 2026-05-27: candle-based entry/exit via webhook.
    # New class lives in strategies/strategy3_candle.py with fresh state
    # schema; the legacy Strategy3WebhookMirror class is left in place but
    # not used.
    from strategies.strategy3_candle import (
        Strategy3CandleMirror, S3StateStore as S3CandleStateStore,
    )
    state_store    = S3CandleStateStore(
        os.path.join(ROOT, s3_cfg.get("state_file", "data/scanner_3_state.json")))
    webhook_sender = S3WebhookSender(scanner_id="scanner_3")
    strategy3      = Strategy3CandleMirror(
        config         = settings,
        state_store    = state_store,
        data_store     = data_store,
        mapper         = mapper,
        webhook_sender = webhook_sender,
    )
    _webhook_server = WebhookServer(strategy3=strategy3, mapper=mapper, port=port)
    _webhook_server.start()

    # Replay any candle closes missed while scanner was down: walks
    # historical candles forward from each non-GENERAL record's last
    # event, firing BUY/AVG/EXIT for any that the conditions trigger.
    try:
        n = strategy3.replay_missed_candles()
        if n:
            print(f"   [S3] {n} record(s) advanced via missed-candle replay.")
    except Exception as e:
        import traceback
        print(f"   ⚠️  S3 replay error: {e}")
        traceback.print_exc()

    print("✅ Strategy 3 ready.\n")


# ─── Strategy 4 initialisation ─────────────────────────────────────────────────
def start_strategy4():
    """
    Initialise Strategy 4 (RSI Momentum) if enabled in config.
    Called from startup_pipeline() AFTER the RSI cache is loaded/built, since
    Strategy 4 needs its own RSI engine seeded from rsi_cache.
    Runs independently of scanner_1 / scanner_2 / scanner_3.
    """
    global strategy4
    config = load_config()
    s4_cfg = config.get("scanners", {}).get("scanner_4", {})

    if not s4_cfg.get("active", False):
        return
    if s4_cfg.get("strategy_type") != "momentum":
        return

    settings = s4_cfg.get("settings", {})
    sig_tf   = str(settings.get("signal_timeframe", "15"))

    print("\n🔌 Starting Strategy 4 — RSI Momentum...")

    s4_rsi_engine  = RSIEngine(period=14)
    state_store    = S4StateStore(s4_cfg.get("state_file", "data/scanner_4_state.json"))
    webhook_sender = S4WebhookSender()

    # Stock-group filters (defaults to "nifty650"; falls back to no-filter
    # if the configured group name doesn't exist on disk)
    stock_groups   = get_stock_groups()
    buy_group_cfg  = s4_cfg.get("buy_stock_group",  "nifty650")
    sell_group_cfg = s4_cfg.get("sell_stock_group", "nifty650")
    if buy_group_cfg and buy_group_cfg not in stock_groups.list():
        print(f"   ⚠️  buy_stock_group '{buy_group_cfg}' not found "
              f"— disabling buy-side group filter.")
        buy_group_cfg = None
    if sell_group_cfg and sell_group_cfg not in stock_groups.list():
        print(f"   ⚠️  sell_stock_group '{sell_group_cfg}' not found "
              f"— disabling sell-side group filter.")
        sell_group_cfg = None

    strategy4      = Strategy4Momentum(
        config           = settings,
        state_store      = state_store,
        webhook_sender   = webhook_sender,
        rsi_engine       = s4_rsi_engine,
        mapper           = mapper,
        stock_groups     = stock_groups,
        buy_stock_group  = buy_group_cfg,
        sell_stock_group = sell_group_cfg,
    )

    # ─── Full-history replay ──────────────────────────────────────────────
    # Walk the last N days of candles through the state machine to restore
    # each stock to the state it would currently be in IF the scanner had
    # been running continuously over that window. Point-in-time Daily and
    # Weekly RSI are advanced at day/week boundaries inside the replay so
    # past intraday decisions are evaluated against the D/W RSI that was
    # actually in force at that moment (not today's stale value).
    #
    # silent=True suppresses webhook fires; only forward-going signals
    # fire webhooks (per agreed policy).
    lookback_days = int(settings.get("replay_lookback_days", 30))
    replay_from   = (datetime.now(IST) - timedelta(days=lookback_days)).replace(
                       hour=9, minute=15, second=0, microsecond=0)

    print(f"📊 [scanner_4] Fresh-seeding RSI engine "
          f"(pre-{replay_from.strftime('%Y-%m-%d')} candles)...")
    seeded = strategy4.fresh_seed_from_csv(data_store, all_symbols, replay_from)
    print(f"   Seeded {seeded}/{len(all_symbols)} symbols on {sig_tf}m.")

    # ─── REPLAY SKIP-CACHE ───────────────────────────────────────────────
    # The reset+replay below is expensive (~10 min) but only needs to run
    # at most once per trading day. Set SCANNER4_REPLAY_MAX_AGE_HOURS to
    # skip the reset+replay block on quick same-day restarts.
    # NOTE: fresh_seed_from_csv (above) is in-memory and ALWAYS runs.
    # We cache only the state-mutating part.
    from core.run_cache import RunCache
    _s4_cache    = RunCache("scanner4_replay")
    _s4_max_age  = float(os.environ.get("SCANNER4_REPLAY_MAX_AGE_HOURS", "0"))
    _s4_fresh, _s4_prev = _s4_cache.is_fresh(_s4_max_age)
    # Only honour cache if the lookback window matches (caller can bump
    # replay_lookback_days in config and the cache will rebuild safely).
    _s4_match_window = (_s4_prev or {}).get("payload", {}).get(
        "lookback_days") == lookback_days
    if _s4_fresh and _s4_match_window:
        prev_pl = _s4_prev["payload"]
        print(f"⚡ [scanner_4] Replay cached — ran {_s4_prev['ran_at']} "
              f"({prev_pl.get('n_replayed', '?')} signals reproduced, "
              f"window {prev_pl.get('lookback_days')}d).")
        print(f"   Existing state in {state_store.state_file} is reused.\n")
    else:
        # Pre-register all symbols + RESET every record to GENERAL so the replay
        # re-derives current state from each stock's history under CURRENT
        # settings. Without this reset, the replay continues from whatever stale
        # state was loaded from scanner_4_state.json and can miss earlier entries
        # that should have fired under current thresholds.
        # Signal log is preserved (immutable audit trail).
        reset_n = 0
        for sym in all_symbols:
            plain   = mapper.get_plain_name(sym)
            company = mapper.get_company_name(plain)
            rec     = state_store.get_or_create(sym, plain, company)
            if rec.state != "GENERAL":
                rec.reset()
                reset_n += 1
        state_store.save()
        print(f"   Reset {reset_n} records to GENERAL for clean replay.")

        print(f"🔄 [scanner_4] Full-history replay "
              f"({lookback_days} days, point-in-time D/W RSI)...")
        try:
            n_replayed = strategy4.run_carry_forward(
                data_store  = data_store,
                all_symbols = all_symbols,
                from_time   = replay_from,
                silent      = True,
                quiet       = True,
            )
            print(f"✅ [scanner_4] Replay complete: {n_replayed} signals reproduced.")
            _s4_cache.mark_success({
                "lookback_days": lookback_days,
                "n_replayed":    n_replayed,
                "n_symbols":     len(all_symbols),
            })
        except Exception as e:
            print(f"⚠️  [scanner_4] Replay error: {e}")

    # Launch the once-per-day daily-RSI exit check thread (default 15:25)
    strategy4.start_exit_check_thread()

    print(f"✅ Strategy 4 ready (signal_tf={sig_tf}).\n")


# ─── Symbol gap healer ──────────────────────────────────────────────────────────
def _heal_symbols(scanner, stale_list: list, heal_tfs: list, now: datetime) -> int:
    """
    For each stale symbol: fetch missing candles from Fyers, save to CSV,
    re-seed RSI cache + engine, reset WATCHED states, replay carry-forward.

    stale_list : [(symbol, last_close_or_None), ...]
    heal_tfs   : intraday timeframes to fetch (intersection with HISTORY_TFS)
    Returns    : number of symbols for which new candles were found.
    """
    if not stale_list or data_feed is None:
        return 0

    from_str = (now - timedelta(days=5)).strftime("%Y-%m-%d")
    to_str   = now.strftime("%Y-%m-%d")
    healed   = []

    for sym, _ in stale_list:
        got_new = False
        _heal_last_attempt[sym] = now.timestamp()   # mark attempt regardless of result

        for tf in heal_tfs:
            try:
                last_dt = data_store.get_last_stored_date(sym, tf)
                resp    = data_feed.client.history(data={
                    "symbol":      sym,
                    "resolution":  tf,
                    "date_format": "1",
                    "range_from":  from_str,
                    "range_to":    to_str,
                    "cont_flag":   "1"
                })
                if resp.get("code") != 200:
                    continue

                candles = []
                for c in resp.get("candles", []):
                    ts   = datetime.fromtimestamp(c[0], tz=IST)
                    hh, mm = ts.hour, ts.minute
                    if not ((hh == 9 and mm >= 15) or (10 <= hh <= 14) or
                            (hh == 15 and mm <= 30)):
                        continue
                    if last_dt is not None and ts <= last_dt:
                        continue
                    candles.append({"datetime": ts,
                                    "open":  c[1], "high": c[2],
                                    "low":   c[3], "close": c[4],
                                    "volume": c[5]})

                if candles:
                    data_store.save_candles(sym, tf, candles, append=True)
                    data_store.deduplicate(sym, tf)
                    _last_candle_time.setdefault(sym, {})[tf] = candles[-1]["datetime"]
                    got_new = True

            except Exception:
                pass
            time.sleep(0.05)

        if got_new:
            healed.append(sym)

    if not healed:
        return 0

    print(f"   📥 Filled missing candles for {len(healed)} symbols")

    # Re-seed RSI cache in memory from the updated CSVs
    rsi_period = int(scanner.config.get("settings", {}).get("rsi_period", 14))
    rsi_cache.update_symbols(healed, heal_tfs, data_store, rsi_period)
    # Re-seed the scanner's live RSI engine
    scanner.rsi_cache.seed_rsi_engine(scanner.rsi_engine, healed, heal_tfs)

    # Reset WATCHED stocks so carry-forward re-evaluates buy triggers
    from core.carry_forward import CarryForwardEngine
    from core.state_store   import StockState
    reset_count = 0
    for sym in healed:
        rec = scanner.state_store.get(sym)
        if rec and rec.state == StockState.WATCHED:
            scanner.state_store.reset_to_general(sym, reason="Gap healer replay")
            reset_count += 1

    carry = CarryForwardEngine(
        rsi_cache       = rsi_cache,
        state_store     = scanner.state_store,
        strategy_config = scanner.config.get("settings", {})
    )
    carry.run(healed, scanner.mapper, webhook_sender=scanner.webhook_sender)
    carry.seed_prev_rsi(healed)
    scanner.state_store.save()

    print(f"   🔄 Carry-forward replayed for {len(healed)} symbols "
          f"({reset_count} WATCHED reset)")
    return len(healed)


def symbol_gap_healer():
    """
    Background thread: detects per-symbol candle gaps during market hours
    and self-heals by fetching the missing data from Fyers API.

    Runs every HEAL_INTERVAL seconds. A symbol is considered stale when its
    last known candle close is more than 2 candle-periods behind the current
    expected close — enough buffer to avoid false positives from normal tick
    delivery jitter. Symbols are not retried within RETRY_COOLDOWN seconds to
    avoid hammering the API for legitimately illiquid stocks.
    """
    HEAL_INTERVAL  = 10 * 60   # check every 10 minutes
    GRACE_MINUTES  = 20        # wait 20 min after open before first check
    RETRY_COOLDOWN = 25 * 60   # don't retry same symbol within 25 minutes

    while True:
        time.sleep(HEAL_INTERVAL)
        try:
            if scan_mgr is None or data_feed is None:
                continue

            now = datetime.now(IST)
            h, m = now.hour, now.minute
            in_market = ((h == 9 and m >= 15) or (10 <= h <= 14) or
                         (h == 15 and m <= 30))
            if not in_market:
                continue
            if (h * 60 + m) - (9 * 60 + 15) < GRACE_MINUTES:
                continue   # still in startup grace period

            # Collect the unique intraday TFs needed across all active scanners
            needed_tfs: set = set()
            for s in scan_mgr.get_active():
                cfg = s.config.get("settings", {})
                for key in ("scan_timeframe", "exit_timeframe"):
                    tf = str(cfg.get(key, "5"))
                    if tf in HISTORY_TFS:
                        needed_tfs.add(tf)
            heal_tfs = sorted(needed_tfs, key=lambda x: int(x))

            total_healed = 0
            now_ts = now.timestamp()

            for scanner in scan_mgr.get_active():
                settings = scanner.config.get("settings", {})
                scan_tf  = str(settings.get("scan_timeframe", "5"))
                tf_min   = int(scan_tf)

                # Cutoff: flag only symbols 2+ candle-periods behind expected close.
                # expected close = floor(now, tf_min) = open of the current in-progress candle
                total_m  = h * 60 + m
                floor_m  = (total_m // tf_min) * tf_min
                cutoff_m = floor_m - tf_min * 2
                cutoff_m = max(cutoff_m, 9 * 60 + 15)   # never before market open
                cutoff   = now.replace(
                    hour    = cutoff_m // 60,
                    minute  = cutoff_m % 60,
                    second  = 0, microsecond=0
                )

                stale = []
                for sym in scanner.all_symbols:
                    # Skip if recently attempted (illiquid / API error throttle)
                    if now_ts - _heal_last_attempt.get(sym, 0) < RETRY_COOLDOWN:
                        continue
                    last = _last_candle_time.get(sym, {}).get(scan_tf)
                    if last is None or last < cutoff:
                        stale.append((sym, last))

                if not stale:
                    continue

                print(f"\n🩹 Gap healer [{scanner.name}]: {len(stale)} symbols stale "
                      f"on {scan_tf}m (expected last close ≥ {cutoff.strftime('%H:%M')})")

                healed = _heal_symbols(scanner, stale, heal_tfs, now)
                total_healed += healed

            if total_healed:
                print(f"✅ Gap healer complete — {total_healed} symbols healed\n")

        except Exception as e:
            print(f"⚠️  Gap healer error: {e}")


# ─── Active-buy watchdog ────────────────────────────────────────────────────────
def active_buy_watchdog():
    """
    Backup exit checker: every 5 minutes during market hours, re-evaluates all
    ACTIVE_BUY stocks against the live RSI engine. Fires the exit if RSI > threshold
    but the candle-close handler apparently missed it.
    """
    WATCHDOG_INTERVAL = 5 * 60   # check every 5 minutes
    GRACE_MINUTES     = 20       # skip first 20 min after open (seeding / carry-fwd)

    while True:
        time.sleep(WATCHDOG_INTERVAL)
        try:
            if scan_mgr is None:
                continue

            now = datetime.now(IST)
            h, m = now.hour, now.minute
            in_market = (h == 9 and m >= 15) or (10 <= h <= 14) or (h == 15 and m <= 30)
            if not in_market:
                continue
            if (h * 60 + m) - (9 * 60 + 15) < GRACE_MINUTES:
                continue

            for scanner in scan_mgr.get_active():
                # Per-scanner isolation: one bad config can't kill the watchdog
                # cycle for OTHER scanners.
                try:
                    cfg        = scanner.config.get("settings", {})
                    exit_tf    = str(cfg.get("exit_timeframe",   "10"))
                    rsi_exit_v = float(cfg.get("rsi_exit_threshold", 68))

                    # Skip D/W scanners: daily/weekly exits are evaluated at
                    # candle-close by the strategy + EOD reconcile, NOT by this
                    # intraday 5-min watchdog. Without this guard, int("D")
                    # raised ValueError and aborted the whole cycle (kills the
                    # safety net for ALL scanners).
                    if exit_tf in ("D", "W"):
                        continue

                    tf_min     = int(exit_tf)

                    # Candle-aligned timestamp for the exit signal
                    total_m      = h * 60 + m
                    floored      = (total_m // tf_min) * tf_min
                    candle_open  = now.replace(
                        hour=floored // 60, minute=floored % 60,
                        second=0, microsecond=0)
                    candle_close = candle_open + timedelta(minutes=tf_min)

                    for rec in scanner.state_store.get_active_buys():
                        try:
                            rsi = scanner.rsi_engine.get_rsi(rec.symbol, exit_tf)
                            if rsi is None or rsi <= rsi_exit_v:
                                continue
                            # RSI is above exit threshold but stock is still ACTIVE_BUY —
                            # the candle-close handler missed this exit. Fire it now.
                            cur_price = rec.current_price or rec.buy_price or 0.0
                            scanner.state_store.move_to_exited(
                                symbol     = rec.symbol,
                                exit_price = cur_price,
                                rsi_value  = rsi,
                                now        = candle_close
                            )
                            scanner.webhook_sender.send_exit(
                                symbol       = rec.symbol,
                                plain_name   = rec.plain_name,
                                company_name = rec.company_name,
                                exit_price   = cur_price,
                                buy_price    = rec.buy_price or cur_price,
                                rsi_at_exit  = rsi,
                                avg_count    = rec.avg_count
                            )
                            print(f"  🔔 [WATCHDOG] EXIT fired: {rec.plain_name} "
                                  f"RSI={rsi:.1f} > {rsi_exit_v} @ ₹{cur_price}")
                        except Exception as e:
                            print(f"  ⚠️  [WATCHDOG] {rec.symbol}: {e}")
                except Exception as e:
                    print(f"  ⚠️  [WATCHDOG] scanner '{scanner.name}': {e}")
                    continue

        except Exception as e:
            print(f"⚠️  Active-buy watchdog error: {e}")


# ─── Main ───────────────────────────────────────────────────────────────────────
def main():
    global data_feed, heartbeat, tick_freshness_mon, tick_sanity_validator
    global subscription_tracker

    print("🔐 Authenticating with Fyers...")
    fyers_client = get_fyers_client()
    print("✅ Authenticated.\n")

    # ─── Runtime monitors (heartbeat, tick freshness, sanity) ──────────────
    from core.runtime_monitors import (
        HeartbeatWriter, TickFreshnessMonitor,
        TickSanityValidator, SubscriptionTracker,
    )
    tick_freshness_mon   = TickFreshnessMonitor(stale_seconds=300, check_interval=60)
    tick_sanity_validator = TickSanityValidator(max_pct_jump=5.0)
    subscription_tracker = SubscriptionTracker()
    subscription_tracker.register_subscription(all_symbols)
    heartbeat            = HeartbeatWriter(
        interval             = 30,
        tick_freshness_mon   = tick_freshness_mon,
        subscription_tracker = subscription_tracker,
    )
    heartbeat.start()
    tick_freshness_mon.start()
    print("✅ Runtime monitors started (heartbeat, tick-freshness, "
          "tick-sanity, subscription-tracker).\n")

    # Start price flush immediately — ticks arrive before startup_pipeline finishes
    price_store.start_flush_thread()

    threading.Thread(target=check_market_events,  daemon=True).start()
    threading.Thread(target=config_reload_thread, daemon=True).start()
    threading.Thread(target=feed_watchdog,        daemon=True).start()
    threading.Thread(target=symbol_gap_healer,    daemon=True).start()
    threading.Thread(target=active_buy_watchdog,  daemon=True).start()

    # Startup pipeline in background (history → RSI cache → scanners → carry-forward)
    threading.Thread(
        target=startup_pipeline,
        args=(fyers_client,),
        daemon=True
    ).start()

    # Strategy 3 — runs independently of startup_pipeline
    start_strategy3()

    # Start live feed immediately — candles still saved to CSV even while scan_mgr is None
    print("\n📡 Starting live data feed...")
    data_feed = DataFeed(
        fyers_client     = fyers_client,
        symbols          = all_symbols,
        on_tick_callback = on_tick,
        timeframes       = TIMEFRAMES
    )
    data_feed.start()

    print("\n✅ Scanner is LIVE.")
    print("   Dashboard : http://localhost:8501")
    print("\nPress Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(60)
            now     = datetime.now(IST)
            storage = data_store.total_storage_mb()
            summary = scan_mgr.summary() if scan_mgr else {}
            for sid, s in summary.items():
                if s.get("active"):
                    print(
                        f"[{now.strftime('%H:%M')}] {s['name']} | "
                        f"Watched:{s.get('watched',0)} | "
                        f"Buys:{s.get('active',0)} | "
                        f"Exited:{s.get('exited',0)}"
                    )
            print(f"   Storage:{storage}MB | Ticks:{data_feed.tick_count}")
            # Save state every minute so dashboard shows current prices
            if scan_mgr:
                for scanner in scan_mgr.get_active():
                    scanner.state_store.save()
            if strategy3:
                strategy3.save_state()
            if strategy4:
                strategy4.save_state()
    except KeyboardInterrupt:
        print("\n\n👋 Scanner stopped.")
    finally:
        # Clean shutdown of runtime monitors + release PID lock
        try:
            from core.env_checks import release_instance_lock
            release_instance_lock()
        except Exception:
            pass
        try:
            if heartbeat: heartbeat.stop()
        except Exception:
            pass
        try:
            if tick_freshness_mon: tick_freshness_mon.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
