"""
refetch_history.py — Full Fyers history refetch for all symbols and timeframes.

WHY THIS EXISTS:
SOLARINDS 15m RSI was 5+ points off Fyers chart because the local CSV had
accumulated stale / revised candles over many sessions.  A fresh API fetch
gave the exact chart value (73.94).  This script does the same wholesale for
all 633 symbols × 7 timeframes (5, 10, 15, 30, 60, D, W).

FOOLPROOF FEATURES:
  1. **Backup**  — every existing CSV is copied to
     data/price_history_backup_<timestamp>/ before being touched.
     Restoring is `Copy-Item -Recurse <backup>\* data\price_history\`.
  2. **Atomic writes** — each new CSV is written to <name>.tmp first then
     os.replace()'d on top of the old file, so a crash mid-write never leaves
     a half-written CSV.
  3. **Resumable** — progress is saved to data/refetch_status.json after
     every (symbol, tf) completes.  Restart the script and it skips the ones
     marked ok.
  4. **Retries** — each (symbol, tf) gets 3 attempts with 2s/4s backoff.
  5. **Parallel** — uses 4 workers (Fyers rate-limit safe).
  6. **Sanity check** — at the end, verifies SOLARINDS 15m RSI at the
     21-May 09:15 candle equals Fyers chart's 73.94 ±0.5.
  7. **Cache reset** — deletes data/rsi_cache.pkl so the scanner rebuilds it
     from the fresh CSVs on next startup.

BEFORE RUNNING:
  STOP the live scanner (main.py) first.  It appends to the same CSVs in
  real time; a concurrent refetch can lose live candles.
"""

import os
import sys
import csv
import json
import time
import shutil
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pytz
from core.fyers_auth      import get_fyers_client
from core.symbol_map      import SymbolMapper
from core.data_store      import DataStore, CSV_HEADERS
from core.history_manager import HistoryManager

IST = pytz.timezone("Asia/Kolkata")

# What we refetch — same as HISTORY_TFS in main.py
TIMEFRAMES = ["5", "10", "15", "30", "60", "D", "W"]

WORKERS         = 4         # Fyers rate-limit safe
RETRIES         = 3         # per (symbol, tf)
BACKOFF_BASE    = 2.0       # 2s, 4s, 6s
STATUS_FILE     = os.path.join(ROOT, "data", "refetch_status.json")
RSI_CACHE_PATH  = os.path.join(ROOT, "data", "rsi_cache.pkl")
PRICE_HIST_DIR  = os.path.join(ROOT, "data", "price_history")


# ──────────────────────────────────────────────────────────────────────────
def warn_if_scanner_running() -> bool:
    """Returns True if the live scanner appears to be running."""
    lp = os.path.join(ROOT, "data", "live_prices.json")
    if not os.path.exists(lp):
        return False
    age = time.time() - os.path.getmtime(lp)
    return age < 120   # updated in the last 2 minutes ⇒ probably live


def load_status() -> dict:
    if not os.path.exists(STATUS_FILE):
        return {"started_at": None, "completed": {}, "failed": {}}
    try:
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"started_at": None, "completed": {}, "failed": {}}


def save_status(status: dict, lock: threading.Lock):
    """Snapshot the dict under lock (avoids 'dict changed size during
    iteration' when serializing while other workers mutate), then write
    the snapshot to disk WITHOUT holding the lock."""
    with lock:
        snapshot = {
            "started_at": status.get("started_at"),
            "backup_dir": status.get("backup_dir"),
            # dict() does a shallow copy under the lock — safe to dump after
            "completed":  dict(status.get("completed", {})),
            "failed":     dict(status.get("failed",    {})),
        }
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snapshot, f, indent=2)
    _safe_replace(tmp, STATUS_FILE)


def _safe_replace(src: str, dst: str, attempts: int = 8):
    """os.replace() but with retries — Windows sometimes hasn't released
    file handles fast enough (antivirus, indexer, OS-level locks)."""
    last_err = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(0.2 * (i + 1))
    raise last_err if last_err else RuntimeError("replace failed")


def key(symbol: str, tf: str) -> str:
    return f"{symbol}|{tf}"


def _filepath(symbol: str, timeframe: str) -> str:
    safe = symbol.replace(":", "_").replace("/", "_")
    return os.path.join(PRICE_HIST_DIR, f"{safe}_{timeframe}m.csv")


def write_csv_atomic(filepath: str, candles: list):
    """Write candles to a .tmp file then atomically replace target."""
    tmp = filepath + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADERS)
        for c in candles:
            w.writerow([
                c["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                c["open"], c["high"], c["low"], c["close"], c["volume"],
            ])
    _safe_replace(tmp, filepath)


def fetch_one(hm: HistoryManager, symbol: str, tf: str) -> int:
    """Returns number of candles written, or raises on failure."""
    candles = hm._fetch_full_history(symbol, tf)
    if not candles:
        raise RuntimeError("API returned 0 candles")
    write_csv_atomic(_filepath(symbol, tf), candles)
    return len(candles)


def worker(hm: HistoryManager, jobs: list, status: dict,
           status_lock: threading.Lock, progress: dict,
           progress_lock: threading.Lock):
    for sym, tf in jobs:
        k = key(sym, tf)
        with status_lock:
            already_done = bool(status["completed"].get(k))
        if already_done:
            continue   # already done in a prior run

        last_err = None
        for attempt in range(1, RETRIES + 1):
            try:
                n = fetch_one(hm, sym, tf)
                with status_lock:
                    status["completed"][k] = {
                        "candles": n,
                        "at": datetime.now(IST).isoformat()
                    }
                    status["failed"].pop(k, None)
                save_status(status, status_lock)
                with progress_lock:
                    progress["done"] += 1
                    if progress["done"] % 25 == 0:
                        print(f"   ... {progress['done']}/{progress['total']} "
                              f"({progress['done']/progress['total']*100:.1f}%)")
                last_err = None
                break
            except Exception as e:
                last_err = str(e)
                if attempt < RETRIES:
                    time.sleep(BACKOFF_BASE * attempt)

        if last_err:
            with status_lock:
                status["failed"][k] = {
                    "error": last_err,
                    "at": datetime.now(IST).isoformat()
                }
            save_status(status, status_lock)
            with progress_lock:
                progress["failed"] += 1
                progress["done"]   += 1


# ──────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 72)
    print("  FYERS HISTORY REFETCH — full rebuild from REST API")
    print("=" * 72)

    if warn_if_scanner_running():
        print("\n⚠️  WARNING: live_prices.json was updated <2 min ago — the")
        print("   scanner appears to be running.  Stop main.py first or risk")
        print("   losing live candles that arrive during the refetch.")
        ans = input("\nContinue anyway? (type 'yes' to proceed): ").strip()
        if ans.lower() != "yes":
            print("Aborted.")
            return 1

    # ── Pre-flight: auth + 1-symbol smoke test ────────────────────────────
    print("\n🔐 Authenticating with Fyers...")
    client = get_fyers_client()
    hm     = HistoryManager(client)

    mapper      = SymbolMapper()
    all_symbols = mapper.get_all_fyers_symbols()
    print(f"   Authenticated.  {len(all_symbols)} symbols, "
          f"{len(TIMEFRAMES)} timeframes.")

    print("\n🧪 Smoke test: fetching SOLARINDS 15m once...")
    smoke = hm._fetch_full_history("NSE:SOLARINDS-EQ", "15")
    if not smoke:
        print("❌ Smoke test FAILED — token expired or API down.  Aborting.")
        return 2
    print(f"   OK — got {len(smoke)} candles.")

    # ── Backup ────────────────────────────────────────────────────────────
    ts         = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(ROOT, "data", f"price_history_backup_{ts}")

    status = load_status()
    if not status["completed"]:
        # Only back up on a fresh start (avoid re-backing up on resume)
        print(f"\n💾 Backing up existing CSVs → {backup_dir}")
        if os.path.exists(PRICE_HIST_DIR):
            shutil.copytree(PRICE_HIST_DIR, backup_dir)
        status["started_at"]  = datetime.now(IST).isoformat()
        status["backup_dir"]  = backup_dir
        save_status(status, threading.Lock())
        print("   Backup complete.")
    else:
        print(f"\n♻️  Resuming previous run "
              f"({len(status['completed'])} (symbol, tf) already done).")

    # ── Build job list ────────────────────────────────────────────────────
    all_jobs = [(s, tf) for s in all_symbols for tf in TIMEFRAMES]
    pending  = [j for j in all_jobs if not status["completed"].get(key(*j))]
    total    = len(all_jobs)
    todo     = len(pending)
    print(f"\n📋 Jobs: {total} total, {todo} pending, "
          f"{total - todo} already done.")

    if todo == 0:
        print("Nothing to do.")
    else:
        # ── Refetch (parallel) ────────────────────────────────────────────
        progress      = {"done": total - todo, "total": total, "failed": 0}
        progress_lock = threading.Lock()
        status_lock   = threading.Lock()

        # Split pending into N roughly equal chunks for WORKERS workers
        chunks = [pending[i::WORKERS] for i in range(WORKERS)]
        print(f"\n⚙️  Refetching with {WORKERS} workers...")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [
                pool.submit(worker, hm, chunk, status,
                            status_lock, progress, progress_lock)
                for chunk in chunks
            ]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    print(f"   ⚠️  Worker error: {e}")
        elapsed = time.time() - t0
        print(f"\n✅ Refetch complete in {elapsed/60:.1f} min "
              f"(failures: {progress['failed']}).")

    # ── Retry failures one more time, single-threaded ─────────────────────
    failed_keys = list(status["failed"].keys())
    if failed_keys:
        print(f"\n🔁 Retrying {len(failed_keys)} failed (symbol, tf) "
              f"single-threaded...")
        single_lock = threading.Lock()
        for k in failed_keys:
            sym, tf = k.split("|")
            try:
                n = fetch_one(hm, sym, tf)
                with single_lock:
                    status["completed"][k] = {
                        "candles": n,
                        "at": datetime.now(IST).isoformat()
                    }
                    status["failed"].pop(k, None)
                save_status(status, single_lock)
                print(f"   ✓ {sym} {tf}m  ({n} candles)")
            except Exception as e:
                print(f"   ✗ {sym} {tf}m  — {e}")
            time.sleep(0.3)

    # ── Cache reset ───────────────────────────────────────────────────────
    if os.path.exists(RSI_CACHE_PATH):
        print(f"\n🗑️  Deleting stale rsi_cache.pkl so it rebuilds from "
              f"fresh CSVs on next scanner startup...")
        os.remove(RSI_CACHE_PATH)

    # Also remove the SOLARINDS test artefact from yesterday's session
    leftover = os.path.join(ROOT, "data", "_solarinds_15m_FRESH.csv")
    if os.path.exists(leftover):
        os.remove(leftover)

    # ── Sanity check: SOLARINDS 15m RSI at 21-May 09:15 candle close ──────
    print("\n🧪 Sanity check: SOLARINDS 15m RSI at 21-May 09:15 candle close")
    print("   (Fyers chart reading was 73.94 — fresh data should match.)")
    ds = DataStore()
    candles = ds.load_candles("NSE:SOLARINDS-EQ", "15")
    if candles:
        target = IST.localize(datetime(2026, 5, 21, 9, 15))
        closes = [c["close"] for c in candles if c["datetime"] <= target]

        def wilder(cs, period=14):
            if len(cs) < period + 1:
                return None
            g = [max(0,  cs[i] - cs[i-1]) for i in range(1, period+1)]
            l = [max(0, -(cs[i] - cs[i-1])) for i in range(1, period+1)]
            ag, al = sum(g)/period, sum(l)/period
            for i in range(period+1, len(cs)):
                ch = cs[i] - cs[i-1]
                ag = (ag * (period-1) + max(0,  ch)) / period
                al = (al * (period-1) + max(0, -ch)) / period
            if al == 0:
                return 100.0
            return 100 - 100/(1 + ag/al)

        rsi = wilder(closes)
        gap = abs(rsi - 73.94) if rsi is not None else None
        print(f"   Computed RSI : {rsi:.2f}")
        print(f"   Chart RSI    : 73.94")
        print(f"   Gap          : {gap:.2f}" if gap is not None else "")
        if gap is not None and gap > 0.5:
            print("\n⚠️  Sanity check OUTSIDE the 0.5-point tolerance.")
            print("    Inspect data/price_history/NSE_SOLARINDS-EQ_15m.csv.")
            print(f"    Backup at: {status.get('backup_dir','?')}")
        else:
            print("\n✅ Sanity check passed.  Fresh CSVs match the chart.")
    else:
        print("   ⚠️  SOLARINDS 15m CSV not found — sanity check skipped.")

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "─" * 72)
    print(f"Completed : {len(status['completed'])}/{total}")
    print(f"Failed    : {len(status['failed'])}")
    if status["failed"]:
        print("\nStill-failed (symbol, tf):")
        for k, v in list(status["failed"].items())[:30]:
            print(f"  {k}  — {v.get('error','?')}")
    print(f"\nBackup of original CSVs : {status.get('backup_dir','?')}")
    print(f"Status file (resume)    : {STATUS_FILE}")
    print("\nNext step: restart main.py. The scanner will rebuild rsi_cache")
    print("           from the fresh CSVs on startup.")
    print("─" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
