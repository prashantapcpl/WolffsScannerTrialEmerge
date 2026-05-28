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
       - timestamp not in future, not too old
       - inside market hours
       - (optional) price within configured band of last accepted price

    BUG-FIX 2026-05-28 — the previous jump-band check silently corrupted the
    09:15 candle for any stock with a meaningful gap (gap opens, circuit
    movers, first tick after overnight). It compared today's first tick
    against yesterday's last accepted price stored in memory. That bad open
    then entered Wilder smoothing and poisoned RSI permanently.

    The fix:
      • Skip the jump check if no fresh baseline (`baseline_stale_seconds`
        elapsed since the last accepted tick — overnight gap, reconnect,
        scanner restart all qualify).
      • Widen threshold during opening volatility window 09:15–09:30 IST.
      • Allow legitimate NSE circuit moves (5/10/20%) without rejection.
      • Master switch via env `TICK_SANITY_JUMP_CHECK_ENABLED` (default on).
      • Log first N rejections per symbol so the corruption is visible.

    Returns (ok, reason). Caller drops rejected ticks before aggregation."""

    # Default policy (all overridable via env or constructor):
    DEFAULT_MAX_PCT_JUMP        = 5.0     # normal session jump cap
    DEFAULT_OPENING_PCT_JUMP    = 25.0    # 09:15–09:30 window cap
    DEFAULT_BASELINE_STALE_SEC  = 1800    # 30 min → treat next tick as "fresh"
    DEFAULT_LOG_FIRST_N         = 3       # log first 3 rejections per symbol
    CIRCUIT_TOLERANCE_PCT       = 0.5     # allow ±0.5% slack around 5/10/20%

    def __init__(self,
                 max_pct_jump: float | None       = None,
                 opening_pct_jump: float | None   = None,
                 baseline_stale_sec: int | None   = None,
                 jump_check_enabled: bool | None  = None):
        # Env overrides — let ops toggle without code change
        env = os.environ.get
        self.max_pct_jump       = float(env("TICK_SANITY_MAX_PCT_JUMP",
                                            max_pct_jump or self.DEFAULT_MAX_PCT_JUMP))
        self.opening_pct_jump   = float(env("TICK_SANITY_OPENING_PCT_JUMP",
                                            opening_pct_jump or self.DEFAULT_OPENING_PCT_JUMP))
        self.baseline_stale_sec = int(env("TICK_SANITY_BASELINE_STALE_SEC",
                                          baseline_stale_sec or self.DEFAULT_BASELINE_STALE_SEC))
        if jump_check_enabled is None:
            jump_check_enabled = env("TICK_SANITY_JUMP_CHECK_ENABLED",
                                     "true").lower() not in ("false", "0", "no", "off")
        self.jump_check_enabled = bool(jump_check_enabled)

        self.last_accepted: dict      = {}   # symbol -> price
        self.last_accepted_time: dict = {}   # symbol -> datetime  (NEW)
        self.rejection_counts: dict   = {}   # symbol -> count
        self._rej_logged: dict        = {}   # symbol -> times logged so far

    @staticmethod
    def _is_opening_window(t: datetime) -> bool:
        """09:15:00 IST through 09:29:59 IST — first 15 minutes."""
        return t.hour == 9 and 15 <= t.minute < 30

    def _allowed_jump_pct(self, tick_time: datetime) -> float:
        """Wider band during the opening 15 min; tight thereafter."""
        return (self.opening_pct_jump
                if self._is_opening_window(tick_time)
                else self.max_pct_jump)

    @classmethod
    def _is_circuit_move(cls, jump_pct: float) -> bool:
        """NSE allows ±5%, ±10%, ±20% daily price bands. A tick that lands
        exactly on one of these (within tolerance) is legitimate — most
        commonly a stock hitting upper/lower circuit."""
        tol = cls.CIRCUIT_TOLERANCE_PCT
        for band in (5.0, 10.0, 20.0):
            if abs(jump_pct - band) <= tol:
                return True
        return False

    def validate(self, symbol: str, price: float, tick_time: datetime) -> tuple:
        from core.market_calendar import is_market_hours
        if price is None or price <= 0:
            return self._reject(symbol, f"price <= 0 ({price})")
        # Timestamp sanity
        if tick_time is None:
            return True, ""   # missing ts; can't check, accept
        now = datetime.now(IST)
        if tick_time.tzinfo is None:
            tick_time = IST.localize(tick_time)
        age = (now - tick_time).total_seconds()
        if age < -5:
            return self._reject(symbol, f"tick in future (skew {age:.1f}s)")
        if age > 60:
            return self._reject(symbol, f"tick too old ({age:.1f}s)")
        # Market hours
        if not is_market_hours(tick_time):
            return self._reject(symbol, "tick outside market hours")
        # Jump check (only if enabled, baseline is fresh, and not a circuit move)
        if self.jump_check_enabled:
            last      = self.last_accepted.get(symbol)
            last_time = self.last_accepted_time.get(symbol)
            baseline_fresh = (
                last is not None and last > 0
                and last_time is not None
                and (tick_time - last_time).total_seconds() <= self.baseline_stale_sec
            )
            if baseline_fresh:
                jump_pct = abs(price - last) / last * 100
                allowed  = self._allowed_jump_pct(tick_time)
                if jump_pct > allowed and not self._is_circuit_move(jump_pct):
                    return self._reject(
                        symbol,
                        f"price jump {jump_pct:.2f}% > {allowed:.1f}% "
                        f"(last {last} → {price})"
                    )
        # Accept
        self.last_accepted[symbol]      = price
        self.last_accepted_time[symbol] = tick_time
        return True, ""

    def _reject(self, symbol: str, reason: str) -> tuple:
        self.rejection_counts[symbol] = self.rejection_counts.get(symbol, 0) + 1
        logged = self._rej_logged.get(symbol, 0)
        if logged < self.DEFAULT_LOG_FIRST_N:
            print(f"  ⚠️  TickSanity REJECT [{symbol}] {reason}")
            self._rej_logged[symbol] = logged + 1
            if logged + 1 == self.DEFAULT_LOG_FIRST_N:
                print(f"     (further rejections for {symbol} suppressed; "
                      f"see get_stats() for counts)")
        return False, reason

    def get_stats(self) -> dict:
        """Snapshot of rejection counts — call from heartbeat / dashboard."""
        total = sum(self.rejection_counts.values())
        return {
            "total_rejections":  total,
            "symbols_with_rejections": len(self.rejection_counts),
            "top_rejected": sorted(
                self.rejection_counts.items(),
                key=lambda kv: kv[1], reverse=True
            )[:10],
            "policy": {
                "jump_check_enabled":  self.jump_check_enabled,
                "max_pct_jump":        self.max_pct_jump,
                "opening_pct_jump":    self.opening_pct_jump,
                "baseline_stale_sec":  self.baseline_stale_sec,
            },
        }


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
