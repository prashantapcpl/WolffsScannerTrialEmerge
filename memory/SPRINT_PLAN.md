# Wolffs Scanner — Sprint Plan (Friday → Monday Go-Live)

**Author:** assistant (Emergent E1)  
**Status:** Phase 0 in progress (May 28, 2026)

---

## Goal
Live by Monday morning (NSE 09:15 IST) with RSI signals that match what
Fyers/TradingView charts show, on the same Windows local server using
the same `LOGIN.bat → START.bat` flow.

## Risk-tiered phases (so we can land Friday/Saturday safely)

### Phase 0 — Critical bug fixes & diagnostics  ✅ DONE (May 28)
- [x] Fix `TickSanityValidator` silently dropping gap-day / circuit-hit
      ticks (the #1 explanation for "most stocks match, a few drift hard"
      that the other AI introduced today)
      → `core/runtime_monitors.py`
- [x] Per-stock RSI parity diagnostic → `tools/diagnose_rsi.py`
- [x] Daily-close drift sanity sweep → `tools/check_daily_drift.py`
- [x] Unit test for the validator fix → `tests/test_tick_sanity.py`
- [x] Repo cleanup: 17 root-level debug scripts → `debug/`, junk deleted
- [x] `.gitignore` deduped + runtime files added, requirements pinned

### Phase 1 — Friday (live session, you driving)
- [ ] You: run `LOGIN.bat` then `START.bat` as usual.
- [ ] You: while market is open, run **one** diagnostic on a recent
      misfire to confirm Phase 0 actually solved it:
      `python tools/diagnose_rsi.py --symbol NSE:<X>-EQ --date <YYYY-MM-DD> --tf 5`
- [ ] You: at any quiet moment, run `python tools/check_daily_drift.py
      --n 25` to measure drift across a sample.
- [ ] Share the two CSV outputs with me.

### Phase 2 — Saturday (safety + observability)
- [ ] Atomic state-file writes (tmp + `os.replace`) so a crash never
      corrupts `data/scanner_*_state.json`.
- [ ] Carry-forward `--dry-run` flag so a startup pass can print "would
      reset N records" before actually resetting.
- [ ] Idempotent webhook payload: add deterministic dedup-key
      (`scanner_id|symbol|signal|candle_close_iso`) so the downstream app
      can drop replays after restarts.
- [ ] Timezone audit — replace naïve `datetime.now()` with
      `datetime.now(IST)` everywhere in `core/` and `strategies/`.

### Phase 3 — Sunday (throughput hardening)
- [ ] Bounded tick queue + worker pool — strategy callbacks move off the
      websocket thread so one slow strategy can't stall the feed.
- [ ] Audit and replace silent `except Exception: pass` blocks with
      logged warnings (8 bare excepts + 81 broad excepts in the codebase).
- [ ] Final smoke test: replay yesterday's CSVs through strategies
      offline; verify signal log matches the last live session.

### Phase 4 — Monday morning go/no-go
- [ ] 06:00 IST: review Sunday smoke-test output.
- [ ] **Hard gate:** if RSI parity isn't ≥99% on the sample from Friday's
      diagnostic, we DO NOT switch the production scanner. We keep the
      old one running.
- [ ] 09:00 IST: `LOGIN.bat → START.bat`.
- [ ] 09:15 IST: re-run diagnostic on the same Friday misfire symbol.

---

## Definition of "live-ready"
1. `python tests/test_tick_sanity.py` passes.
2. `python tools/check_daily_drift.py --n 25` reports <0.1% drift on
   ≥95% of sampled symbols on the previous trading day.
3. `python tools/diagnose_rsi.py` shows **zero** RSI divergence beyond
   ±0.10 on the misfire that triggered this engagement.
4. State writes are atomic.
5. Webhook payloads include a dedup-key.
6. No new bare `except:` introduced; existing ones logged.

If any of 1–6 fail at 06:00 Monday → we ship the **fixed validator only**
(Phase 0 alone), keep Phases 2–3 behind feature flags, and re-test next
week.
