"""
rsi_engine.py
Calculates RSI using Wilder's smoothing method.
Maintains RSI state per symbol per timeframe so it updates
incrementally on each new candle — no need to recalculate everything.
"""

import collections


class RSICalculator:
    """
    Per-symbol, per-timeframe RSI calculator.
    Uses Wilder's smoothing (same as TradingView, Zerodha etc).
    """

    def __init__(self, period: int = 14):
        self.period = period
        self.closes = collections.deque(maxlen=period + 1)
        self.avg_gain = None
        self.avg_loss = None
        self.rsi = None
        self.initialized = False
        self.candle_count = 0

    def update(self, close_price: float) -> float | None:
        """
        Feed one new close price. Returns current RSI or None if not enough data.
        Call this every time a candle closes.
        """
        self.closes.append(close_price)
        self.candle_count += 1

        if self.candle_count < self.period + 1:
            # Not enough data yet
            return None

        if not self.initialized:
            # First RSI calculation — use simple average
            closes_list = list(self.closes)
            gains = []
            losses = []
            for i in range(1, len(closes_list)):
                diff = closes_list[i] - closes_list[i - 1]
                if diff > 0:
                    gains.append(diff)
                    losses.append(0)
                else:
                    gains.append(0)
                    losses.append(abs(diff))

            self.avg_gain = sum(gains) / self.period
            self.avg_loss = sum(losses) / self.period
            self.initialized = True

        else:
            # Wilder's smoothing for subsequent candles
            closes_list = list(self.closes)
            diff = closes_list[-1] - closes_list[-2]
            gain = diff if diff > 0 else 0
            loss = abs(diff) if diff < 0 else 0

            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period

        if self.avg_loss == 0:
            self.rsi = 100.0
        else:
            rs = self.avg_gain / self.avg_loss
            self.rsi = round(100 - (100 / (1 + rs)), 2)

        return self.rsi

    def get_rsi(self) -> float | None:
        """Return the latest RSI value."""
        return self.rsi

    def seed_from_history(self, close_prices: list) -> float | None:
        """
        Seed the RSI with a list of historical close prices.
        Call this at startup with historical data from Fyers REST API.
        Returns the last RSI value after processing all prices.
        """
        self.closes.clear()
        self.avg_gain = None
        self.avg_loss = None
        self.rsi = None
        self.initialized = False
        self.candle_count = 0

        last_rsi = None
        for price in close_prices:
            result = self.update(price)
            if result is not None:
                last_rsi = result

        return last_rsi

    def get_state(self) -> dict | None:
        """Export the 5 numbers needed to resume without replaying history."""
        if not self.initialized or self.rsi is None:
            return None
        closes_list = list(self.closes)
        if len(closes_list) < 2:
            return None
        return {
            "avg_gain":   self.avg_gain,
            "avg_loss":   self.avg_loss,
            "rsi":        self.rsi,
            "last_close": closes_list[-1],
            "prev_close": closes_list[-2],
        }

    def restore_state(self, avg_gain: float, avg_loss: float, rsi: float,
                      last_close: float, prev_close: float):
        """Restore calculator from saved state — no history replay needed."""
        self.closes.clear()
        self.closes.append(prev_close)
        self.closes.append(last_close)
        self.avg_gain    = avg_gain
        self.avg_loss    = avg_loss
        self.rsi         = rsi
        self.initialized = True
        self.candle_count = self.period + 2   # past the initialization threshold

    def is_ready(self) -> bool:
        """Returns True if RSI has been calculated (enough data)."""
        return self.rsi is not None


class RSIEngine:
    """
    Manages RSI calculators for all symbols and all timeframes.
    Access like: engine.get_rsi("NSE:RELIANCE-EQ", "5")
    """

    def __init__(self, period: int = 14):
        self.period = period
        # Structure: { symbol: { timeframe: RSICalculator } }
        self._calculators: dict[str, dict[str, RSICalculator]] = {}

    def _get_calculator(self, symbol: str, timeframe: str) -> RSICalculator:
        """Get or create a calculator for symbol+timeframe."""
        if symbol not in self._calculators:
            self._calculators[symbol] = {}
        if timeframe not in self._calculators[symbol]:
            self._calculators[symbol][timeframe] = RSICalculator(self.period)
        return self._calculators[symbol][timeframe]

    def update(self, symbol: str, timeframe: str, close_price: float) -> float | None:
        """Update RSI for a symbol on a specific timeframe with a new close."""
        calc = self._get_calculator(symbol, timeframe)
        return calc.update(close_price)

    def get_rsi(self, symbol: str, timeframe: str) -> float | None:
        """Get current RSI for a symbol on a specific timeframe."""
        if symbol in self._calculators and timeframe in self._calculators[symbol]:
            return self._calculators[symbol][timeframe].get_rsi()
        return None

    def seed(self, symbol: str, timeframe: str, close_prices: list) -> float | None:
        """
        Seed historical data for a symbol+timeframe.
        Call this at startup before market opens.
        """
        calc = self._get_calculator(symbol, timeframe)
        rsi = calc.seed_from_history(close_prices)
        return rsi

    def restore(self, symbol: str, timeframe: str, state: dict):
        """Restore a calculator directly from saved state (no replay)."""
        calc = self._get_calculator(symbol, timeframe)
        calc.restore_state(
            avg_gain   = state["avg_gain"],
            avg_loss   = state["avg_loss"],
            rsi        = state["rsi"],
            last_close = state["last_close"],
            prev_close = state["prev_close"],
        )

    def is_ready(self, symbol: str, timeframe: str) -> bool:
        """Check if RSI is ready for a symbol+timeframe."""
        if symbol in self._calculators and timeframe in self._calculators[symbol]:
            return self._calculators[symbol][timeframe].is_ready()
        return False

    def get_all_symbols(self) -> list:
        """Return all symbols that have calculators."""
        return list(self._calculators.keys())

    def reset_symbol(self, symbol: str):
        """Reset all calculators for a symbol (e.g., new trading day)."""
        if symbol in self._calculators:
            del self._calculators[symbol]


# ─── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing RSI Engine...")

    # Simulate some close prices
    test_prices = [
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.15,
        43.61, 44.33, 44.83, 45.85, 46.08, 45.89, 46.03, 45.61,
        46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21,
        46.25, 45.71, 46.45, 45.78, 45.35, 44.03
    ]

    engine = RSIEngine(period=14)
    rsi = engine.seed("NSE:RELIANCE-EQ", "5", test_prices)
    print(f"RSI after seeding: {rsi}")

    # Simulate a new candle
    new_rsi = engine.update("NSE:RELIANCE-EQ", "5", 44.50)
    print(f"RSI after new candle (close=44.50): {new_rsi}")
    print(f"Is ready: {engine.is_ready('NSE:RELIANCE-EQ', '5')}")
