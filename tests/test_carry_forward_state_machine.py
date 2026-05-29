"""
test_carry_forward_state_machine.py — 8 canonical scenarios from the
other AI's session, ported verbatim. These define the spec.

Run: python tests/test_carry_forward_state_machine.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.carry_forward_state_machine import replay_cycles  # noqa: E402

# Shared thresholds (all 8 cases)
RSI_ENTRY = 22
RSI_RESET = 68
DROP_PCT  = 1.0
AVG_PCT   = 2.0


def expect(label, got_state, got_cycles, want_state, want_cycles):
    ok = (got_state == want_state) and (got_cycles == want_cycles)
    icon = "✅" if ok else "❌"
    print(f"  {icon}  {label:<70} got=({got_state}, {got_cycles})  want=({want_state}, {want_cycles})")
    if not ok:
        sys.exit(1)


def main():
    print("\n  CarryForward state machine — 8 canonical scenarios\n")

    # T1 — single watch, never bought (in-progress watch at session end)
    r = replay_cycles(
        rsi_series=[25, 17, 18, 19, 20],
        closes=[100, 100, 100, 100, 100],
        date_strs=["t1", "t2", "t3", "t4", "t5"],
        exit_events=[],
        rsi_entry=RSI_ENTRY, rsi_reset=RSI_RESET,
        drop_pct=DROP_PCT, avg_pct=AVG_PCT,
    )
    expect("T1: single watch, never bought (session-end watch)",
           r.final_state, r.n_cycles, "watched", 0)

    # T2 — watch → buy, no exit (held; buy without exit same day)
    r = replay_cycles(
        rsi_series=[25, 17, 15, 30],
        closes=[100, 100, 98, 99],
        date_strs=["t1", "t2", "t3", "t4"],
        exit_events=[],
        rsi_entry=RSI_ENTRY, rsi_reset=RSI_RESET,
        drop_pct=DROP_PCT, avg_pct=AVG_PCT,
    )
    expect("T2: watch → buy, no exit (held)",
           r.final_state, r.n_cycles, "active", 0)

    # T3 — watch → buy → exit (one full cycle, same day)
    r = replay_cycles(
        rsi_series=[25, 17, 15, 30, 35],
        closes=[100, 100, 98, 99, 100],
        date_strs=["t1", "t2", "t3", "t4", "t5"],
        exit_events=[("t4", 99)],
        rsi_entry=RSI_ENTRY, rsi_reset=RSI_RESET,
        drop_pct=DROP_PCT, avg_pct=AVG_PCT,
    )
    expect("T3: watch → buy → exit (full cycle, restored as EXITED)",
           r.final_state, r.n_cycles, "none", 1)

    # T4 — cycle 1 exits, then a new watch (latest in-progress wins)
    r = replay_cycles(
        rsi_series=[25, 17, 15, 30, 35, 15, 17],
        closes=[100, 100, 98, 99, 100, 105, 105],
        date_strs=["t1", "t2", "t3", "t4", "t5", "t6", "t7"],
        exit_events=[("t4", 99)],
        rsi_entry=RSI_ENTRY, rsi_reset=RSI_RESET,
        drop_pct=DROP_PCT, avg_pct=AVG_PCT,
    )
    expect("T4: cycle 1 exits + new watch (latest wins)",
           r.final_state, r.n_cycles, "watched", 1)

    # T5 — cycle 1 exits, then new watch + buy (latest wins as ACTIVE)
    r = replay_cycles(
        rsi_series=[25, 17, 15, 30, 35, 15, 17, 16],
        closes=[100, 100, 98, 99, 100, 105, 105, 103],
        date_strs=["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"],
        exit_events=[("t4", 99)],
        rsi_entry=RSI_ENTRY, rsi_reset=RSI_RESET,
        drop_pct=DROP_PCT, avg_pct=AVG_PCT,
    )
    expect("T5: cycle 1 exits + new watch+buy (latest wins as ACTIVE)",
           r.final_state, r.n_cycles, "active", 1)

    # T6 — ICICIAMC bug: watch → buy → RSI later rises above reset, NO exit
    r = replay_cycles(
        rsi_series=[25, 17, 15, 70],
        closes=[100, 100, 98, 99],
        date_strs=["t1", "t2", "t3", "t4"],
        exit_events=[],
        rsi_entry=RSI_ENTRY, rsi_reset=RSI_RESET,
        drop_pct=DROP_PCT, avg_pct=AVG_PCT,
    )
    expect("T6: ICICIAMC bug — ACTIVE survives RSI > reset (no exit)",
           r.final_state, r.n_cycles, "active", 0)

    # T7 — watch → RSI > reset before any buy (reset, no position)
    r = replay_cycles(
        rsi_series=[25, 17, 70],
        closes=[100, 100, 105],
        date_strs=["t1", "t2", "t3"],
        exit_events=[],
        rsi_entry=RSI_ENTRY, rsi_reset=RSI_RESET,
        drop_pct=DROP_PCT, avg_pct=AVG_PCT,
    )
    expect("T7: watch → reset before buy → none",
           r.final_state, r.n_cycles, "none", 0)

    # T8 — multiple resets, latest watch survives
    r = replay_cycles(
        rsi_series=[25, 17, 70, 16, 71, 18],
        closes=[100, 100, 105, 103, 108, 107],
        date_strs=["t1", "t2", "t3", "t4", "t5", "t6"],
        exit_events=[],
        rsi_entry=RSI_ENTRY, rsi_reset=RSI_RESET,
        drop_pct=DROP_PCT, avg_pct=AVG_PCT,
    )
    expect("T8: multiple resets, latest watch survives",
           r.final_state, r.n_cycles, "watched", 0)

    print("\n  ✅ All 8 carry-forward state-machine scenarios passed.\n")


if __name__ == "__main__":
    main()
