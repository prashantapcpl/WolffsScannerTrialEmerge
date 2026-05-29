"""
carry_forward_state_machine.py — the multi-cycle RSI-Drop replay state machine.

This module is the *core cycle logic* lifted out of carry_forward.py for
clean isolation + unit testing. The 8 fixtures in
tests/test_carry_forward_state_machine.py are the canonical spec.

Background
----------
The original `carry_forward.py` had a `break` after the first watch→buy,
so any stock that completed one cycle and entered a fresh watch (or
fresh buy) the same session was missed → empty Scanner 1/2 on restart.
Also, the old code applied `rsi_reset` to ACTIVE positions, wrongly
closing held positions whose RSI later rose above the reset threshold.

State machine
-------------
States:  none → watched → active → none ...

Transitions, evaluated candle-by-candle on the scan_tf:

  none    → watched : rsi < rsi_entry              (lock reference price)
  watched → active  : drop_pct% drop from reference (BUY fires)
  watched → none    : rsi > rsi_reset BEFORE a buy fires (reset)
  active  → none    : exit-tf RSI crossover above exit threshold AFTER
                      buy time (the caller pre-computes these events)

Critical invariants:
  • rsi_reset does NOT apply while ACTIVE — only WATCHED can reset.
  • Multi-cycle: keep walking the series after an exit. Latest in-progress
    state wins. If the walk ends with no in-progress state but at least
    one cycle completed, restore as EXITED (the completed cycle).

Inputs to `replay_cycles`:
  rsi_series, closes:  parallel lists of scan-tf RSI values + closes
  date_strs:           ordered labels (strings) for each candle — only
                       used for `exit_events` time comparison
  exit_events:         list of (exit_time, exit_close) tuples that the
                       caller has already computed by scanning the
                       exit-tf series for crossovers above the exit
                       threshold.
  rsi_entry, rsi_reset, drop_pct, avg_pct:  thresholds.

Output:
  ReplayResult dataclass:
    final_state : "none" | "watched" | "active"
    n_cycles    : number of completed exit cycles
    ref_price   : reference price for current in-progress watch/active
    ref_time    : date_str when current ref was locked
    buy_price   : buy price for current ACTIVE (None if not ACTIVE)
    buy_time    : date_str of current ACTIVE buy
    last_exit_time, last_exit_price: only set when final_state="none" AND
                  n_cycles >= 1 (i.e. restored as EXITED on the last cycle).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class ReplayResult:
    final_state:     str   = "none"
    n_cycles:        int   = 0
    ref_price:       float | None = None
    ref_time:        str | None   = None
    buy_price:       float | None = None
    buy_time:        str | None   = None
    last_exit_time:  str | None   = None
    last_exit_price: float | None = None


def replay_cycles(
    rsi_series:  Sequence[float | None],
    closes:      Sequence[float],
    date_strs:   Sequence[str],
    exit_events: Sequence[tuple],
    rsi_entry:   float,
    rsi_reset:   float,
    drop_pct:    float,
    avg_pct:     float = 0.0,    # accepted for API parity, not used here
) -> ReplayResult:
    """Pure function. Walks the RSI series candle-by-candle, applying the
    state machine described in the module docstring. Returns ReplayResult."""

    if not (len(rsi_series) == len(closes) == len(date_strs)):
        raise ValueError("rsi_series, closes, date_strs must be same length")

    # Sort exit_events by time so we can advance through them.
    pending_exits = sorted(exit_events, key=lambda e: e[0])

    state          = "none"   # "none" | "watched" | "active"
    ref_price      = None
    ref_time       = None
    buy_price      = None
    buy_time       = None
    last_exit_t    = None
    last_exit_p    = None
    n_cycles       = 0

    def _consume_exits_before_or_at(t):
        """Pop and return the first pending exit whose time <= t.
        Returns (exit_time, exit_close) or None."""
        if pending_exits and pending_exits[0][0] <= t:
            return pending_exits.pop(0)
        return None

    for i, (rsi, close, dt) in enumerate(zip(rsi_series, closes, date_strs)):

        # ── While ACTIVE, check first if an exit event has fired by `dt`.
        if state == "active":
            ev = _consume_exits_before_or_at(dt)
            while ev is not None:
                # Only count an exit that's strictly AFTER our buy_time.
                if buy_time is None or ev[0] > buy_time:
                    last_exit_t = ev[0]
                    last_exit_p = ev[1]
                    n_cycles  += 1
                    state      = "none"
                    ref_price  = None
                    ref_time   = None
                    buy_price  = None
                    buy_time   = None
                    break
                ev = _consume_exits_before_or_at(dt)
            # If we exited, fall through to also evaluate this candle
            # for a fresh watch (rsi could already be < entry on the
            # exit candle).

        if rsi is None:
            continue

        if state == "none":
            if rsi < rsi_entry:
                state     = "watched"
                ref_price = close
                ref_time  = dt
            continue

        if state == "watched":
            # Reset takes priority over buy in the same candle (matches
            # the existing logic: if a stock's RSI snapped back above
            # reset, the watch is invalidated).
            if rsi > rsi_reset:
                state     = "none"
                ref_price = None
                ref_time  = None
                continue
            # Reference price is LOCKED at the watch moment per spec —
            # it is NOT updated to track lowest close during the watch.
            # Buy check.
            if ref_price and ((ref_price - close) / ref_price) * 100 >= drop_pct:
                state     = "active"
                buy_price = close
                buy_time  = dt
            continue

        if state == "active":
            # IMPORTANT: rsi_reset does NOT close an ACTIVE position.
            # Only an exit event (handled at top of loop) closes it.
            continue

    # ── End of walk: if there are still pending exits ≤ "infinity" they
    # would apply, but only if we're ACTIVE. Process any remaining exits
    # that come strictly after our buy_time.
    if state == "active" and pending_exits:
        for ev_t, ev_p in pending_exits:
            if buy_time is None or ev_t > buy_time:
                last_exit_t = ev_t
                last_exit_p = ev_p
                n_cycles   += 1
                state       = "none"
                ref_price   = None
                ref_time    = None
                buy_price   = None
                buy_time    = None
                break

    return ReplayResult(
        final_state     = state,
        n_cycles        = n_cycles,
        ref_price       = ref_price,
        ref_time        = ref_time,
        buy_price       = buy_price,
        buy_time        = buy_time,
        last_exit_time  = last_exit_t if state == "none" and n_cycles >= 1 else None,
        last_exit_price = last_exit_p if state == "none" and n_cycles >= 1 else None,
    )
