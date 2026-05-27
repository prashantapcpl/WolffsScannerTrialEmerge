"""
data_integrity.py
Single source of truth for "is our stored historical data trustworthy?"

Responsibilities:
  1. Per-day row-count validation (vs market_calendar.expected_intraday_rows)
  2. OHLC sanity (open <= high, low <= open, low <= close <= high, non-negative)
  3. Phantom-data detection (frozen OHLCV runs that indicate websocket
     disconnect / Fyers API returning cached stale ticks)
  4. Auto-fix via Fyers force-fetch (uses HistoryManager._fetch_range)
  5. Re-validation after fix (proves we actually closed the gap)
  6. Final report: any gap that couldn't be fixed gets flagged to console

Used at startup (before carry-forward) and after market close (15:35 IST).
Designed so it can be called repeatedly without harm.
"""

import os
import csv
import time
from datetime import datetime, timedelta, date
from collections import defaultdict
import pytz

from core.market_calendar import (
    is_trading_day, is_market_hours, expected_intraday_rows,
    trading_days_between, IST,
)

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data", "price_history")

# Intraday TFs we validate. D/W are derived from 5m / daily-CSV so they get
# checked indirectly (if 5m is complete, D is complete).
INTRADAY_TFS = ("5", "10", "15", "30", "60")

# Row count must be >= EXPECTED * THRESHOLD to be considered OK. Strict (1.0)
# would flag legitimate Fyers-side micro-gaps; 0.93 catches real holes.
ROW_COUNT_THRESHOLD = 0.93

# A run of >=N consecutive identical OHLCV rows = phantom data
PHANTOM_RUN_MIN = 5


def _csv_path(symbol: str, tf: str) -> str:
    safe = symbol.replace(":", "_").replace("/", "_")
    return os.path.join(DATA_DIR, f"{safe}_{tf}m.csv")


def _load_rows(symbol: str, tf: str) -> list:
    """Parsed rows for a symbol×tf — returns list of dicts with parsed dt."""
    path = _csv_path(symbol, tf)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", newline="") as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S")
                out.append({
                    "datetime": dt,
                    "open":   float(r["open"]),
                    "high":   float(r["high"]),
                    "low":    float(r["low"]),
                    "close":  float(r["close"]),
                    "volume": float(r.get("volume", 0)),
                })
            except Exception:
                continue
    return out


# ─── Validators ─────────────────────────────────────────────────────────────
def find_row_count_gaps(symbol: str, tf: str, since: date, until: date) -> list:
    """List of (date, have, expected) for trading days where row count is short."""
    rows = _load_rows(symbol, tf)
    if not rows:
        return [(d, 0, expected_intraday_rows(d, tf))
                for d in trading_days_between(since, until)]

    by_day = defaultdict(int)
    for r in rows:
        d = r["datetime"].date()
        by_day[d] += 1

    gaps = []
    for d in trading_days_between(since, until):
        exp  = expected_intraday_rows(d, tf)
        have = by_day.get(d, 0)
        if have < int(exp * ROW_COUNT_THRESHOLD):
            gaps.append((d, have, exp))
    return gaps


def find_ohlc_violations(symbol: str, tf: str) -> list:
    """Return rows that violate basic OHLC sanity (l<=o, l<=c, o<=h, c<=h, v>=0)."""
    bad = []
    for r in _load_rows(symbol, tf):
        o, h, l, c, v = r["open"], r["high"], r["low"], r["close"], r["volume"]
        if not (l <= o <= h and l <= c <= h and v >= 0):
            bad.append(r)
        elif h < l:
            bad.append(r)
    return bad


def find_timestamp_duplicates(symbol: str, tf: str) -> list:
    """Return datetimes that appear more than once in the CSV."""
    rows = _load_rows(symbol, tf)
    seen = {}
    dups = []
    for r in rows:
        dt = r["datetime"]
        seen[dt] = seen.get(dt, 0) + 1
    return [dt for dt, c in seen.items() if c > 1]


def find_chronological_violations(symbol: str, tf: str) -> list:
    """Return (idx, prev_dt, curr_dt) where the curr row's dt is <= prev row's."""
    rows = _load_rows(symbol, tf)
    out = []
    for i in range(1, len(rows)):
        if rows[i]["datetime"] <= rows[i-1]["datetime"]:
            out.append((i, rows[i-1]["datetime"], rows[i]["datetime"]))
    return out


def find_holiday_rows(symbol: str, tf: str) -> list:
    """Return rows that fall on NSE holidays (using market_calendar)."""
    from core.market_calendar import is_holiday, is_trading_day
    rows = _load_rows(symbol, tf)
    return [r for r in rows
            if not is_trading_day(r["datetime"].date())
            and r["datetime"].weekday() < 5]   # weekday but holiday


def find_closing_auction_rows(symbol: str, tf: str) -> list:
    """Return rows that fall in 15:30-15:40 closing auction window for
    intraday TFs — Fyers sometimes returns these settlement candles which
    have artificial prices."""
    if tf in ("D", "W"):
        return []
    rows = _load_rows(symbol, tf)
    bad = []
    for r in rows:
        h, m = r["datetime"].hour, r["datetime"].minute
        if h == 15 and m >= 30:
            bad.append(r)
        elif h > 15:
            bad.append(r)
    return bad


def find_split_candidates(symbol: str) -> list:
    """Detect potential stock-split / bonus events on the DAILY series.

    Returns list of (date, prev_close, this_open, ratio) where the open
    gapped >40% from previous close — usually a split / bonus / demerger.
    """
    rows = _load_rows(symbol, "5")    # use 5m to derive daily-from-5m equivalent
    if not rows:
        return []
    # Build daily closes (last 5m close of each day)
    from collections import defaultdict
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["datetime"].date()].append(r)
    days = sorted(by_day.keys())
    events = []
    for i in range(1, len(days)):
        prev_close = by_day[days[i-1]][-1]["close"]
        this_open  = by_day[days[i]][0]["open"]
        if prev_close <= 0:
            continue
        ratio = this_open / prev_close
        # >40% gap either direction looks like a corporate action
        if ratio < 0.6 or ratio > 1.6:
            events.append((days[i], prev_close, this_open, ratio))
    return events


def find_state_invariant_violations(state_records: dict) -> list:
    """Validate state-machine invariants. Returns list of (symbol, issue) tuples.

    Pass state_records = state_store._records (a dict symbol -> StockRecord
    or S4Record). Works for both StockRecord and S4Record shapes.
    """
    issues = []
    for sym, rec in state_records.items():
        s = getattr(rec, "state", None)
        if s is None:
            continue
        # ACTIVE_BUY / BUY*_ACTIVE require buy_price/entry_price
        if s in ("ACTIVE", "ACTIVE_BUY",
                 "BUY1_ACTIVE", "BUY2_ACTIVE", "BUY3_ACTIVE", "BUY4_ACTIVE"):
            bp = getattr(rec, "buy_price", None) or getattr(rec, "entry_price", None)
            if not bp or bp <= 0:
                issues.append((sym, f"state={s} but no positive buy/entry price ({bp})"))
            bt = getattr(rec, "buy_time", None) or getattr(rec, "entry_time", None)
            if not bt:
                issues.append((sym, f"state={s} but no buy/entry time"))
        # WATCHED requires reference_price
        if s in ("WATCHED", "WATCHED_SELL", "WATCHED_BUY"):
            ref = getattr(rec, "reference_price", None)
            if not ref or ref <= 0:
                issues.append((sym, f"state={s} but no positive reference_price ({ref})"))
        # Reference price sanity: within +/- 80% of current_price (catches
        # ancient/stale refs from bad data eras)
        ref = getattr(rec, "reference_price", None)
        cur = getattr(rec, "current_price", None)
        if ref and cur and ref > 0:
            ratio = cur / ref
            if ratio < 0.5 or ratio > 1.8:
                issues.append((sym,
                    f"reference_price {ref} far from current_price {cur} (ratio={ratio:.2f})"))
    return issues


def find_frozen_ohlc_only(symbol: str, tf: str) -> list:
    """Catches STLTECH-style corruption: 5+ consecutive bars where
    O == H == L == C, but volume varies row-to-row (so passes the strict
    OHLCV-phantom detector). For liquid stocks during market hours, having
    OHLC pinned at the SAME exact tick for 25+ minutes is impossible."""
    rows = _load_rows(symbol, tf)
    out  = []   # list of (start_idx, length)
    i = 0
    while i < len(rows):
        if rows[i]["open"] == rows[i]["high"] == rows[i]["low"] == rows[i]["close"]:
            j = i + 1
            while j < len(rows):
                if (rows[j]["open"] == rows[j]["high"] == rows[j]["low"] == rows[j]["close"]
                        == rows[i]["close"]):
                    j += 1
                else:
                    break
            if j - i >= PHANTOM_RUN_MIN:
                out.append((i, j - i))
            i = j
        else:
            i += 1
    return out


def find_phantom_runs(symbol: str, tf: str) -> list:
    """Return start-indices of OHLCV-frozen runs (>= PHANTOM_RUN_MIN)."""
    rows = _load_rows(symbol, tf)
    runs = []
    i = 0
    while i < len(rows):
        j = i + 1
        while j < len(rows):
            if (rows[j]["open"]   == rows[i]["open"]   and
                rows[j]["high"]   == rows[i]["high"]   and
                rows[j]["low"]    == rows[i]["low"]    and
                rows[j]["close"]  == rows[i]["close"]  and
                rows[j]["volume"] == rows[i]["volume"]):
                j += 1
            else:
                break
        if j - i >= PHANTOM_RUN_MIN:
            runs.append((i, j - i))
        i = j if j > i else i + 1
    return runs


# ─── Bulk scan ──────────────────────────────────────────────────────────────
def scan_all(symbols: list, since: date, until: date,
             tfs: tuple = INTRADAY_TFS) -> dict:
    """
    Walk every (symbol, tf) and collect issues. Fast — no API calls.

    Returns:
      {
        "row_gaps":   {(symbol, tf): [(date, have, expected), ...]},
        "ohlc_bad":   {(symbol, tf): [row, ...]},
        "phantoms":   {(symbol, tf): [(start_idx, length), ...]},
      }
    """
    out = {
        "row_gaps": {}, "ohlc_bad": {}, "phantoms": {},
        "dup_timestamps": {}, "chron_violations": {},
        "holiday_rows": {}, "closing_auction": {},
    }
    for symbol in symbols:
        for tf in tfs:
            gaps = find_row_count_gaps(symbol, tf, since, until)
            if gaps:
                out["row_gaps"][(symbol, tf)] = gaps
            bad = find_ohlc_violations(symbol, tf)
            if bad:
                out["ohlc_bad"][(symbol, tf)] = bad
            runs = find_phantom_runs(symbol, tf)
            if runs:
                out["phantoms"][(symbol, tf)] = runs
            frozen = find_frozen_ohlc_only(symbol, tf)
            if frozen:
                out.setdefault("frozen_ohlc", {})[(symbol, tf)] = frozen
            dups = find_timestamp_duplicates(symbol, tf)
            if dups:
                out["dup_timestamps"][(symbol, tf)] = dups
            chrons = find_chronological_violations(symbol, tf)
            if chrons:
                out["chron_violations"][(symbol, tf)] = chrons
            hols = find_holiday_rows(symbol, tf)
            if hols:
                out["holiday_rows"][(symbol, tf)] = hols
            ca = find_closing_auction_rows(symbol, tf)
            if ca:
                out["closing_auction"][(symbol, tf)] = ca
    return out


def fix_csv_structural(report: dict) -> dict:
    """Rewrite each affected CSV after removing duplicates, sorting
    chronologically, and dropping holiday + closing-auction rows.
    Returns counts of rows removed per category."""
    affected_combos = set()
    affected_combos.update(report.get("dup_timestamps", {}).keys())
    affected_combos.update(report.get("chron_violations", {}).keys())
    affected_combos.update(report.get("holiday_rows", {}).keys())
    affected_combos.update(report.get("closing_auction", {}).keys())

    removed = {"dups": 0, "holiday": 0, "closing_auction": 0, "sorted": 0}
    from core.market_calendar import is_trading_day

    for sym, tf in affected_combos:
        rows = _load_rows(sym, tf)
        if not rows:
            continue
        before = len(rows)

        # Strip holiday rows (intraday TFs only)
        if tf not in ("D", "W"):
            kept = []
            for r in rows:
                d = r["datetime"].date()
                # weekend already stripped elsewhere; only flag weekday holidays
                if not is_trading_day(d) and r["datetime"].weekday() < 5:
                    removed["holiday"] += 1
                    continue
                # Closing auction 15:30+
                h, m = r["datetime"].hour, r["datetime"].minute
                if (h == 15 and m >= 30) or h > 15:
                    removed["closing_auction"] += 1
                    continue
                kept.append(r)
            rows = kept

        # Sort + dedupe
        rows.sort(key=lambda r: r["datetime"])
        seen = set()
        unique = []
        for r in rows:
            if r["datetime"] in seen:
                removed["dups"] += 1
                continue
            seen.add(r["datetime"])
            unique.append(r)

        if before != len(unique):
            removed["sorted"] += 1
            path = _csv_path(sym, tf)
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["datetime","open","high","low","close","volume"])
                w.writeheader()
                for r in unique:
                    w.writerow({
                        "datetime": r["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                        "open":     round(r["open"],   2),
                        "high":     round(r["high"],   2),
                        "low":      round(r["low"],    2),
                        "close":    round(r["close"],  2),
                        "volume":   int(r["volume"]),
                    })
    return removed


# ─── Auto-fix ───────────────────────────────────────────────────────────────
def fix_gaps(report: dict, history_manager, data_store,
             since: date, until: date,
             sleep_ok: float = 0.25, sleep_429: float = 2.0) -> dict:
    """
    For each (symbol, tf) with gaps, force-fetch the full [since, until]
    range from Fyers and merge.

    history_manager: a HistoryManager instance with active client
    data_store:      a DataStore instance for save_candles_merged

    Returns: dict with counts {"fetched", "skipped", "errors", "rate_limited"}.
    """
    gap_combos = sorted(report.get("row_gaps", {}).keys())
    if not gap_combos:
        return {"fetched": 0, "skipped": 0, "errors": 0, "rate_limited": 0}

    range_start = IST.localize(datetime(since.year, since.month, since.day, 9, 15))
    range_end   = IST.localize(datetime(until.year, until.month, until.day, 15, 30))

    fetched = errors = rl = 0
    for i, (symbol, tf) in enumerate(gap_combos):
        try:
            candles = None
            for attempt in range(3):
                try:
                    candles = history_manager._fetch_range(
                        symbol, tf, range_start, range_end)
                    break
                except Exception as e:
                    msg = str(e).lower()
                    if "429" in msg or "rate" in msg or "limit" in msg:
                        rl += 1
                        time.sleep(sleep_429 * (2 ** attempt))
                    else:
                        raise
            if candles:
                data_store.save_candles_merged(symbol, tf, candles)
                fetched += 1
            time.sleep(sleep_ok)
        except Exception:
            errors += 1

    return {"fetched": fetched, "skipped": 0,
            "errors": errors, "rate_limited": rl}


def fix_phantoms(report: dict, data_store) -> int:
    """Drop phantom runs from disk. Handles BOTH the strict OHLCV-frozen
    (volume-matching) and the loose OHLC-frozen-only (varying volume).
    Returns # rows removed."""
    removed = 0
    # Merge phantom + frozen-OHLC reports
    all_runs = defaultdict(list)
    for k, v in report.get("phantoms", {}).items():
        all_runs[k].extend(v)
    for k, v in report.get("frozen_ohlc", {}).items():
        all_runs[k].extend(v)
    for (symbol, tf), runs in all_runs.items():
        rows = _load_rows(symbol, tf)
        if not rows:
            continue
        # Build a kept-list excluding rows that fall inside any run
        drop_set = set()
        for start, length in runs:
            for k in range(start, start + length):
                drop_set.add(k)
        kept = [r for k, r in enumerate(rows) if k not in drop_set]
        removed += len(rows) - len(kept)
        # Re-write CSV from kept
        path = _csv_path(symbol, tf)
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["datetime","open","high","low","close","volume"])
            w.writeheader()
            for r in kept:
                w.writerow({
                    "datetime": r["datetime"].strftime("%Y-%m-%d %H:%M:%S"),
                    "open":     round(r["open"],   2),
                    "high":     round(r["high"],   2),
                    "low":      round(r["low"],    2),
                    "close":    round(r["close"],  2),
                    "volume":   int(r["volume"]),
                })
    return removed


# ─── Full pipeline ──────────────────────────────────────────────────────────
def run_full_integrity_pass(symbols, history_manager, data_store,
                             lookback_days: int = 30, verbose: bool = True) -> dict:
    """
    Full integrity sweep:
      1. Drop OHLCV-phantom runs from CSVs
      2. Scan for row-count gaps
      3. Force-fetch missing days from Fyers
      4. Re-scan and report any UNFIXABLE gaps to console

    Returns a structured report dict.
    """
    today = datetime.now(IST).date()
    since = today - timedelta(days=lookback_days)

    if verbose:
        print(f"\n{'='*62}")
        print(f"  DATA INTEGRITY PASS  {since} -> {today}")
        print(f"{'='*62}")

    # Pass 1: scan everything, fix structural problems (dups, order, holidays,
    # closing-auction rows, phantoms, frozen-OHLC) — all PURE CSV cleanup,
    # no API calls.
    if verbose:
        print("  [1] Scanning + fixing structural CSV issues...")
    pre_report = scan_all(symbols, since, today)
    n_dups     = sum(len(v) for v in pre_report.get("dup_timestamps",   {}).values())
    n_chron    = sum(len(v) for v in pre_report.get("chron_violations", {}).values())
    n_hol      = sum(len(v) for v in pre_report.get("holiday_rows",     {}).values())
    n_ca       = sum(len(v) for v in pre_report.get("closing_auction",  {}).values())
    n_phantoms = sum(sum(l for _, l in runs)
                     for runs in pre_report.get("phantoms", {}).values())
    n_frozen   = sum(sum(l for _, l in runs)
                     for runs in pre_report.get("frozen_ohlc", {}).values())
    if verbose:
        print(f"      Found: dups={n_dups} chron={n_chron} holidays={n_hol} "
              f"closing-auction={n_ca} phantom-rows={n_phantoms} "
              f"frozen-ohlc={n_frozen}")
    fix_csv_structural(pre_report)
    if n_phantoms or n_frozen:
        fix_phantoms(pre_report, data_store)

    # Pass 2: row-count gaps (after phantoms removed)
    if verbose:
        print("  [2] Scanning row-count gaps...")
    report = scan_all(symbols, since, today)
    n_gap_combos = len(report["row_gaps"])
    n_gap_days   = sum(len(g) for g in report["row_gaps"].values())
    if verbose:
        print(f"      {n_gap_combos} (symbol, tf) combos with gaps "
              f"({n_gap_days} total stock-days).")

    # Pass 3: fix
    fix_stats = {"fetched": 0, "errors": 0, "rate_limited": 0}
    if n_gap_combos:
        if verbose:
            print(f"  [3] Force-fetching {n_gap_combos} combos from Fyers...")
        fix_stats = fix_gaps(report, history_manager, data_store, since, today)
        if verbose:
            print(f"      fetched={fix_stats['fetched']}  "
                  f"errors={fix_stats['errors']}  "
                  f"rate_limited={fix_stats['rate_limited']}")

    # Pass 4: re-scan
    if verbose:
        print("  [4] Re-validating after fix...")
    final_report = scan_all(symbols, since, today)
    final_gaps = len(final_report["row_gaps"])
    final_days = sum(len(g) for g in final_report["row_gaps"].values())

    if final_gaps:
        if verbose:
            print(f"  ⚠️  STILL {final_gaps} combos / {final_days} stock-days "
                  f"unfixable (Fyers has no data, or stock delisted).")
            # Show first 5 unfixable
            for (sym, tf), days in list(final_report["row_gaps"].items())[:5]:
                day_strs = ", ".join(str(d) for d, _, _ in days[:3])
                print(f"     {sym} {tf}m: {day_strs}{'...' if len(days)>3 else ''}")
    else:
        if verbose:
            print(f"  ✅ All gaps fixed. Data complete for {since} -> {today}.")

    if verbose:
        print(f"{'='*62}\n")

    return {
        "phantoms_removed": n_phantoms,
        "initial_gaps":     n_gap_combos,
        "fetched":          fix_stats["fetched"],
        "errors":           fix_stats["errors"],
        "rate_limited":     fix_stats["rate_limited"],
        "final_unfixable":  final_gaps,
        "final_report":     final_report,
    }
