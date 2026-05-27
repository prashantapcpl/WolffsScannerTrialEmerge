"""
candle_engine.py
Builds OHLCV candles from live tick data for all timeframes.
Supported: 1m, 2m, 5m, 15m, 30m, 1H, 1D, 1W
"""

import json
from datetime import datetime, time, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

# Timeframe definitions — key is what we use, value is minutes
TIMEFRAME_MINUTES = {
    "1":   1,
    "2":   2,
    "5":   5,
    "10":  10,
    "15":  15,
    "30":  30,
    "60":  60,
    "D":   375,   # Full trading day (9:15 to 3:30)
    "W":   1875,  # Full trading week
}

class Candle:
    """Single OHLCV candle."""
    def __init__(self, symbol: str, timeframe: str, open_time: datetime, open_price: float):
        self.symbol    = symbol
        self.timeframe = timeframe
        self.open_time = open_time
        self.open      = open_price
        self.high      = open_price
        self.low       = open_price
        self.close     = open_price
        self.volume    = 0
        self.is_closed = False

    @property
    def close_time(self) -> datetime:
        """Candle close time = open_time + timeframe duration."""
        tf_min = TIMEFRAME_MINUTES.get(self.timeframe, 1)
        return self.open_time + timedelta(minutes=tf_min)

    def update(self, price: float, volume: int = 0):
        self.high   = max(self.high, price)
        self.low    = min(self.low, price)
        self.close  = price
        self.volume += volume

    def to_dict(self) -> dict:
        return {
            "symbol":    self.symbol,
            "timeframe": self.timeframe,
            "open_time": self.open_time.isoformat(),
            "open":      self.open,
            "high":      self.high,
            "low":       self.low,
            "close":     self.close,
            "volume":    self.volume,
            "is_closed": self.is_closed,
        }

    def __repr__(self):
        return (f"Candle({self.symbol} {self.timeframe}m "
                f"O:{self.open} H:{self.high} L:{self.low} C:{self.close})")


class SymbolCandleBuilder:
    """
    Manages candle building for ONE symbol across ALL timeframes.
    """
    def __init__(self, symbol: str, timeframes: list, on_candle_close=None):
        self.symbol      = symbol
        self.timeframes  = timeframes
        self.on_candle_close = on_candle_close  # callback(candle)

        # Current open candle per timeframe
        self.current_candles: dict[str, Candle | None] = {tf: None for tf in timeframes}
        # Last N closed candles per timeframe (keep 50 for reference)
        self.closed_candles: dict[str, list] = {tf: [] for tf in timeframes}

    def _get_candle_open_time(self, tick_time: datetime, timeframe: str) -> datetime:
        """Calculate which candle slot this tick belongs to."""
        tf_min = TIMEFRAME_MINUTES.get(timeframe, 1)

        if timeframe in ("D", "W"):
            # Daily/Weekly — align to market open
            market_open = tick_time.replace(hour=9, minute=15, second=0, microsecond=0)
            return market_open

        # For minute-based timeframes, floor to the nearest interval
        total_minutes = tick_time.hour * 60 + tick_time.minute
        floored = (total_minutes // tf_min) * tf_min
        new_hour   = floored // 60
        new_minute = floored % 60
        return tick_time.replace(hour=new_hour, minute=new_minute, second=0, microsecond=0)

    def process_tick(self, price: float, volume: int, tick_time: datetime) -> list:
        """
        Process one price tick. Returns list of candles that CLOSED due to this tick.
        """
        closed_candles = []

        for tf in self.timeframes:
            candle_open_time = self._get_candle_open_time(tick_time, tf)
            current = self.current_candles[tf]

            if current is None:
                # Start first candle
                self.current_candles[tf] = Candle(self.symbol, tf, candle_open_time, price)
                self.current_candles[tf].update(price, volume)

            elif candle_open_time > current.open_time:
                # New candle period started — close the old one
                current.is_closed = True
                self.closed_candles[tf].append(current)
                # Keep only last 50 closed candles
                if len(self.closed_candles[tf]) > 50:
                    self.closed_candles[tf] = self.closed_candles[tf][-50:]

                closed_candles.append(current)

                # Fire the callback
                if self.on_candle_close:
                    try:
                        self.on_candle_close(current)
                    except Exception as e:
                        print(f"⚠️  Candle close callback error for {self.symbol}: {e}")

                # Start new candle
                self.current_candles[tf] = Candle(self.symbol, tf, candle_open_time, price)
                self.current_candles[tf].update(price, volume)

            else:
                # Same candle period — update it
                current.update(price, volume)

        return closed_candles

    def get_current_candle(self, timeframe: str) -> Candle | None:
        return self.current_candles.get(timeframe)

    def get_closed_candles(self, timeframe: str) -> list:
        return self.closed_candles.get(timeframe, [])

    def get_last_close(self, timeframe: str) -> float | None:
        candles = self.closed_candles.get(timeframe, [])
        if candles:
            return candles[-1].close
        return None


class CandleEngine:
    """
    Manages candle builders for ALL symbols.
    Central hub — receives ticks and routes to per-symbol builders.
    """

    def __init__(self, timeframes: list, on_candle_close=None):
        self.timeframes     = timeframes
        self.on_candle_close = on_candle_close
        self._builders: dict[str, SymbolCandleBuilder] = {}

    def _get_builder(self, symbol: str) -> SymbolCandleBuilder:
        if symbol not in self._builders:
            self._builders[symbol] = SymbolCandleBuilder(
                symbol, self.timeframes, self.on_candle_close
            )
        return self._builders[symbol]

    def process_tick(self, symbol: str, price: float, volume: int = 0,
                     tick_time: datetime = None) -> list:
        """
        Main entry point. Feed a tick from the WebSocket here.
        Returns list of candles that closed.
        """
        if tick_time is None:
            tick_time = datetime.now(IST)
        return self._get_builder(symbol).process_tick(price, volume, tick_time)

    def get_current_candle(self, symbol: str, timeframe: str) -> Candle | None:
        if symbol in self._builders:
            return self._builders[symbol].get_current_candle(timeframe)
        return None

    def get_closed_candles(self, symbol: str, timeframe: str) -> list:
        if symbol in self._builders:
            return self._builders[symbol].get_closed_candles(timeframe)
        return []

    def get_last_close(self, symbol: str, timeframe: str) -> float | None:
        if symbol in self._builders:
            return self._builders[symbol].get_last_close(timeframe)
        return None

    def get_all_symbols(self) -> list:
        return list(self._builders.keys())

    def seed_historical_candles(self, symbol: str, timeframe: str,
                                 historical_ohlcv: list):
        """
        Pre-load historical candles at startup (from Fyers REST API).
        historical_ohlcv: list of dicts with keys: open, high, low, close, volume, datetime
        """
        builder = self._get_builder(symbol)
        for bar in historical_ohlcv:
            candle = Candle(
                symbol    = symbol,
                timeframe = timeframe,
                open_time = bar["datetime"],
                open_price= bar["open"]
            )
            candle.high   = bar["high"]
            candle.low    = bar["low"]
            candle.close  = bar["close"]
            candle.volume = bar.get("volume", 0)
            candle.is_closed = True
            builder.closed_candles[timeframe].append(candle)

        # Keep only last 50
        if len(builder.closed_candles[timeframe]) > 50:
            builder.closed_candles[timeframe] = builder.closed_candles[timeframe][-50:]


if __name__ == "__main__":
    print("Testing Candle Engine...")

    closed_log = []

    def on_close(candle):
        closed_log.append(candle)
        print(f"  🕯️  CANDLE CLOSED: {candle}")

    engine = CandleEngine(timeframes=["1", "5"], on_candle_close=on_close)

    # Simulate ticks
    from datetime import timedelta
    base_time = datetime.now(IST).replace(hour=9, minute=15, second=0, microsecond=0)

    prices = [100, 101, 99, 102, 103, 98, 104, 105, 100, 102]
    for i, price in enumerate(prices):
        tick_time = base_time + timedelta(minutes=i)
        closed = engine.process_tick("NSE:RELIANCE-EQ", price, 1000, tick_time)
        print(f"  Tick {i+1}: {price} @ {tick_time.strftime('%H:%M')} → {len(closed)} candle(s) closed")

    print(f"\nTotal closed candles: {len(closed_log)}")
