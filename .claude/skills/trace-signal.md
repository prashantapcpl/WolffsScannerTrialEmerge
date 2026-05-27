---
name: trace-signal
description: Forensic trace of why a specific stock is (or isn't) in a specific state at a specific date/time, comparing current scanner settings against clean RSI from CSV. Use this when the user points at a dashboard entry and asks "why is this stock in BUY?" or "why didn't this fire?" or "verify this signal."
---

# /trace-signal — single-stock signal forensic

When the user invokes this skill, they're usually frustrated about a specific
entry on the dashboard. Goal: in under 10 seconds, give them the actual
numbers (real RSI, real prices, what current settings say should have
happened) without writing yet another one-off Python script.

## Invocation

`/trace-signal SYMBOL DATE [TIME] [SCANNER_ID]`

Examples:
- `/trace-signal TATACOMM 2026-05-22 15:30 scanner_2`
- `/trace-signal NSE:DIVISLAB-EQ 2026-05-25 13:15 scanner_1`
- `/trace-signal HINDALCO 2026-05-25` (whole day, default scanner_1)

## What to do

Run the Python tool that does the heavy lifting:

```
python tools/trace_signal.py <SYMBOL> <DATE> [TIME] [SCANNER_ID]
```

The tool prints 6 sections:
1. Settings being used (so user can confirm the right scanner config)
2. Raw 5m CSV rows around the time (raw truth)
3. Computed RSI series at scan_tf using ReplayEngine (clean RSI)
4. Condition matrix: each filter PASS/FAIL with actual numbers
5. State file lookup (what's currently stored for this stock)
6. Discrepancy analysis (if stored rsi_at_watch or reference_price disagree
   with clean replay, flag it — that's a stale-data position)

## After running

- If the user asked "why is this in BUY?" and the condition matrix shows
  all conditions PASS → tell them: legitimate entry, here's why each
  condition was satisfied.
- If conditions FAIL but stock IS in BUY → stale corrupted-data position,
  recommend restart (which now auto-resets and re-derives).
- If conditions PASS but stock NOT in state → either (a) was exited and
  cooldown blocks re-entry, or (b) state needs restart to reconstruct.

Do NOT re-explain how RSI works. Do NOT add code commentary unless
something genuinely surprising shows up. The script output is the answer.

## When NOT to use

- General "is my scanner working?" — that's the data integrity pass at
  startup, not this.
- Multi-stock comparisons or universe-wide scans — write a one-off script.
- Backtest-style analysis — use the backtester.
