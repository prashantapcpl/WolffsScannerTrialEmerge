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
START.bat          :: launches engine (main.py) + dashboard (port 8503)
```

Backtest:
```bat
BACKTEST.bat       :: opens the backtest dashboard on port 8504
BACKTEST_RUN.bat   :: runs a backtest job in the background
```

> The Wolffs Scanner copy uses ports **8503 / 8504** (and webhook server
> port **5002**) so it can run side-by-side with the original Fyers RSI
> Scanner (ports 8501 / 8502 / 5001) for shadow-mode comparison.
> The dashboard tab uses a custom icon (`assets/wolffs_icon.png`) and is
> titled "Wolffs Scanner" for instant visual distinction in the browser.

> ⚠️  For shadow mode, also change `s3.incoming_webhooks.server_port`
> from `5001` to `5002` in your local `config.json`, and point any real
> outbound webhook URL to a test endpoint (or empty string) so this copy
> doesn't fire real trade signals.

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
| `tools/restore_state.py`    | Roll a scanner's state files back to a carry-forward backup. `--list` to see snapshots; `--from <dir>` to restore. |
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
| `HISTORY_UPDATE_SKIP`            | `false` | Emergency: skip the startup history-update step entirely. Use ONLY for quick restarts where you know CSVs are already fresh from earlier today. |
| `HISTORY_UPDATE_WORKERS`         | `1`     | Number of parallel symbol-fetchers during history update. Increase only after testing — the Fyers SDK is not certified thread-safe. |
| `DATA_INTEGRITY_MAX_AGE_HOURS`   | `0`     | Skip the ~30 min data-integrity pass if the last successful run was less than N hours ago AND left 0 unfixable gaps. `0` = always run. Set to `12` for quick same-day restarts. Marker stored under `data/run_cache/data_integrity.json`. |
| `SCANNER4_REPLAY_MAX_AGE_HOURS`  | `0`     | Skip the ~10 min Scanner 4 30-day replay if cached within N hours and the lookback window is unchanged. `0` = always run. Set to `12` for same-day restarts. Marker under `data/run_cache/scanner4_replay.json`. |
| `CARRY_FORWARD_DRY_RUN`          | `false` | Print what carry-forward WOULD reset across active scanners and exit without touching state. Use after any structural change to inspect impact before going live. |

### Setting env flags on Windows

In Command Prompt, set BEFORE launching `START.bat`:

```cmd
set HISTORY_UPDATE_WORKERS=4
set HISTORY_UPDATE_SKIP=false
START.bat
```

Or, more permanently, edit `START.bat` and add the `set` lines at the top.

## Expected startup times

| Scenario | Workers=1 (default) | Workers=4 | Workers=8 |
|---|---|---|---|
| First-ever run (fetch all history) | 25–40 min | 8–12 min | 4–6 min |
| Daily startup before market open    | 5–10 min  | 2–3 min  | 1–2 min  |
| Restart **during** market hours, data fresh (new fast-skip path) | **30–90 sec** | **20–60 sec** | **15–40 sec** |
| `HISTORY_UPDATE_SKIP=true`          | <1 sec     | —         | —         |

The fast-skip path (added May 28) detects that today's CSV count meets
the expected per-timeframe count and the tail is recent — and skips the
Fyers API call entirely for that symbol×TF. On a healthy mid-day restart
~95% of combos are skipped.

---

## Recent fixes (Sprint May 27–28, 2026)

- **Startup time** went from ~70 min (full pipeline) down to **~1–3 min**
  on a healthy same-day restart, via three skip-caches that respect the
  state of the previous run:
  - **Data integrity pass** (~30 min on cold start) skips when the last
    pass completed cleanly and `DATA_INTEGRITY_MAX_AGE_HOURS` has not
    elapsed. Marker: `data/run_cache/data_integrity.json`.
  - **Scanner 4 full-history replay** (~10 min on cold start) skips when
    `SCANNER4_REPLAY_MAX_AGE_HOURS` has not elapsed and the lookback
    window is unchanged. Marker: `data/run_cache/scanner4_replay.json`.
  - **History update** (~5 min on cold start) fast-skips per-symbol when
    today's CSV count meets the expected threshold and the tail is fresh.
- **Carry-forward safety**: every active scanner's state file is backed
  up to `data/state_backups/<YYYYMMDD_HHMMSS>/` BEFORE any reset.
  Pre/post position counts are printed; >50% drop triggers a loud warning
  with restore instructions. Use `tools/restore_state.py --list` to see
  available backups; `--from <dir>` to roll back. `CARRY_FORWARD_DRY_RUN=true`
  inspects what would happen without mutating state.
- **Atomic state writes**: `StateStore.save()` now writes to a tmp file
  then atomically replaces — a crash mid-write can never leave a
  half-written state file.
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
