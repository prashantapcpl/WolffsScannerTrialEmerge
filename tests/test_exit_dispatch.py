"""
test_exit_dispatch.py — regression test for the "exit dispatch independent
if-blocks" fix in strategies/rsi_drop.py.

Bug scenario
------------
When the user sets `trigger_timeframe == exit_timeframe` (e.g. both = "5"),
the old chained `if scan / elif trigger / elif exit` dispatch matched the
trigger branch first and the exit branch NEVER ran. Positions would never
exit, no matter how high the exit-tf RSI rose.

Fix
---
Convert chained `elif` to independent `if`s, each guarded by
`timeframe == X and timeframe != scan_tf` so:
- when scan_tf handles everything (scan == trigger == exit), only scan runs;
- when scan_tf != trigger_tf == exit_tf, BOTH trigger and exit branches run.

This test only verifies the EXIT BRANCH FIRES when trigger_tf == exit_tf.
The buy-side / sell-side / D / W behaviour is untouched and already covered
by existing acceptance.
"""
import os
import sys
import tempfile
from datetime import datetime
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.state_store import StateStore, StockState  # noqa: E402
from strategies.rsi_drop import RSIDropStrategy  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


# ─── lightweight fakes ────────────────────────────────────────────────────
class FakeRSIEngine:
    """Returns the value the test sets per (symbol, tf)."""
    def __init__(self):
        self.values = {}
        self._calculators = {}   # rsi_drop._live_daily_rsi reads this

    def set(self, symbol, tf, value):
        self.values[(symbol, tf)] = value

    def get_rsi(self, symbol, tf):
        return self.values.get((symbol, tf))

    # rsi_drop._live_daily_rsi uses these — return safe defaults
    def get_yesterday_d_state(self, symbol):
        return None


class FakeCandle:
    def __init__(self, close, close_time):
        self.close = close
        self.close_time = close_time


class FakeWebhook:
    def __init__(self):
        self.calls = []

    def send_buy(self, **kw):     self.calls.append(("buy", kw))
    def send_avg(self, **kw):     self.calls.append(("avg", kw))
    def send_exit(self, **kw):    self.calls.append(("exit", kw))
    def send_sell(self, **kw):    self.calls.append(("sell", kw))
    def send_sell_avg(self, **kw):    self.calls.append(("sell_avg", kw))
    def send_sell_exit(self, **kw):   self.calls.append(("sell_exit", kw))
    def send_stoploss_exit(self, **kw):  self.calls.append(("sl_exit", kw))
    def send_sell_stoploss_exit(self, **kw):  self.calls.append(("sl_sell_exit", kw))


def _seed_active(state_store, symbol, plain_name, buy_price, when):
    """Get a record straight into ACTIVE_BUY with a buy_price set."""
    state_store.get_or_create(symbol, plain_name)
    state_store.move_to_watched(symbol=symbol, reference_price=buy_price*1.05,
                                 rsi_value=20.0, now=when)
    state_store.move_to_active_buy(symbol=symbol, buy_price=buy_price,
                                    drop_pct=2.5, now=when)


def run():
    print("\n  Exit dispatch regression (trigger_tf == exit_tf)\n")

    with tempfile.TemporaryDirectory() as td:
        state_path = os.path.join(td, "state.json")
        store = StateStore(state_path)

        cfg = {
            "scan_timeframe":          "5",
            "trigger_timeframe":      "15",
            "exit_timeframe":         "15",   # ← coincides with trigger
            "rsi_entry_threshold":     20,
            "rsi_reset_threshold":     70,
            "rsi_exit_threshold":      65,
            "drop_percent":            2.0,
            "avg_drop_percent":        3.0,
            "daily_rsi_filter_enabled":  False,
            "weekly_rsi_filter_enabled": False,
        }
        strat = RSIDropStrategy(cfg)

        rsi    = FakeRSIEngine()
        hook   = FakeWebhook()
        symbol = "NSE:FOO-EQ"
        now    = IST.localize(datetime(2026, 1, 6, 10, 30, 0))

        # Seed an ACTIVE_BUY position
        _seed_active(store, symbol, "FOO", buy_price=100.0, when=now)
        assert store.get(symbol).state == StockState.ACTIVE
        print("  ✅  ACTIVE_BUY seeded")

        # Now fire a candle on the SHARED trigger/exit TF. exit_tf RSI > 65 →
        # exit MUST fire. With the old `elif` chain this would silently skip.
        rsi.set(symbol, "15", 72.0)  # exit_tf RSI above 65
        candle = FakeCandle(close=104.0,
                            close_time=now.replace(hour=10, minute=45))

        strat.on_candle_close(
            symbol=symbol, plain_name="FOO", candle=candle,
            timeframe="15", rsi_engine=rsi, state_store=store,
            webhook_sender=hook
        )

        rec = store.get(symbol)
        assert rec.state == StockState.EXITED, (
            f"FAIL: state is {rec.state}, expected EXITED. "
            "Exit dispatch did not run — `elif` regression."
        )
        print("  ✅  EXIT fired on shared trigger/exit TF")

        # Webhook must also have fired the exit event
        kinds = [k for k, _ in hook.calls]
        assert "exit" in kinds, f"FAIL: no exit webhook fired ({kinds})"
        print("  ✅  exit webhook fired")

        # Now verify the OTHER guard: when scan_tf == trigger_tf == exit_tf,
        # the scan_tf branch handles everything and the new `!= scan_tf`
        # guards prevent the standalone exit block from running twice.
        store2 = StateStore(os.path.join(td, "state2.json"))
        cfg2 = dict(cfg, scan_timeframe="15")  # scan == trigger == exit
        strat2 = RSIDropStrategy(cfg2)
        rsi2 = FakeRSIEngine()
        hook2 = FakeWebhook()

        _seed_active(store2, symbol, "FOO", buy_price=100.0, when=now)
        rsi2.set(symbol, "15", 72.0)            # scan_tf RSI > exit
        rsi2.set(symbol, "D", None)
        candle2 = FakeCandle(close=104.0,
                              close_time=now.replace(hour=10, minute=45))
        strat2.on_candle_close(
            symbol=symbol, plain_name="FOO", candle=candle2,
            timeframe="15", rsi_engine=rsi2, state_store=store2,
            webhook_sender=hook2
        )
        rec2 = store2.get(symbol)
        assert rec2.state == StockState.EXITED, (
            f"FAIL: scan==trigger==exit state {rec2.state}, expected EXITED"
        )
        exit_count = sum(1 for k, _ in hook2.calls if k == "exit")
        assert exit_count == 1, (
            f"FAIL: exit fired {exit_count}× (expected exactly 1) — "
            "the guard against double-dispatch failed."
        )
        print("  ✅  scan==trigger==exit: EXIT fired exactly once (no double-dispatch)")

    print("\n  ✅ All exit-dispatch regression checks passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(run())
