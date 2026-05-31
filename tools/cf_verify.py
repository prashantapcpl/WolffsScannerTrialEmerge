"""
cf_verify.py — Offline carry-forward verification using the new state machine.

What it does
------------
Reads your existing rsi_cache + config.json. For every symbol, runs the
8-scenario-tested state machine (core.carry_forward_state_machine.replay_cycles)
and prints what positions WOULD be restored for the scanner you pick.

Compare the output to your production dashboard's active-buys list. If they
match (minus any that the new forming-D/W rule correctly blocks), the new
state machine is verified — we can then wire it into the live engine safely
in a future session.

Usage
-----
    py -3.11 tools/cf_verify.py --scanner 1
    py -3.11 tools/cf_verify.py --scanner 2 --apply-dw-gate

Flags
-----
    --scanner N            Scanner index (1, 2, etc.) — reads from config.json
    --apply-dw-gate        Apply the new forming-D/W RSI gate at BUY time
                           (User-directive fix #6). Default: off, to first
                           reproduce production behavior, then on to see
                           what changes.
    --symbol  NSE:XXX-EQ   Only show this symbol (debug one stock at a time)
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.carry_forward_state_machine import replay_cycles  # noqa: E402


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_scanner_cfg(cfg: dict, idx: int) -> dict:
    """Return scanner_N config from your config.json, robust to either
    layout (top-level scanner_1 / scanner_2 keys, or nested 'scanners' list)."""
    key = f"scanner_{idx}"
    if key in cfg:
        return cfg[key]
    if "scanners" in cfg:
        scanners = cfg["scanners"]
        if isinstance(scanners, list) and len(scanners) >= idx:
            return scanners[idx - 1]
        if isinstance(scanners, dict) and key in scanners:
            return scanners[key]
    raise KeyError(f"Cannot find {key} in config.json")


def compute_exit_events(rsi_cache, symbol: str, exit_tf: str,
                        rsi_exit_threshold: float) -> list:
    """Pre-compute (date_str, close) tuples for every exit_tf candle where
    RSI rose strictly above the exit threshold. The state machine will use
    the first one that's strictly AFTER the buy time."""
    rsis  = rsi_cache.get_rsi_series(symbol, exit_tf) or []
    cls   = rsi_cache.get_closes(symbol, exit_tf) or []
    dts   = rsi_cache.get_datetimes(symbol, exit_tf) or []
    out   = []
    for r, c, d in zip(rsis, cls, dts):
        if r is not None and r > rsi_exit_threshold:
            out.append((str(d), float(c)))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scanner", type=int, required=True)
    ap.add_argument("--apply-dw-gate", action="store_true",
                    help="Apply new forming-D/W RSI gate at BUY time (Fix #6).")
    ap.add_argument("--symbol", default=None,
                    help="Verify a single symbol only (e.g. NSE:POLICYBZR-EQ)")
    args = ap.parse_args()

    # Load config + rsi_cache
    cfg_path = os.path.join(ROOT, "config.json")
    if not os.path.exists(cfg_path):
        print("❌ config.json not found in repo root.")
        sys.exit(2)
    cfg = load_config(cfg_path)
    s_cfg = get_scanner_cfg(cfg, args.scanner)

    settings = s_cfg.get("settings", s_cfg)
    scan_tf       = str(settings.get("scan_timeframe",   settings.get("trigger_timeframe", "5")))
    trigger_tf    = str(settings.get("trigger_timeframe", scan_tf))
    exit_tf       = str(settings.get("exit_timeframe",    "10"))
    rsi_entry     = float(settings.get("rsi_entry_threshold", 22))
    rsi_reset     = float(settings.get("rsi_reset_threshold", 68))
    rsi_exit_v    = float(settings.get("rsi_exit_threshold",  65))
    drop_pct      = float(settings.get("drop_pct", settings.get("buy_drop_pct", 1.5)))
    avg_pct       = float(settings.get("avg_drop_pct", settings.get("avg_pct", 3.0)))
    daily_filter  = bool(settings.get("daily_rsi_filter",  False))
    weekly_filter = bool(settings.get("weekly_rsi_filter", False))
    d_thresh      = float(settings.get("daily_rsi_threshold",  60))
    w_thresh      = float(settings.get("weekly_rsi_threshold", 60))

    print(f"\n  Scanner {args.scanner} carry-forward verification")
    print(f"  Settings: scan={scan_tf}m  trigger={trigger_tf}m  exit={exit_tf}m  "
          f"entry<{rsi_entry}  reset>{rsi_reset}  exit>{rsi_exit_v}  drop={drop_pct}%")
    print(f"  Filters:  D-RSI>{d_thresh}={daily_filter}   "
          f"W-RSI>{w_thresh}={weekly_filter}   "
          f"DW-gate-at-buy={args.apply_dw_gate}\n")

    # Load rsi cache + symbol list
    from core.rsi_cache import get_rsi_cache
    from core.symbol_map import SymbolMapper
    rsi_cache = get_rsi_cache()
    rsi_cache.load()
    mapper    = SymbolMapper()
    symbols   = ([args.symbol] if args.symbol
                 else mapper.get_all_fyers_symbols())

    actives  = []
    watcheds = []
    skipped  = 0

    for sym in symbols:
        rsis  = rsi_cache.get_rsi_series(sym, scan_tf) or []
        cls   = rsi_cache.get_closes(sym, scan_tf) or []
        dts   = [str(d) for d in (rsi_cache.get_datetimes(sym, scan_tf) or [])]
        if len(rsis) < 20:
            skipped += 1
            continue

        exit_events = compute_exit_events(rsi_cache, sym, exit_tf, rsi_exit_v)
        r = replay_cycles(
            rsi_series  = rsis, closes = cls, date_strs = dts,
            exit_events = exit_events,
            rsi_entry   = rsi_entry, rsi_reset = rsi_reset,
            drop_pct    = drop_pct, avg_pct   = avg_pct,
        )

        if r.final_state == "active":
            # Apply Fix #6 D/W gate at buy time (forming D/W RSI).
            if args.apply_dw_gate and r.buy_time:
                pit = r.buy_time
                blocked = False
                if daily_filter:
                    d_rsi = _point_in_time(rsi_cache, sym, "D", pit)
                    if d_rsi is not None and d_rsi < d_thresh:
                        watcheds.append((sym, r.ref_price, r.ref_time,
                                         f"BLOCKED-D({d_rsi:.1f}<{d_thresh})"))
                        blocked = True
                if not blocked and weekly_filter:
                    w_rsi = _point_in_time(rsi_cache, sym, "W", pit)
                    if w_rsi is not None and w_rsi < w_thresh:
                        watcheds.append((sym, r.ref_price, r.ref_time,
                                         f"BLOCKED-W({w_rsi:.1f}<{w_thresh})"))
                        blocked = True
                if blocked:
                    continue
            actives.append((sym, r.ref_price, r.ref_time,
                            r.buy_price, r.buy_time, r.n_cycles))
        elif r.final_state == "watched":
            watcheds.append((sym, r.ref_price, r.ref_time, "watched"))

    print(f"  ─── ACTIVE BUYS ({len(actives)}) ───────────────────────────────────")
    for sym, ref_p, ref_t, buy_p, buy_t, n_cyc in actives:
        plain = mapper.get_plain_name(sym)
        print(f"    {plain:<14}  ref=₹{ref_p:>9.2f} ({ref_t[-8:-3] if ref_t else '--:--'})  "
              f"buy=₹{buy_p:>9.2f} ({buy_t[-8:-3] if buy_t else '--:--'})  "
              f"prior_cycles={n_cyc}")

    print(f"\n  ─── WATCHED ({len(watcheds)}) ─────────────────────────────────────")
    # Only show first 20 watched to keep output readable
    for sym, ref_p, ref_t, tag in watcheds[:20]:
        plain = mapper.get_plain_name(sym)
        ref_p_s = f"₹{ref_p:>9.2f}" if ref_p else "?"
        ref_t_s = ref_t[-8:-3] if ref_t else "--:--"
        print(f"    {plain:<14}  ref={ref_p_s} ({ref_t_s})  {tag}")
    if len(watcheds) > 20:
        print(f"    ... and {len(watcheds) - 20} more")

    print("\n  ─── SUMMARY ──────────────────────────────────────────────────────")
    print(f"    Active buys: {len(actives)}")
    print(f"    Watched:     {len(watcheds)}")
    print(f"    Skipped (insufficient history): {skipped}")
    print()


def _point_in_time(rsi_cache, sym: str, tf: str, ts: str) -> float | None:
    """Return the last D/W RSI on or before timestamp `ts`. Best-effort —
    returns None if no series for this symbol or no candle qualifies."""
    rsis = rsi_cache.get_rsi_series(sym, tf) or []
    dts  = rsi_cache.get_datetimes(sym, tf) or []
    last = None
    for r, d in zip(rsis, dts):
        if r is None:
            continue
        if str(d) <= ts:
            last = r
        else:
            break
    return last


if __name__ == "__main__":
    main()
