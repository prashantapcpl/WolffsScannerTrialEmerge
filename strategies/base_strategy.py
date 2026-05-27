"""
base_strategy.py
Template that all strategies must inherit from.
When you add a new strategy in the future, copy this file,
rename it, and fill in the methods below.
"""

from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    """
    Base class for all scanner strategies.
    Subclass this and implement the required methods.
    """

    def __init__(self, name: str, config: dict):
        self.name   = name
        self.config = config
        self.active = True

    @abstractmethod
    def on_candle_close(self, symbol: str, plain_name: str,
                         candle, timeframe: str,
                         rsi_engine, state_store, webhook_sender):
        """
        Called every time a candle closes for any tracked symbol.
        Implement your strategy logic here.

        Args:
            symbol:         Fyers symbol (e.g. NSE:RELIANCE-EQ)
            plain_name:     Plain name (e.g. RELIANCE)
            candle:         The closed Candle object (has .open .high .low .close)
            timeframe:      Timeframe string (e.g. "5", "1", "10")
            rsi_engine:     RSIEngine instance — call rsi_engine.get_rsi(symbol, tf)
            state_store:    StateStore instance — call state_store.get(symbol)
            webhook_sender: WebhookSender instance
        """
        pass

    @abstractmethod
    def get_description(self) -> str:
        """Return a human-readable description of this strategy."""
        pass

    def on_tick(self, symbol: str, price: float, rsi_engine, state_store,
                webhook_sender=None):
        """
        Optional: Called on every price tick (very frequent).
        Only override if your strategy needs tick-level checks (e.g. live
        daily-RSI stoploss).  webhook_sender is optional for back-compat
        with older overrides.
        """
        pass

    def on_market_open(self, state_store, rsi_engine):
        """
        Optional: Called once when market opens at 9:15 AM IST.
        Use for any daily initialization.
        """
        pass

    def on_market_close(self, state_store):
        """
        Optional: Called once when market closes at 3:30 PM IST.
        Use for EOD cleanup if needed.
        """
        pass

    def update_config(self, new_config: dict):
        """Update strategy config (called from dashboard settings).
        Replaces the entire config dict so removing a setting on disk
        actually removes it in memory too (was previously a merge which
        left stale keys behind)."""
        self.config = dict(new_config)
        print(f"🔧 Strategy '{self.name}' config updated: {new_config}")
