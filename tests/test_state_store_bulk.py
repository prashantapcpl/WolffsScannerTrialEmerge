"""
test_state_store_bulk.py — bulk_mode + debounce regression for state_store.

Why
---
Carry-forward replays hundreds of state transitions per startup. Each
move_to_* / add_avg / reset_to_general previously triggered an atomic
os.replace, fighting the dashboard's read lock on Windows (WinError 32)
and producing a save-storm.

We added:
  • store.bulk_mode() — coalesces all in-context saves into one flush.
  • store.set_save_debounce(s) — coalesces rapid live saves within a window.

This test verifies disk writes are actually suppressed during bulk mode
and produced exactly once on exit, without losing any state changes.
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.state_store import StateStore  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


def _read_signal_count(path):
    with open(path) as f:
        return len(json.load(f).get("signal_log", []))


def run():
    print("\n  StateStore bulk_mode + debounce regression\n")

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "state.json")
        store = StateStore(path)
        store.get_or_create("NSE:AAA-EQ", "AAA")
        store.get_or_create("NSE:BBB-EQ", "BBB")
        now = IST.localize(datetime(2026, 1, 6, 10, 30, 0))

        # Baseline: file exists and is empty-ish.
        store.save_now()
        mtime_baseline = os.path.getmtime(path)
        assert mtime_baseline > 0
        print("  ✅  initial save persisted")

        # ── bulk_mode coalesces N saves into ONE ─────────────────────────
        time.sleep(0.05)
        write_count_marker = [0]
        orig = store._save_to_disk

        def counted_save():
            write_count_marker[0] += 1
            orig()

        store._save_to_disk = counted_save

        with store.bulk_mode():
            for i in range(10):
                store.move_to_watched(
                    symbol="NSE:AAA-EQ", reference_price=100.0 + i,
                    rsi_value=18.0, now=now,
                )
            # Inside bulk → ZERO disk writes
            assert write_count_marker[0] == 0, (
                f"FAIL: {write_count_marker[0]} writes during bulk_mode"
            )
            print("  ✅  zero writes inside bulk_mode (10 transitions queued)")

        # On exit, ONE flush
        assert write_count_marker[0] == 1, (
            f"FAIL: bulk_mode exit produced {write_count_marker[0]} writes (expected 1)"
        )
        print("  ✅  exactly 1 atomic flush on bulk_mode exit")

        # State actually persisted — latest reference_price wins
        loaded = StateStore(path)
        rec = loaded.get("NSE:AAA-EQ")
        assert rec is not None, "FAIL: record lost"
        assert rec.reference_price == 109.0, (
            f"FAIL: reference_price={rec.reference_price}, expected 109.0"
        )
        print("  ✅  latest mutation persisted (no data loss in bulk_mode)")

        # ── nested bulk_mode flushes only on outermost exit ──────────────
        write_count_marker[0] = 0
        with store.bulk_mode():
            with store.bulk_mode():
                store.move_to_watched(
                    symbol="NSE:BBB-EQ", reference_price=200.0,
                    rsi_value=15.0, now=now,
                )
                assert write_count_marker[0] == 0
            assert write_count_marker[0] == 0, "FAIL: inner exit flushed"
        assert write_count_marker[0] == 1, "FAIL: outer exit didn't flush"
        print("  ✅  nested bulk_mode flushes only on outermost exit")

        # ── debounce coalesces rapid saves ───────────────────────────────
        store._save_to_disk = orig   # restore real
        store.set_save_debounce(0.30)
        store.save_now()              # baseline
        write_count_marker[0] = 0
        store._save_to_disk = counted_save

        # Two saves inside the 300ms window → only the first writes
        store.save()
        store.save()
        store.save()
        assert write_count_marker[0] == 0, (
            f"FAIL: {write_count_marker[0]} writes during debounce window"
        )
        print("  ✅  saves within debounce window suppressed")

        # Wait past window, force a flush
        time.sleep(0.35)
        store.save_now()
        assert write_count_marker[0] == 1, "FAIL: save_now didn't force flush"
        print("  ✅  save_now() bypasses debounce")

        store.set_save_debounce(0.0)  # restore default

        # ── flush on exception inside bulk_mode (don't drop state) ───────
        store._save_to_disk = orig
        write_count_marker[0] = 0
        store._save_to_disk = counted_save
        try:
            with store.bulk_mode():
                store.move_to_watched(
                    symbol="NSE:AAA-EQ", reference_price=999.0,
                    rsi_value=10.0, now=now,
                )
                raise RuntimeError("simulated mid-replay crash")
        except RuntimeError:
            pass
        assert write_count_marker[0] == 1, (
            f"FAIL: exception path produced {write_count_marker[0]} writes (expected 1)"
        )
        print("  ✅  bulk_mode flushes on exception (no silent data loss)")

    print("\n  ✅ All bulk_mode / debounce regression checks passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(run())
