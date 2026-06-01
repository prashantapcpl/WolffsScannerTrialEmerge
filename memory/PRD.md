# Wolffs Scanner Shadow — PRD

**Status:** Phase 1+2 work landed; awaiting user live-validation on Windows.
**Last updated:** 2026-Feb (Emergent E1)
**Audience:** the next agent + the user.

---

## Original problem statement

The user runs a Python-based NSE (Fyers API) RSI scanner on a local
Windows box (`START.bat`). Two recurring pain points:

1. **RSI drift** between the scanner and Fyers charts on a few symbols.
2. **Restart cost > 1 hour** with the dashboard frequently coming up
   *empty* (carry-forward wiping live positions, then a 12-min S4
   replay, plus a save-storm causing WinError 32 PermissionErrors).

This repo (`WolffsScannerTrialEmerge`) is a **shadow** of their production
scanner — same code, ports remapped to 8503 (dashboard) / 5002
(webhook) to coexist with the real money one (`WolffsScannerTrial`).

## Architecture (file map)

```
/app/
├── core/
│   ├── carry_forward.py                    ← state-machine wired in (Feb 26)
│   ├── carry_forward_state_machine.py      ← 8 scenarios unit-tested
│   ├── state_store.py                      ← + bulk_mode + debounce (Feb 26)
│   ├── replay_engine.py                    ← + fast_replay_from_cache (Feb 26)
│   ├── runtime_monitors.py                 ← TickSanity gap-up fix
│   ├── rsi_cache.py / rsi_engine.py
│   ├── run_cache.py                        ← fast-skip marker
│   └── ...
├── strategies/
│   ├── rsi_drop.py                         ← exit-dispatch fix (Feb 26)
│   └── strategy4_momentum.py               ← takes rsi_cache fast path
├── tools/
│   ├── diagnose_rsi.py     check_daily_drift.py
│   ├── cf_verify.py        restore_state.py
├── tests/
│   ├── test_carry_forward_state_machine.py
│   ├── test_exit_dispatch.py               ← NEW (Feb 26)
│   ├── test_fast_replay.py                 ← NEW (Feb 26)
│   ├── test_state_store_bulk.py            ← NEW (Feb 26)
│   ├── test_state_store_safety.py
│   ├── test_run_cache.py
│   └── test_tick_sanity.py
├── main.py  dashboard.py  backtest_dashboard.py
├── START.bat  BACKTEST.bat  LOGIN.bat
└── config.json (NOT in repo — user-owned)
```

## What's implemented (CHANGELOG)

### 2026-Feb-26 (this session)
- **Carry-forward state-machine wired in** (`core/carry_forward.py`
  L380-432). Old single-cycle walk loop is gone; the 8-scenario tested
  `replay_cycles()` now drives the watch/buy/exit reconstruction for
  every symbol. Restart-empty-dashboard bug fixed at the code level —
  needs live Windows validation.
- **Exit-dispatch independent if-blocks** in `strategies/rsi_drop.py`.
  Chained `elif` for `scan_tf / trigger_tf / exit_tf / D / W` converted
  to independent `if`s with `!= scan_tf` guards. Latent bug — positions
  never exited when `trigger_tf == exit_tf` — now closed. Verified by
  `tests/test_exit_dispatch.py`.
- **Debounced saves + bulk_mode** in `core/state_store.py`. Added:
  - `save(force=False)` — no-op while in bulk_mode or within a debounce
    window; marks dirty.
  - `bulk_mode()` context-manager — coalesces N writes into 1 atomic
    flush on exit, re-entrant, flushes even on exception.
  - `save_now()` — explicit force-flush for shutdown/checkpoints.
  - `set_save_debounce(seconds)` — opt-in coalescing of rapid saves.
  - `carry_forward.run()` wrapped: WinError 32 storm on Windows is gone.
  Verified by `tests/test_state_store_bulk.py`.
- **Scanner 4 reads RSI cache (35× speedup)**. New
  `ReplayEngine.fast_replay_from_cache()`. `Strategy4Momentum.run_carry_forward`
  takes an optional `rsi_cache=` and uses the fast path; falls back to
  CSV when cache is empty. `main.py` passes `rsi_cache` at both
  call sites. Verified by `tests/test_fast_replay.py`.

### Earlier (prior session)
- TickSanityValidator no longer drops legitimate gap-up opens.
- Atomic state writes with Windows-safe `os.replace` retry loop.
- Fast-skip restarts via `RunCache` markers.
- Shadow ports (8503 dashboard, 5002 webhook) + custom titles.
- Carry-forward state machine + 8 unit-tested scenarios.
- Watchdog `int('D')` crash fixed.
- Strict forming D/W RSI buy-time gate.

## Roadmap

### P0 — needs user validation on Windows
- Run `LOGIN.bat → START.bat`, verify:
  - Scanner 1 + 2 dashboards populate (no empty restart).
  - Saturday smoke-test prints "Carry-forward complete: …" without
    `WinError 32` errors.
  - Scanner 4 replay completes in ≪ 12 min (target ~20 s).

### P1 — open
- Live RSI parity check during market hours (`tools/diagnose_rsi.py`)
  on a known mid-market misfire — confirm TickSanity fix held.
- Cache tail-staleness: live trading should update in-memory cache on
  candle close, not wait for full rebuild.

### P2 — backlog
- Tick handler back-pressure (worker queue off the websocket thread).
- Webhook idempotency (deterministic dedup-key
  `scanner_id|symbol|signal|candle_close_iso`).
- Timezone audit (naive `datetime.now()` → `datetime.now(IST)`).
- Cache weekly truncation fix (fetch native daily ≥3 yr at build time).
- Refactor `main.py` (1.7 k LOC, 8 daemons, no lifecycle mgr).
- Log every silenced `except Exception: pass` (~80 sites).

## Definition of done
1. All 7 active test suites green:
   `tests/test_{carry_forward_state_machine,exit_dispatch,fast_replay,run_cache,state_store_bulk,state_store_safety,tick_sanity}.py`.
2. `cf_verify.py` output matches dashboard active-buys list (minus any
   that the new forming-D/W rule correctly blocks — e.g., POLICYBZR).
3. RSI parity ≥99% on the sample from `tools/check_daily_drift.py --n 25`.
4. No `WinError 32` lines in `START.bat` logs on a quick restart.

## Operational notes for the next agent
- This repo runs on Linux in the Emergent container; the user runs it
  on **Windows** via `START.bat`. File locks differ; `os.replace`
  contention with the dashboard is the #1 historical bug source.
- The user owns `config.json`; ours is intentionally absent. Don't
  break imports by assuming config is present.
- Streamlit holds the state file open while reading. The Windows-safe
  retry loop + bulk_mode + debounce together absorb that.
- Tests are pytest-style **scripts** runnable with plain `python3` —
  they bootstrap `sys.path` themselves.
