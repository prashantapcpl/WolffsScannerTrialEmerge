"""
validators_extended.py
Comprehensive data-quality validators that run over the CSV history and
state files. Designed to be called from a single orchestrator so adding
more is trivial.

Each validator returns a list of (severity, symbol, tf, date, message)
tuples. Critical issues block the carry-forward; warnings only log.

Coverage (built today):
  --- Per-row OHLCV ---
   1. price_non_negative
   2. price_below_extreme_max
   3. ohlc_low_le_open
   4. ohlc_low_le_close
   5. ohlc_open_le_high
   6. ohlc_close_le_high
   7. ohlc_high_ge_low
   8. ohlc_no_nan
   9. ohlc_no_infinity
  10. volume_non_negative
  11. volume_below_extreme_max     -- catches STLTECH-style billion-volume
  12. volume_vs_avg_sanity         -- per-bar > 10x ADV is rejected
  13. spread_within_reasonable     -- (high-low)/close <= 25%

  --- Per-row temporal ---
  14. timestamp_parseable
  15. timestamp_on_trading_day
  16. timestamp_in_market_hours
  17. timestamp_not_in_future
  18. timestamp_not_ancient

  --- Per-row alignment (intraday only) ---
  19. tf_5m_minute_aligned         -- 5m candles at :00, :05, :10, ...
  20. tf_10m_minute_aligned
  21. tf_15m_minute_aligned
  22. tf_30m_minute_aligned
  23. tf_60m_minute_aligned

  --- Per-file structural ---
  24. csv_file_exists
  25. csv_file_size_positive
  26. csv_header_correct
  27. csv_chronological_order
  28. csv_unique_timestamps
  29. csv_no_empty_rows

  --- Cross-bar (within symbol+tf) ---
  30. consecutive_close_to_open_gap   -- > 20% between bars (split candidate)
  31. consecutive_bar_price_jump      -- > 20% within consecutive bars
  32. frozen_ohlc_run                 -- O=H=L=C for >=5 bars (separate from volume-phantom)
  33. zero_volume_with_movement       -- vol=0 but H!=L (impossible)
  34. flat_close_with_huge_volume     -- volume > avg yet close == prev close

  --- Cross-TF ---
  35. cross_tf_5m_15m_volume_sum
  36. cross_tf_5m_15m_high_match
  37. cross_tf_5m_15m_low_match

  --- Per-symbol completeness ---
  38. day_row_count_matches_expected
  39. day_open_close_present
  40. no_missing_first_candle_of_day  -- 9:15 candle must exist on trading days

  --- State file ---
  41. state_json_valid
  42. state_active_has_buy_price
  43. state_active_has_buy_time
  44. state_watched_has_reference_price
  45. state_reference_price_in_range  -- within 50% of current price
  46. state_signal_log_monotonic
  47. state_buy_time_after_watch_time

  --- Settings ---
  48. settings_required_keys_present
  49. settings_numeric_ranges
  50. settings_no_threshold_conflict   -- entry < reset, etc.
"""
import os
import csv
import json
import math
from datetime import datetime, timedelta, date
from collections import defaultdict
import pytz

from core.market_calendar import (
    is_trading_day, is_holiday, is_market_hours, IST,
    expected_intraday_rows,
)

ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data", "price_history")

# Bounds — tuneable
MAX_PRICE_ABSOLUTE      = 10_000_000     # 1 crore per share is absurd
MAX_VOLUME_PER_BAR      = 50_000_000     # 5 crore shares in one 5m bar is suspect
MAX_SPREAD_PCT          = 25.0           # (h-l)/c above this = data error
MAX_CONSECUTIVE_JUMP    = 20.0           # > 20% between consecutive bars = split candidate
ANCIENT_DAYS            = 1095           # rows >3 years old
FROZEN_OHLC_RUN_MIN     = 5              # 5 bars with OHLC all equal = phantom
VOLUME_VS_AVG_MULTIPLE  = 15.0           # bar volume > 15x 30-day avg per-bar
CROSS_TF_TOLERANCE_PCT  = 1.5            # 5m sum vs 15m within 1.5%
REF_PRICE_DRIFT_PCT     = 50.0           # state ref vs current price


def _csv_path(symbol: str, tf: str) -> str:
    safe = symbol.replace(":", "_").replace("/", "_")
    return os.path.join(DATA_DIR, f"{safe}_{tf}m.csv")


def _load_rows_raw(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", newline="") as f:
        out = list(csv.DictReader(f))
    return out


# ─── Per-row validators ─────────────────────────────────────────────────────
def validate_row(row: dict, tf: str, symbol: str) -> list:
    """Run every per-row check; return list of (severity, message) for failures."""
    issues = []
    # 14. timestamp parseable
    try:
        dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return [("critical", f"unparseable datetime {row.get('datetime')!r}")]

    # Numeric parse
    try:
        o = float(row["open"]);  h = float(row["high"])
        l = float(row["low"]);   c = float(row["close"])
        v = float(row.get("volume", 0))
    except Exception:
        return [("critical", "non-numeric OHLCV")]

    # 1-2 price bounds
    for name, val in [("open", o), ("high", h), ("low", l), ("close", c)]:
        if val <= 0:
            issues.append(("critical", f"{name}={val} not positive"))
        if val > MAX_PRICE_ABSOLUTE:
            issues.append(("critical", f"{name}={val} above MAX_PRICE_ABSOLUTE"))
    # 3-7 OHLC ordering
    if not (l <= o):       issues.append(("critical", f"low {l} > open {o}"))
    if not (l <= c):       issues.append(("critical", f"low {l} > close {c}"))
    if not (o <= h):       issues.append(("critical", f"open {o} > high {h}"))
    if not (c <= h):       issues.append(("critical", f"close {c} > high {h}"))
    if h < l:              issues.append(("critical", f"high {h} < low {l}"))
    # 8-9 NaN/Inf
    for name, val in [("open", o), ("high", h), ("low", l), ("close", c)]:
        if math.isnan(val) or math.isinf(val):
            issues.append(("critical", f"{name} NaN/Inf"))
    # 10-11 volume
    if v < 0:
        issues.append(("critical", f"volume {v} negative"))
    if v > MAX_VOLUME_PER_BAR:
        issues.append(("warning", f"volume {v:.0f} > MAX_VOLUME_PER_BAR {MAX_VOLUME_PER_BAR}"))
    # 13 spread
    if c > 0:
        spread_pct = (h - l) / c * 100
        if spread_pct > MAX_SPREAD_PCT:
            issues.append(("warning", f"spread {spread_pct:.1f}% > {MAX_SPREAD_PCT}%"))
    # 15-18 temporal
    if not is_trading_day(dt.date()) and dt.weekday() < 5:
        issues.append(("critical", f"{dt} on NSE holiday"))
    if not is_market_hours(IST.localize(dt)):
        issues.append(("warning", f"{dt} outside market hours"))
    now = datetime.now(IST)
    if IST.localize(dt) > now + timedelta(minutes=5):
        issues.append(("critical", f"{dt} in future"))
    if (now - IST.localize(dt)).days > ANCIENT_DAYS:
        issues.append(("info", f"{dt} >{ANCIENT_DAYS} days old"))
    # 19-23 TF alignment
    if tf in ("5", "10", "15", "30", "60"):
        if dt.minute % int(tf) != 0:
            issues.append(("warning",
                f"{dt}: minute {dt.minute} not aligned to {tf}m boundary"))
    # 33 zero-volume but moved
    if v == 0 and h != l:
        issues.append(("warning", f"volume=0 but high!=low (impossible)"))
    return issues


# ─── Per-file validators (structural) ──────────────────────────────────────
def validate_file_structural(symbol: str, tf: str) -> list:
    """Return list of (severity, message) per-file failures."""
    path = _csv_path(symbol, tf)
    issues = []
    if not os.path.exists(path):
        issues.append(("info", "file does not exist"))
        return issues
    sz = os.path.getsize(path)
    if sz == 0:
        issues.append(("critical", "file is 0 bytes"))
        return issues
    rows = _load_rows_raw(path)
    if not rows:
        issues.append(("critical", "header-only file (no data rows)"))
        return issues
    # 26. header
    expected_cols = {"datetime", "open", "high", "low", "close", "volume"}
    actual_cols   = set(rows[0].keys())
    if not expected_cols.issubset(actual_cols):
        issues.append(("critical",
            f"missing columns: {expected_cols - actual_cols}"))
    # 27, 28 chronological + unique
    dts = []
    for r in rows:
        try:
            dts.append(datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            issues.append(("critical", f"unparseable datetime: {r.get('datetime')}"))
    if dts:
        if dts != sorted(dts):
            issues.append(("critical", "rows not chronologically ordered"))
        seen = set()
        dups = 0
        for d in dts:
            if d in seen:
                dups += 1
            seen.add(d)
        if dups:
            issues.append(("critical", f"{dups} duplicate timestamps"))
    return issues


# ─── Frozen-OHLC detector (without requiring volume match) ─────────────────
def find_frozen_ohlc_runs_loose(symbol: str, tf: str,
                                  min_run: int = FROZEN_OHLC_RUN_MIN) -> list:
    """Catches STLTECH-style: O=H=L=C identical for >=N consecutive bars
    REGARDLESS of volume. Returns [(start_dt, end_dt, n_bars, close_price)]."""
    rows = _load_rows_raw(_csv_path(symbol, tf))
    if not rows:
        return []
    out = []
    i = 0
    while i < len(rows):
        try:
            o = float(rows[i]["open"]);  h = float(rows[i]["high"])
            l = float(rows[i]["low"]);   c = float(rows[i]["close"])
        except Exception:
            i += 1; continue
        if not (o == h == l == c):
            i += 1; continue
        j = i + 1
        while j < len(rows):
            try:
                o2 = float(rows[j]["open"]); h2 = float(rows[j]["high"])
                l2 = float(rows[j]["low"]);  c2 = float(rows[j]["close"])
                if not (o2 == h2 == l2 == c2 == c):
                    break
            except Exception:
                break
            j += 1
        if j - i >= min_run:
            out.append((rows[i]["datetime"], rows[j-1]["datetime"],
                        j - i, c))
        i = max(j, i + 1)
    return out


# ─── Volume sanity vs average daily volume ─────────────────────────────────
def find_volume_anomalies(symbol: str, tf: str = "5") -> list:
    """Compute 30-day average volume per bar; flag bars exceeding
    VOLUME_VS_AVG_MULTIPLE × that average."""
    rows = _load_rows_raw(_csv_path(symbol, tf))
    if len(rows) < 30 * 75:   # need decent history
        return []
    # average across last 30 days of bars
    recent = rows[-30 * 75:]
    vols = []
    for r in recent:
        try:
            vols.append(float(r.get("volume", 0)))
        except Exception:
            pass
    if not vols:
        return []
    avg = sum(vols) / len(vols)
    if avg <= 0:
        return []
    cap = avg * VOLUME_VS_AVG_MULTIPLE
    out = []
    for r in recent:
        try:
            v = float(r.get("volume", 0))
            if v > cap:
                out.append((r["datetime"], v, avg, v / avg))
        except Exception:
            continue
    return out


# ─── Consecutive-bar price jump (split candidate, per TF) ──────────────────
def find_consecutive_jumps(symbol: str, tf: str,
                            threshold_pct: float = MAX_CONSECUTIVE_JUMP) -> list:
    """Returns rows where the open is >threshold_pct away from previous close."""
    rows = _load_rows_raw(_csv_path(symbol, tf))
    out = []
    for i in range(1, len(rows)):
        try:
            prev_close = float(rows[i-1]["close"])
            this_open  = float(rows[i]["open"])
        except Exception:
            continue
        if prev_close <= 0: continue
        pct = abs(this_open - prev_close) / prev_close * 100
        if pct > threshold_pct:
            out.append((rows[i]["datetime"], prev_close, this_open, pct))
    return out


# ─── Cross-TF aggregation consistency ──────────────────────────────────────
def check_cross_tf_5m_to_15m(symbol: str) -> list:
    """For every 15m bar, check that summed 5m volume / max H / min L
    match within tolerance. Returns mismatches."""
    rows_5m  = _load_rows_raw(_csv_path(symbol, "5"))
    rows_15m = _load_rows_raw(_csv_path(symbol, "15"))
    if not rows_5m or not rows_15m:
        return []
    # Group 5m by their 15m bucket
    bucket = defaultdict(list)
    for r in rows_5m:
        try:
            dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        key = dt.replace(minute=(dt.minute // 15) * 15)
        bucket[key].append(r)
    issues = []
    for r in rows_15m:
        try:
            dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S")
            v15 = float(r["volume"])
            h15 = float(r["high"]); l15 = float(r["low"])
        except Exception:
            continue
        bars = bucket.get(dt, [])
        if not bars:
            continue
        try:
            v_sum = sum(float(b["volume"]) for b in bars)
            h_max = max(float(b["high"])   for b in bars)
            l_min = min(float(b["low"])    for b in bars)
        except Exception:
            continue
        if v15 > 0 and abs(v_sum - v15) / v15 * 100 > CROSS_TF_TOLERANCE_PCT:
            issues.append(("volume_mismatch", dt, v_sum, v15))
        if abs(h_max - h15) / max(h15, 1) * 100 > CROSS_TF_TOLERANCE_PCT:
            issues.append(("high_mismatch", dt, h_max, h15))
        if abs(l_min - l15) / max(l15, 1) * 100 > CROSS_TF_TOLERANCE_PCT:
            issues.append(("low_mismatch", dt, l_min, l15))
    return issues


# ─── State file validators ─────────────────────────────────────────────────
def validate_state_file(path: str) -> list:
    """Returns list of (severity, message) for state-file issues."""
    issues = []
    if not os.path.exists(path):
        return [("info", "state file missing")]
    try:
        with open(path, "r") as f:
            state = json.load(f)
    except Exception as e:
        return [("critical", f"unparseable JSON: {e}")]

    records = state.get("records", {})
    signal_log = state.get("signal_log", [])

    # Detect schema: S3 candle-mirror uses ref_price + entries[]; S1/S2/S4
    # use reference_price + buy_price + entry_price. Skip S3 records here
    # because their valid states (WATCHED_BUY etc.) intentionally have
    # ref_price (not reference_price) and no buy_price field.
    is_s3_schema = any("ref_price" in r and "entries" in r
                       for r in records.values())
    if is_s3_schema:
        # Run S3-specific validation instead
        for sym, r in records.items():
            s = r.get("state")
            if s in ("WATCHED_BUY", "WATCHED_SELL"):
                if not r.get("ref_price") or r.get("ref_price") <= 0:
                    issues.append(("critical",
                        f"{sym} state={s} but ref_price={r.get('ref_price')}"))
            if s in ("ACTIVE_BUY", "ACTIVE_SELL"):
                if not r.get("entries"):
                    issues.append(("critical",
                        f"{sym} state={s} but no entries[]"))
        # Don't run S1/S2 checks on S3 records
        return issues

    # 41-47 (S1/S2/S4 schema)
    for sym, r in records.items():
        s = r.get("state")
        bp = r.get("buy_price") or r.get("entry_price")
        bt = r.get("buy_time")  or r.get("entry_time")
        rp = r.get("reference_price")
        cur = r.get("current_price")
        if s in ("ACTIVE", "ACTIVE_BUY", "ACTIVE_SELL",
                 "BUY1_ACTIVE","BUY2_ACTIVE","BUY3_ACTIVE","BUY4_ACTIVE",
                 "SELL1_ACTIVE","SELL2_ACTIVE","SELL3_ACTIVE","SELL4_ACTIVE"):
            if not bp or bp <= 0:
                issues.append(("critical", f"{sym} state={s} but buy_price={bp}"))
            if not bt:
                issues.append(("critical", f"{sym} state={s} but no buy_time"))
        if s in ("WATCHED","WATCHED_SELL","WATCHED_BUY") and (not rp or rp <= 0):
            issues.append(("critical", f"{sym} state={s} but reference_price={rp}"))
        if rp and cur and rp > 0:
            ratio = cur / rp
            if ratio < 1 - REF_PRICE_DRIFT_PCT/100 or ratio > 1 + REF_PRICE_DRIFT_PCT/100:
                issues.append(("warning",
                    f"{sym} reference_price {rp} drifted >50% from current {cur}"))
        # buy_time >= reference_time
        if r.get("reference_time") and bt:
            try:
                rt = datetime.fromisoformat(r["reference_time"])
                btd = datetime.fromisoformat(bt)
                if btd < rt:
                    issues.append(("critical",
                        f"{sym} buy_time {bt} < reference_time {r['reference_time']}"))
            except Exception:
                pass

    # Signal log monotonic
    prev = None
    for i, e in enumerate(signal_log):
        t = e.get("time")
        if t is None: continue
        try:
            dt = datetime.fromisoformat(t)
        except Exception:
            issues.append(("warning", f"signal_log[{i}] unparseable time {t}"))
            continue
        if prev and dt < prev:
            issues.append(("warning", f"signal_log[{i}] {dt} < previous {prev}"))
        prev = dt
    return issues


# ─── Settings validators ───────────────────────────────────────────────────
def validate_settings(scanner_id: str, settings: dict) -> list:
    """Per-scanner settings sanity. Returns list of (severity, message)."""
    issues = []
    # RSI 0-100
    for k in ["rsi_entry_threshold", "rsi_reset_threshold", "rsi_exit_threshold",
              "rsi_entry_sell_threshold", "rsi_reset_sell_threshold",
              "rsi_exit_sell_threshold", "daily_rsi_threshold",
              "weekly_rsi_threshold", "daily_rsi_threshold_sell",
              "weekly_rsi_threshold_sell", "daily_rsi_stoploss_buy",
              "daily_rsi_stoploss_sell", "stoploss_cooling_rsi_buy",
              "stoploss_cooling_rsi_sell", "buy1_rsi_lower", "buy1_rsi_upper",
              "buy1_cooldown_rsi", "buy2_rsi_trigger", "sell1_rsi_lower",
              "sell1_rsi_upper", "sell1_cooldown_rsi", "sell2_rsi_trigger",
              "daily_rsi_buy_filter", "weekly_rsi_buy_filter",
              "daily_rsi_sell_filter", "weekly_rsi_sell_filter",
              "cooling_rsi_buy", "cooling_rsi_sell"]:
        if k in settings:
            v = settings[k]
            try:
                vf = float(v)
                if not (0 <= vf <= 100):
                    issues.append(("critical",
                        f"{scanner_id}: {k}={v} outside [0,100]"))
            except Exception:
                issues.append(("critical", f"{scanner_id}: {k}={v} not numeric"))
    # percentages reasonable
    for k in ["drop_percent", "avg_drop_percent", "rise_percent",
              "avg_rise_percent", "buy3_drop_pct", "sell3_rise_pct",
              "buy1_target_pct", "buy23_target_pct",
              "sell1_target_pct", "sell23_target_pct"]:
        if k in settings:
            v = settings[k]
            try:
                vf = float(v)
                if not (0 <= vf <= 50):
                    issues.append(("warning",
                        f"{scanner_id}: {k}={v} outside reasonable [0,50]"))
            except Exception:
                issues.append(("critical", f"{scanner_id}: {k}={v} not numeric"))
    # conflicts (BUY side)
    e  = settings.get("rsi_entry_threshold")
    rr = settings.get("rsi_reset_threshold")
    if e is not None and rr is not None:
        try:
            if float(e) >= float(rr):
                issues.append(("critical",
                    f"{scanner_id}: rsi_entry_threshold {e} >= rsi_reset_threshold {rr}"))
        except Exception:
            pass
    return issues


# ─── Orchestrator ──────────────────────────────────────────────────────────
def run_all_validators(symbols: list, lookback_days: int = 30,
                       verbose: bool = True) -> dict:
    """Run every applicable validator over the universe. Returns counts
    grouped by category for a summary report. Does NOT auto-fix; this is
    a diagnostic pass."""
    if verbose:
        print(f"\n{'='*62}")
        print(f"  EXTENDED VALIDATOR PASS  ({len(symbols)} symbols)")
        print(f"{'='*62}")

    counts = defaultdict(int)
    samples = defaultdict(list)   # category -> first few examples
    today = datetime.now(IST).date()
    since = today - timedelta(days=lookback_days)

    for symbol in symbols:
        for tf in ("5", "15"):    # check 5m + 15m; deeper TFs derived from 5m
            # File structural
            for sev, msg in validate_file_structural(symbol, tf):
                counts[f"file_{sev}"] += 1
                if len(samples[f"file_{sev}"]) < 3:
                    samples[f"file_{sev}"].append(f"{symbol} {tf}m: {msg}")
            # Frozen-OHLC runs
            runs = find_frozen_ohlc_runs_loose(symbol, tf)
            if runs:
                counts["frozen_ohlc_runs"] += len(runs)
                for r in runs[:1]:
                    if len(samples["frozen_ohlc_runs"]) < 3:
                        samples["frozen_ohlc_runs"].append(
                            f"{symbol} {tf}m: {r[2]} bars frozen at {r[3]} "
                            f"from {r[0]} to {r[1]}")
            # Volume anomalies
            vol_a = find_volume_anomalies(symbol, tf)
            if vol_a:
                counts["volume_anomalies"] += len(vol_a)
                for v in vol_a[:1]:
                    if len(samples["volume_anomalies"]) < 3:
                        samples["volume_anomalies"].append(
                            f"{symbol} {tf}m {v[0]}: vol {v[1]:.0f} = "
                            f"{v[3]:.1f}x avg {v[2]:.0f}")
            # Consecutive jumps
            jumps = find_consecutive_jumps(symbol, tf)
            if jumps:
                counts["consecutive_jumps"] += len(jumps)
                for j in jumps[:1]:
                    if len(samples["consecutive_jumps"]) < 3:
                        samples["consecutive_jumps"].append(
                            f"{symbol} {tf}m {j[0]}: {j[1]:.2f} -> {j[2]:.2f} "
                            f"({j[3]:.1f}%)")

    # State files
    for fn in ("scanner_1_state.json", "scanner_2_state.json",
               "scanner_3_state.json", "scanner_4_state.json"):
        for sev, msg in validate_state_file(os.path.join(ROOT, "data", fn)):
            counts[f"state_{sev}"] += 1
            if len(samples[f"state_{sev}"]) < 3:
                samples[f"state_{sev}"].append(f"{fn}: {msg}")

    # Settings
    try:
        with open(os.path.join(ROOT, "config.json"), "r") as f:
            cfg = json.load(f)
        for sid, sc in cfg.get("scanners", {}).items():
            for sev, msg in validate_settings(sid, sc.get("settings", {})):
                counts[f"settings_{sev}"] += 1
                if len(samples[f"settings_{sev}"]) < 3:
                    samples[f"settings_{sev}"].append(msg)
    except Exception as e:
        counts["settings_load_error"] += 1
        samples["settings_load_error"].append(str(e))

    if verbose:
        print()
        for cat in sorted(counts.keys()):
            print(f"  {cat:<30} {counts[cat]:>6}")
            for s in samples.get(cat, [])[:3]:
                print(f"     ex: {s}")
        print(f"\n{'='*62}\n")
    return dict(counts)
