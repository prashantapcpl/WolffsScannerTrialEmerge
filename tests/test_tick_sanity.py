"""
test_tick_sanity.py — unit checks for the patched TickSanityValidator.

Wall-clock-agnostic via per-test freezing of datetime + market_calendar.

Run: python tests/test_tick_sanity.py
"""
import os
import sys
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

IST = pytz.timezone("Asia/Kolkata")

import core.market_calendar as _mc  # noqa: E402
_mc.is_market_hours = lambda *_a, **_kw: True

import core.runtime_monitors as _rm  # noqa: E402


def freeze(hour: int, minute: int):
    """Pin runtime_monitors.datetime.now() to today @ HH:MM IST."""
    pinned = IST.localize(datetime(2026, 5, 28, hour, minute, 0))

    class _DT:
        @staticmethod
        def now(tz=None):
            return pinned.astimezone(tz) if tz else pinned.replace(tzinfo=None)
    _rm.datetime = _DT
    return pinned


from core.runtime_monitors import TickSanityValidator  # noqa: E402


def assert_(cond, msg):
    print(f"  {'✅' if cond else '❌'}  {msg}")
    if not cond:
        sys.exit(1)


def main():
    print("\n  TickSanityValidator — fix verification\n")

    # 1) First tick of session — no baseline → always accepted.
    now = freeze(9, 16)
    v = TickSanityValidator()
    ok, _ = v.validate("NSE:FOO-EQ", 100.0, now)
    assert_(ok, "first tick of session accepted (no baseline)")

    # 2) Tiny normal move accepted.
    now = freeze(13, 0)
    v2 = TickSanityValidator()
    v2.last_accepted["NSE:FOO-EQ"]      = 100.0
    v2.last_accepted_time["NSE:FOO-EQ"] = freeze(12, 59)
    freeze(13, 0)  # re-pin for validate()
    ok, _ = v2.validate("NSE:FOO-EQ", 100.5, now)
    assert_(ok, "0.5% move accepted")

    # 3) Big jump within normal mid-session window REJECTED (not a circuit band).
    now = freeze(11, 31)
    v3 = TickSanityValidator()
    v3.last_accepted["NSE:FOO-EQ"]      = 100.0
    v3.last_accepted_time["NSE:FOO-EQ"] = freeze(11, 30)
    freeze(11, 31)
    ok, reason = v3.validate("NSE:FOO-EQ", 107.0, now)  # +7%, not 5/10/20
    assert_(not ok and "jump" in reason, "+7% mid-session jump rejected")

    # 4) Same +10% in 09:15–09:30 opening window IS accepted.
    now = freeze(9, 20)
    v4 = TickSanityValidator()
    v4.last_accepted["NSE:GAP-EQ"]      = 100.0
    v4.last_accepted_time["NSE:GAP-EQ"] = freeze(9, 15)
    freeze(9, 20)
    ok, _ = v4.validate("NSE:GAP-EQ", 108.0, now)
    assert_(ok, "+8% gap during 09:15–09:30 opening accepted")

    # 5) Exact 5% / 10% / 20% circuit moves accepted even mid-session.
    now = freeze(13, 5)
    v5 = TickSanityValidator()
    v5.last_accepted["NSE:CIR-EQ"]      = 100.0
    v5.last_accepted_time["NSE:CIR-EQ"] = freeze(13, 0)
    freeze(13, 5)
    ok, _ = v5.validate("NSE:CIR-EQ", 105.0, now)
    assert_(ok, "+5% (circuit) accepted")

    v5.last_accepted["NSE:CIR-EQ"]      = 100.0
    v5.last_accepted_time["NSE:CIR-EQ"] = freeze(13, 0)
    freeze(13, 5)
    ok, _ = v5.validate("NSE:CIR-EQ", 120.0, now)
    assert_(ok, "+20% (circuit) accepted")

    # 6) Stale baseline → next tick accepted regardless of jump size.
    now = freeze(9, 16)
    v6 = TickSanityValidator()
    v6.last_accepted["NSE:OVR-EQ"]      = 100.0
    # Yesterday — well beyond 30 min stale threshold.
    v6.last_accepted_time["NSE:OVR-EQ"] = now - timedelta(hours=18)
    ok, _ = v6.validate("NSE:OVR-EQ", 115.0, now)
    assert_(ok, "first tick after overnight gap accepted (stale baseline)")

    # 7) Env flag disables jump check entirely.
    now = freeze(11, 5)
    os.environ["TICK_SANITY_JUMP_CHECK_ENABLED"] = "false"
    v7 = TickSanityValidator()
    v7.last_accepted["NSE:DIS-EQ"]      = 100.0
    v7.last_accepted_time["NSE:DIS-EQ"] = freeze(11, 0)
    freeze(11, 5)
    ok, _ = v7.validate("NSE:DIS-EQ", 200.0, now)
    assert_(ok, "TICK_SANITY_JUMP_CHECK_ENABLED=false bypasses jump check")
    del os.environ["TICK_SANITY_JUMP_CHECK_ENABLED"]

    # 8) get_stats() shape.
    stats = v7.get_stats()
    assert_("total_rejections" in stats and "policy" in stats,
            "get_stats() returns expected dict")
    assert_(stats["policy"]["max_pct_jump"] == 5.0,
            "default policy reports max_pct_jump=5.0")
    assert_(stats["policy"]["opening_pct_jump"] == 25.0,
            "default policy reports opening_pct_jump=25.0")

    # 9) Zero / negative price rejected.
    now = freeze(13, 0)
    v9 = TickSanityValidator()
    ok, reason = v9.validate("NSE:ZERO-EQ", 0.0, now)
    assert_(not ok and "<= 0" in reason, "zero price rejected")

    print("\n  ✅ All TickSanityValidator checks passed.\n")


if __name__ == "__main__":
    main()
