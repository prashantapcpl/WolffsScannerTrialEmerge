# Wolffs Scanner

Multi-scanner RSI engine for Indian NSE markets on Fyers v3 (live ticks +
historical bars). Webhook signals on RSI-based entries/exits; backtester
for parameter tuning.

> ⚠️  This repo is a **copy** of the live `WolffsScanner` codebase used
> for iterative improvements. Do not run alongside the production scanner
> without setting `WEBHOOK_URL_OVERRIDE` to a test endpoint (shadow mode).

---

## Quick start (Windows local server)

```bat
INSTALL.bat        :: one-time, installs Python deps (py -3.11)
LOGIN.bat          :: daily, opens Fyers OAuth, paste the redirect URL
START.bat          :: launches engine (main.py) + dashboard (port 8501)
```

Backtest:
```bat
BACKTEST.bat       :: opens the backtest dashboard on port 8502
BACKTEST_RUN.bat   :: runs a backtest job in the background
```

---

## Folder layout (post-cleanup, May 2026)

```
core/        Engine: candle, RSI, data store, history, market calendar,
             carry-forward, replay, state store, runtime monitors.
strategies/  Per-scanner strategy modules (rsi_drop, strategy3_*, strategy4).
backtester/  Offline backtest engine driven by the same strategy classes.
signals/     Webhook sender + Flask webhook server (Scanner 3 mirror).
tools/       Live diagnostic / inspection utilities — keep concise.
tests/       Unit + smoke tests (no live Fyers calls).
debug/       One-off debug/audit scripts kept for historical reference.
             NOT part of the runtime; safe to delete or extend.
data/        Runtime artifacts (gitignored): CSVs, state JSON, RSI cache.
```

---

## Diagnostic tools (the only debug paths you should reach for)

| Tool | Purpose |
|---|---|
| `tools/diagnose_rsi.py`     | Per-stock per-day 3-way RSI parity check: scanner CSV vs Fyers REST vs chart. Pinpoints whether divergence is OHLC drift or RSI drift. |
| `tools/check_daily_drift.py`| Daily drift sanity sweep: samples N symbols, compares scanner's derived daily close vs Fyers' authoritative daily close. |
| `tools/trace_signal.py`     | Replays a single symbol through the strategy engine for a date to reproduce a signal. |

Example:
```bash
python tools/diagnose_rsi.py --symbol NSE:AEQUS-EQ --date 2026-05-22 --tf 5
python tools/check_daily_drift.py --n 25
```

---

## Operational env flags

| Env var | Default | Effect |
|---|---|---|
| `TICK_SANITY_JUMP_CHECK_ENABLED` | `true` | Master switch for the price-jump rejection in `TickSanityValidator`. Set to `false` to fully disable if it's mis-rejecting valid ticks (gap days, circuit hits). |
| `TICK_SANITY_MAX_PCT_JUMP`       | `5.0`   | Mid-session max jump (% of last accepted price). |
| `TICK_SANITY_OPENING_PCT_JUMP`   | `25.0`  | Wider band for 09:15–09:30 IST opening volatility window. |
| `TICK_SANITY_BASELINE_STALE_SEC` | `1800`  | Treat the next tick as "fresh" if the last accepted tick is older than this (handles overnight gaps / reconnects). |

---

## Recent fixes (Sprint May 27–28, 2026)

- **TickSanityValidator** silently dropped legitimate gap-up / gap-down /
  circuit-hit ticks because it compared today's first tick against an
  in-memory baseline from a previous session. Fixed by:
  - skipping the jump check when the baseline is stale (>30 min by default),
  - widening the threshold during the 09:15–09:30 opening window,
  - allowing exact NSE circuit-band moves (±5/10/20%),
  - logging the first N rejections per symbol so corruption is visible,
  - master env switch `TICK_SANITY_JUMP_CHECK_ENABLED`.
  See `tests/test_tick_sanity.py`.
- Added `tools/diagnose_rsi.py` for per-stock RSI parity checks.
- Added `tools/check_daily_drift.py` for daily-close drift sweeps.
- Moved 17 one-off debug scripts from root into `debug/`.
- Removed `dashboard.py.mojibake.bak` and the 449 MB committed
  `data/price_history_backup_*/` directory.
- Tightened `.gitignore` (removed duplicates, added runtime files).
- Pinned `requirements.txt` to compatible-release majors.
