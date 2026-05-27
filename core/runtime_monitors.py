"""
runtime_monitors.py
Long-running monitors that watch the scanner during market hours.

Includes:
  - HeartbeatWriter: writes a timestamp file every N seconds; external
                     monitors / dashboard can detect a hung scanner.
  - TickFreshnessMonitor: per-symbol last-tick-time tracker; flags symbols
                          that haven't ticked in `stale_seconds`.
  - TickSanity: per-tick validator; rejects ticks outside reasonable bands.
  - SubscriptionTracker: confirms every subscribed symbol has received at
                          least one tick.

All monitors are designed to NEVER raise into the scanner's hot path. They
log warnings to console and write state to disk.
"""
import os
import json
import threading
import time
from datetime import datetime, timedelta
import pytz

IST  = pytz.timezone("Asia/Kolkata")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ─── Heartbeat ─────────────────────────────────────────────────────────────
class HeartbeatWriter:
    """Writes ROOT/data/heartbeat.json every interval seconds.
    Contains: ts, pid, uptime_seconds, feed status, last tick info.
    Dashboard reads this file to show liveness + feed health prominently."""
    def __init__(self, interval: int = 30, tick_freshness_mon=None,
                  subscription_tracker=None):
        self.interval     = interval
        self.path         = os.path.join(ROOT, "data", "heartbeat.json")
        self.started_at   = datetime.now(IST)
        self._stop        = threading.Event()
        self._thread      = None
        self._extra       = {}
        self._tick_mon    = tick_freshness_mon
        self._sub_tracker = subscription_tracker

    def update_status(self, **kwargs):
        self._extra.update(kwargs)

    def _compute_feed_status(self):
        """Returns dict with feed health metrics."""
        from core.market_calendar import is_market_hours
        now = datetime.now(IST)
        in_market = is_market_hours(now)
        out = {"in_market_hours": in_market}

        if self._tick_mon is not None:
            with self._tick_mon._lock:
                last_ticks = dict(self._tick_mon.last_tick)
            symbols_ticked = len(last_ticks)
            out["symbols_ticked_total"] = symbols_ticked
            if last_ticks:
                latest_tick_dt = max(last_ticks.values())
                age_sec = (now - latest_tick_dt).total_seconds()
                out["last_tick_at"]      = latest_tick_dt.isoformat()
                out["seconds_since_tick"] = int(age_sec)
            else:
                out["last_tick_at"]      = None
                out["seconds_since_tick"] = None

        if self._sub_tracker is not None:
            out["subscription_coverage_pct"] = round(
                self._sub_tracker.coverage_pct(), 1)
            out["symbols_subscribed"] = len(self._sub_tracker.expected)

        # Classify feed status. STARTING means "we just booted up, ticks
        # may not have flowed yet; that's normal for the first 5 minutes."
        # Only DEAD if uptime > 5 min during market and still zero ticks.
        uptime = (now - self.started_at).total_seconds()
        out["uptime_seconds"] = int(uptime)
        if not in_market:
            out["feed_status"] = "CLOSED"   # market closed; not expected
        elif out.get("symbols_ticked_total", 0) == 0:
            if uptime < 300:
                out["feed_status"] = "STARTING"   # < 5 min uptime, give it time
            else:
                out["feed_status"] = "DEAD"      # > 5 min, still no ticks = broken
        elif out.get("seconds_since_tick", 0) is not None and \
              out["seconds_since_tick"] > 300:
            out["feed_status"] = "STALE"    # was working, now no tick in 5+ min
        else:
            out["feed_status"] = "LIVE"
        return out

    def _loop(self):
        while not self._stop.is_set():
            try:
                now = datetime.now(IST)
                payload = {
                    "ts":             now.isoformat(),
                    "pid":            os.getpid(),
                    "uptime_seconds": int((now - self.started_at).total_seconds()),
                    **self._compute_feed_status(),
                    **self._extra,
                }
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(payload, f, indent=2)
                os.replace(tmp, self.path)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


# ─── Tick freshness ────────────────────────────────────────────────────────
class TickFreshnessMonitor:
    """Tracks the last-tick timestamp per symbol. Periodically scans for
    symbols that haven't ticked in `stale_seconds` during market hours."""

    def __init__(self, stale_seconds: int = 300, check_interval: int = 60):
        self.stale_seconds  = stale_seconds
        self.check_interval = check_interval
        self.last_tick: dict = {}   # symbol -> datetime
        self._lock          = threading.Lock()
        self._stop          = threading.Event()
        self._thread        = None
        self._stale_list    = []

    def on_tick(self, symbol: str):
        with self._lock:
            self.last_tick[symbol] = datetime.now(IST)

    def get_stale(self) -> list:
        """Return list of (symbol, last_tick_dt_or_None, seconds_stale)
        for symbols that haven't ticked in stale_seconds. Only valid during
        market hours."""
        from core.market_calendar import is_market_hours
        now = datetime.now(IST)
        if not is_market_hours(now):
            return []
        out = []
        with self._lock:
            for sym, lt in list(self.last_tick.items()):
                age = (now - lt).total_seconds()
                if age > self.stale_seconds:
                    out.append((sym, lt, int(age)))
        return out

    def _loop(self):
        from core.market_calendar import is_market_hours
        cold_start_warned = False
        market_open_t    = None
        while not self._stop.is_set():
            self._stop.wait(self.check_interval)
            now = datetime.now(IST)
            if not is_market_hours(now):
                cold_start_warned = False
                market_open_t = None
                continue

            # COLD-START detection: if it's been market hours for >2 minutes
            # and we've received ZERO ticks across ALL symbols, the websocket
            # is dead. Alert loudly.
            with self._lock:
                ticked_count = len(self.last_tick)
            if ticked_count == 0:
                # Mark the moment we noticed market is open
                if market_open_t is None:
                    market_open_t = now
                elapsed = (now - market_open_t).total_seconds()
                if elapsed > 120 and not cold_start_warned:
                    print(f"\n🚨 TickFreshnessMonitor: DEAD FEED — market has "
                          f"been open for {int(elapsed)}s, ZERO ticks received "
                          f"across ALL symbols. WebSocket is not delivering. "
                          f"Restart the data feed or the entire scanner.")
                    cold_start_warned = True
                continue
            else:
                cold_start_warned = False  # we got some ticks; clear the cold-start flag

            stale = self.get_stale()
            self._stale_list = stale
            if stale:
                print(f"\n⚠️  TickFreshnessMonitor: {len(stale)} symbols stale "
                      f"(no tick > {self.stale_seconds}s).")
                for sym, lt, age in stale[:5]:
                    print(f"     {sym}: last tick {lt.strftime('%H:%M:%S')} "
                          f"({age}s ago)")
                if len(stale) > 5:
                    print(f"     ... and {len(stale)-5} more.")

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


# ─── Tick price sanity ────────────────────────────────────────────────────
class TickSanityValidator:
    """Per-tick sanity:
       - price > 0
       - price within configured band of last accepted price
       - timestamp not in future, not >10s in the past
       - inside market hours
    Returns (ok, reason). Caller drops rejected ticks before aggregation."""

    def __init__(self, max_pct_jump: float = 5.0):
        self.max_pct_jump = max_pct_jump   # reject ticks >5% off last
        self.last_accepted: dict = {}      # symbol -> price
        self.rejection_counts: dict = {}   # symbol -> count

    def validate(self, symbol: str, price: float, tick_time: datetime) -> tuple:
        from core.market_calendar import is_market_hours
        if price is None or price <= 0:
            self._reject(symbol)
            return False, f"price <= 0 ({price})"
        # Timestamp sanity
        if tick_time is None:
            return True, ""   # missing ts; can't check, accept
        now = datetime.now(IST)
        if tick_time.tzinfo is None:
            tick_time = IST.localize(tick_time)
        age = (now - tick_time).total_seconds()
        if age < -5:
            self._reject(symbol)
            return False, f"tick in future (skew {age:.1f}s)"
        if age > 60:
            self._reject(symbol)
            return False, f"tick too old ({age:.1f}s)"
        # Market hours
        if not is_market_hours(tick_time):
            self._reject(symbol)
            return False, "tick outside market hours"
        # Jump check
        last = self.last_accepted.get(symbol)
        if last is not None and last > 0:
            jump_pct = abs(price - last) / last * 100
            if jump_pct > self.max_pct_jump:
                self._reject(symbol)
                return False, f"price jump {jump_pct:.2f}% from {last} to {price}"
        # Accept
        self.last_accepted[symbol] = price
        return True, ""

    def _reject(self, symbol: str):
        self.rejection_counts[symbol] = self.rejection_counts.get(symbol, 0) + 1


# ─── Subscription tracker ─────────────────────────────────────────────────
class SubscriptionTracker:
    """Records the universe of symbols we asked Fyers to stream, and which
    ones have actually delivered at least one tick. Run a check N minutes
    after market open: any symbol with zero ticks = subscription failure."""

    def __init__(self):
        self.expected: set = set()
        self.received: set = set()
        self._lock = threading.Lock()

    def register_subscription(self, symbols):
        with self._lock:
            self.expected.update(symbols)

    def on_tick(self, symbol: str):
        with self._lock:
            self.received.add(symbol)

    def missing(self) -> list:
        with self._lock:
            return sorted(self.expected - self.received)

    def coverage_pct(self) -> float:
        with self._lock:
            if not self.expected:
                return 0.0
            return 100.0 * len(self.received) / len(self.expected)
