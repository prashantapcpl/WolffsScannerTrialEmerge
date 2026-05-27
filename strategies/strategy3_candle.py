"""
strategy3_candle.py
Strategy 3 (rebuilt 2026-05-27) — Webhook Mirror with Candle-Based Entry.

Logic:
  1. Incoming webhook delivers (BUY|SELL, symbol) from external source
     (e.g. Chartink). Symbol with non-GENERAL state ⇒ ignore (dedupe).
  2. Lock REF candle = the LAST FULLY CLOSED candle of `trigger_timeframe`
     (5/15/60m) before the webhook arrival time. The ref candle's CLOSE
     PRICE is the reference for the drop-trigger.
  3. drop_pct (0..5):
        - 0      → fire BUY/SELL signal immediately at ref price
        - 0.5..5 → enter WATCHED state; wait until any subsequent same-TF
                   candle CLOSES at or below ref*(1 - drop_pct/100) for BUY
                   (mirror for SELL: at or above ref*(1 + rise_pct/100)).
  4. After first entry, monitor for AVG entries:
        - avg_drop_pct (1..5) from the LAST entry's price triggers another
          BUY/SELL on candle close.
  5. EXIT target = average(all entry prices) × (1 + exit_pct/100)  [BUY]
                 = average(all entry prices) × (1 - exit_pct/100)  [SELL]
        - When candle closes at/beyond target, fire EXIT and reset to
          GENERAL. Same symbol may re-enter via new webhook later.

Exposes the same interface old strategy3 used so signals/webhook_server.py
does NOT need to change:
   - process_incoming_signal(side, symbol, price, now)
   - on_candle_close(candle, timeframe)
   - on_tick(symbol, price)
   - save_state()
   - state_store.all_active() / .records / etc.
"""
import os
import sys
import json
import threading
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

IST = pytz.timezone("Asia/Kolkata")

VALID_TFS = ("5", "15", "60")


# ─── Record + State Store ───────────────────────────────────────────────────
class S3State:
    GENERAL       = "GENERAL"
    WATCHED_BUY   = "WATCHED_BUY"     # ref locked; waiting for drop trigger
    WATCHED_SELL  = "WATCHED_SELL"
    ACTIVE_BUY    = "ACTIVE_BUY"      # at least one BUY fired
    ACTIVE_SELL   = "ACTIVE_SELL"


class S3Record:

    __slots__ = (
        "symbol", "plain_name", "company_name",
        "state", "side",
        "ref_price", "ref_time",      # ref candle CLOSE price + OPEN time
        "trigger_tf",                 # TF used for this entry's ref + monitoring
        "entries",                    # list of {"price", "time", "level", "label"}
        "current_price",
        "last_signal_at",             # when the most recent inbound webhook arrived
    )

    def __init__(self, symbol, plain_name="", company_name=""):
        self.symbol         = symbol
        self.plain_name     = plain_name
        self.company_name   = company_name
        self.state          = S3State.GENERAL
        self.side           = None
        self.ref_price      = None
        self.ref_time       = None
        self.trigger_tf     = None
        self.entries        = []
        self.current_price  = None
        self.last_signal_at = None

    def reset_to_general(self):
        self.state = S3State.GENERAL
        self.side  = None
        self.ref_price = None
        self.ref_time  = None
        self.trigger_tf = None
        self.entries   = []
        self.last_signal_at = None

    def to_dict(self):
        return {
            "symbol":          self.symbol,
            "plain_name":      self.plain_name,
            "company_name":    self.company_name,
            "state":           self.state,
            "side":            self.side,
            "ref_price":       self.ref_price,
            "ref_time":        self.ref_time,
            "trigger_tf":      self.trigger_tf,
            "entries":         list(self.entries),
            "current_price":   self.current_price,
            "last_signal_at":  self.last_signal_at,
        }

    @classmethod
    def from_dict(cls, d):
        r = cls(d["symbol"], d.get("plain_name", ""), d.get("company_name", ""))
        r.state          = d.get("state", S3State.GENERAL)
        r.side           = d.get("side")
        r.ref_price      = d.get("ref_price")
        r.ref_time       = d.get("ref_time")
        r.trigger_tf     = d.get("trigger_tf")
        r.entries        = list(d.get("entries", []))
        r.current_price  = d.get("current_price")
        r.last_signal_at = d.get("last_signal_at")
        return r


class S3StateStore:

    def __init__(self, path):
        self._path     = path
        self._records  = {}
        self._signal_log = []
        self._lock     = threading.Lock()
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r") as f:
                data = json.load(f)
            # Be lenient: accept records as dict-of-dicts OR list
            records = data.get("records", {})
            if isinstance(records, dict):
                for sym, d in records.items():
                    if isinstance(d, dict) and "symbol" in d:
                        self._records[sym] = S3Record.from_dict(d)
                    # silently ignore old schema entries
            self._signal_log = data.get("signal_log", [])
        except Exception as e:
            print(f"   [S3] state load error: {e}")

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            data = {
                "records":    {s: r.to_dict() for s, r in self._records.items()},
                "signal_log": list(self._signal_log),
            }
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)

    def get(self, symbol):
        return self._records.get(symbol)

    def get_or_create(self, symbol, plain_name="", company_name=""):
        with self._lock:
            r = self._records.get(symbol)
            if r is None:
                r = S3Record(symbol, plain_name, company_name)
                self._records[symbol] = r
            else:
                # opportunistically fill in names
                if plain_name and not r.plain_name:
                    r.plain_name = plain_name
                if company_name and not r.company_name:
                    r.company_name = company_name
            return r

    def all_records(self):
        return list(self._records.values())

    def all_active(self):
        return [r for r in self._records.values()
                if r.state in (S3State.ACTIVE_BUY, S3State.ACTIVE_SELL)]

    def all_watched(self):
        return [r for r in self._records.values()
                if r.state in (S3State.WATCHED_BUY, S3State.WATCHED_SELL)]

    def log_signal(self, entry: dict):
        self._signal_log.append(entry)

    def signal_log(self):
        return list(self._signal_log)


# ─── Strategy ──────────────────────────────────────────────────────────────
class Strategy3CandleMirror:
    """Candle-based webhook-mirror strategy.

    Config keys (all optional with defaults):
      trigger_timeframe   : "5" | "15" | "60"  (default "5")
      drop_pct_buy        : 0..5               (default uses 'drop_pct')
      rise_pct_sell       : 0..5               (default uses 'rise_pct')
      avg_drop_pct_buy    : 1..5               (default uses 'avg_pct_buy')
      avg_rise_pct_sell   : 1..5               (default uses 'avg_pct_sell')
      exit_pct_buy        : configurable       (default 4.0)
      exit_pct_sell       : configurable       (default 4.0)

    Legacy aliases (drop_pct / rise_pct / avg_pct_buy / avg_pct_sell /
    exit_offset_pct_buy / exit_offset_pct_sell) are read as fallbacks so
    the existing config.json doesn't need to change.
    """

    def __init__(self, config, state_store, data_store, mapper,
                 webhook_sender=None):
        self.config         = config
        self.state_store    = state_store
        self.data_store     = data_store
        self.mapper         = mapper
        self.webhook_sender = webhook_sender
        self._lock          = threading.Lock()

    # ─── Config helpers ────────────────────────────────────────────────
    def _trigger_tf(self) -> str:
        """Legacy single-side TF (used by on_candle_close to gate by TF).
        Returns the union -- if either buy or sell TF is X, candles of X
        are processed. on_candle_close uses rec.trigger_tf for the actual
        record-specific check."""
        tf = str(self.config.get("trigger_timeframe", "5"))
        if tf not in VALID_TFS:
            tf = "5"
        return tf

    def _trigger_tf_buy(self) -> str:
        tf = str(self.config.get("trigger_timeframe_buy",
                self.config.get("trigger_timeframe", "5")))
        return tf if tf in VALID_TFS else "5"

    def _trigger_tf_sell(self) -> str:
        tf = str(self.config.get("trigger_timeframe_sell",
                self.config.get("trigger_timeframe", "5")))
        return tf if tf in VALID_TFS else "5"

    def _drop_pct_buy(self) -> float:
        return float(self.config.get("drop_pct_buy",
                self.config.get("drop_pct", 0.0)))

    def _rise_pct_sell(self) -> float:
        return float(self.config.get("rise_pct_sell",
                self.config.get("rise_pct", 0.0)))

    def _avg_drop_pct_buy(self) -> float:
        return float(self.config.get("avg_drop_pct_buy",
                self.config.get("avg_pct_buy", 3.0)))

    def _avg_rise_pct_sell(self) -> float:
        return float(self.config.get("avg_rise_pct_sell",
                self.config.get("avg_pct_sell", 3.0)))

    def _exit_pct_buy(self) -> float:
        return float(self.config.get("exit_pct_buy",
                self.config.get("exit_offset_pct_buy", 4.0)))

    def _exit_pct_sell(self) -> float:
        return float(self.config.get("exit_pct_sell",
                self.config.get("exit_offset_pct_sell", 4.0)))

    # ─── Last-completed-candle lookup ──────────────────────────────────
    def _last_completed_candle(self, symbol: str, tf: str,
                                 now: datetime) -> dict | None:
        """Return the candle of timeframe `tf` whose CLOSE TIME is the most
        recent completed close strictly <= now. The candle's OPEN time =
        close_time - tf minutes.

        Example: now = 14:15:30, tf = "5":
          floor to 14:15 → that's the close time of the candle opening 14:10.
          Candle dict has datetime = 14:10 (open).
        """
        tf_min  = int(tf)
        # Floor `now` to TF boundary
        floor_minute = (now.minute // tf_min) * tf_min
        floor_time   = now.replace(minute=floor_minute, second=0, microsecond=0)
        # If floor_time > now (shouldn't happen) or floor_time == now, that
        # candle just closed; use the candle ending at floor_time
        if floor_time > now:
            floor_time -= timedelta(minutes=tf_min)
        # If floor_time is EXACTLY now and seconds==0 etc., that candle may
        # not yet be in the CSV. Walk back one step to be safe.
        if floor_time == now.replace(second=0, microsecond=0):
            # exactly aligned -- assume that candle close hasn't propagated
            # to disk yet; use the previous one
            pass   # actually, since CSV is updated by the live engine, the
                    # boundary candle MAY exist. The robust path is to load
                    # and look up by exact match.
        # candle's OPEN time = close_time - tf_min
        candle_open = floor_time - timedelta(minutes=tf_min)
        # Load and find
        try:
            candles = self.data_store.load_candles(symbol, tf)
        except Exception:
            return None
        if not candles:
            return None
        # Search backwards for a candle with datetime <= candle_open
        target = candle_open
        # tz handling: candles have IST-localized datetimes; candle_open may be naive
        if target.tzinfo is None:
            target = IST.localize(target)
        # Walk in reverse (recent first)
        for c in reversed(candles):
            dt = c["datetime"]
            if dt.tzinfo is None:
                dt = IST.localize(dt)
            if dt <= target:
                return c
        return None

    # ─── Public API ────────────────────────────────────────────────────
    def process_incoming_signal(self, side: str, symbol: str,
                                  price=None, now=None):
        """External webhook callback. side is 'BUY' / 'SELL' / 'EXIT_BUY' /
        'EXIT_SELL'. We only act on BUY and SELL (entries). EXIT_BUY/EXIT_SELL
        are ignored — exit is computed by our target logic.
        """
        if side not in ("BUY", "SELL"):
            # legacy EXIT events from older Chartink alerts -- ignore
            print(f"   [S3] ignoring inbound {side} for {symbol} "
                  f"(exit is computed, not delivered)")
            return

        if now is None:
            now = datetime.now(IST)
        if now.tzinfo is None:
            now = IST.localize(now)

        plain = ""
        try:
            plain = self.mapper.get_plain_name(symbol) if self.mapper else ""
        except Exception:
            pass
        company = ""
        try:
            company = self.mapper.get_company_name(plain) if (self.mapper and plain) else ""
        except Exception:
            pass

        with self._lock:
            rec = self.state_store.get_or_create(symbol, plain, company)

            # Dedup: ignore inbound signal if already in non-GENERAL
            if rec.state != S3State.GENERAL:
                print(f"   [S3] DUP ignored: {symbol} {side} (currently {rec.state})")
                return

            tf = self._trigger_tf_buy() if side == "BUY" else self._trigger_tf_sell()
            ref = self._last_completed_candle(symbol, tf, now)
            if not ref:
                print(f"   [S3] {symbol}: no {tf}m candle available for ref")
                return

            rec.ref_price      = float(ref["close"])
            rec.ref_time       = ref["datetime"].isoformat() if hasattr(
                                    ref["datetime"], "isoformat") else str(ref["datetime"])
            rec.trigger_tf     = tf
            rec.side           = side
            rec.last_signal_at = now.isoformat()

            tf_min       = int(tf)
            candle_close = ref["datetime"] + timedelta(minutes=tf_min)
            if candle_close.tzinfo is None:
                candle_close = IST.localize(candle_close)

            if side == "BUY":
                drop_pct = self._drop_pct_buy()
                if drop_pct <= 0:
                    self._fire_entry(rec, rec.ref_price, candle_close, level=1)
                    rec.state = S3State.ACTIVE_BUY
                else:
                    rec.state = S3State.WATCHED_BUY
                    print(f"   [S3] {symbol}: WATCHED_BUY ref={rec.ref_price} "
                          f"({tf}m); waiting for close ≤ "
                          f"{rec.ref_price*(1-drop_pct/100):.2f} ({drop_pct}% drop)")
            else:    # SELL
                rise_pct = self._rise_pct_sell()
                if rise_pct <= 0:
                    self._fire_entry(rec, rec.ref_price, candle_close, level=1)
                    rec.state = S3State.ACTIVE_SELL
                else:
                    rec.state = S3State.WATCHED_SELL
                    print(f"   [S3] {symbol}: WATCHED_SELL ref={rec.ref_price} "
                          f"({tf}m); waiting for close ≥ "
                          f"{rec.ref_price*(1+rise_pct/100):.2f} ({rise_pct}% rise)")

            self.state_store.save()

    def on_candle_close(self, candle, timeframe):
        """Called by main on every candle close. We process candles for the
        trigger TFs of both sides (buy_tf and sell_tf can differ); the
        per-record check below filters to whichever TF the record itself
        was opened on."""
        if timeframe not in (self._trigger_tf_buy(), self._trigger_tf_sell()):
            return

        # Extract from candle (CandleEngine candle is an object with attrs)
        symbol = getattr(candle, "symbol", None) or candle.get("symbol")
        close  = float(getattr(candle, "close", None) or candle.get("close"))
        ct     = getattr(candle, "close_time", None) or (
                    getattr(candle, "open_time",  None)
                    or candle.get("datetime"))
        if ct is None:
            return
        if hasattr(ct, "tzinfo") and ct.tzinfo is None:
            ct = IST.localize(ct)
        # If we have open_time, compute close_time as open + tf
        if isinstance(ct, datetime) and ct == getattr(candle, "open_time", None):
            ct = ct + timedelta(minutes=int(timeframe))

        with self._lock:
            rec = self.state_store.get(symbol)
            if rec is None:
                return
            # Per-record TF gate: only process if THIS candle's TF matches
            # the TF this record was opened on.
            if rec.trigger_tf and timeframe != rec.trigger_tf:
                return
            rec.current_price = close

            # WATCHED_BUY → ACTIVE_BUY when drop_pct reached
            if rec.state == S3State.WATCHED_BUY:
                drop_pct = self._drop_pct_buy()
                if rec.ref_price and rec.ref_price > 0:
                    drop = (rec.ref_price - close) / rec.ref_price * 100
                    if drop >= drop_pct:
                        self._fire_entry(rec, close, ct, level=1)
                        rec.state = S3State.ACTIVE_BUY
                        self.state_store.save()
                return

            # WATCHED_SELL → ACTIVE_SELL when rise_pct reached
            if rec.state == S3State.WATCHED_SELL:
                rise_pct = self._rise_pct_sell()
                if rec.ref_price and rec.ref_price > 0:
                    rise = (close - rec.ref_price) / rec.ref_price * 100
                    if rise >= rise_pct:
                        self._fire_entry(rec, close, ct, level=1)
                        rec.state = S3State.ACTIVE_SELL
                        self.state_store.save()
                return

            # ACTIVE_BUY: avg or exit
            if rec.state == S3State.ACTIVE_BUY and rec.entries:
                avg_drop = self._avg_drop_pct_buy()
                # avg_drop <= 0 ⇒ averaging DISABLED. The first BUY is the
                # only entry; subsequent candles only check the exit target.
                if avg_drop > 0:
                    last_price = rec.entries[-1]["price"]
                    if last_price > 0:
                        drop_from_last = (last_price - close) / last_price * 100
                        if drop_from_last >= avg_drop:
                            self._fire_entry(rec, close, ct,
                                              level=len(rec.entries) + 1)
                            self.state_store.save()
                            return
                # Exit target: avg of all entries × (1 + exit_pct/100)
                exit_pct = self._exit_pct_buy()
                avg_p    = sum(e["price"] for e in rec.entries) / len(rec.entries)
                target   = avg_p * (1 + exit_pct / 100)
                if close >= target:
                    self._fire_exit(rec, close, ct, avg_p, target)
                    self.state_store.save()
                return

            # ACTIVE_SELL: avg or exit (mirror)
            if rec.state == S3State.ACTIVE_SELL and rec.entries:
                avg_rise = self._avg_rise_pct_sell()
                if avg_rise > 0:
                    last_price = rec.entries[-1]["price"]
                    if last_price > 0:
                        rise_from_last = (close - last_price) / last_price * 100
                        if rise_from_last >= avg_rise:
                            self._fire_entry(rec, close, ct,
                                              level=len(rec.entries) + 1)
                            self.state_store.save()
                            return
                exit_pct = self._exit_pct_sell()
                avg_p    = sum(e["price"] for e in rec.entries) / len(rec.entries)
                target   = avg_p * (1 - exit_pct / 100)
                if close <= target:
                    self._fire_exit(rec, close, ct, avg_p, target)
                    self.state_store.save()
                return

    def on_tick(self, symbol: str, price: float):
        """Update display price only. All decisions happen on candle close."""
        rec = self.state_store.get(symbol)
        if rec:
            rec.current_price = float(price)

    def save_state(self):
        self.state_store.save()

    # ─── Carry-forward / replay missed candles ─────────────────────────
    def replay_missed_candles(self, now: datetime = None) -> int:
        """Walk every candle close that occurred between each non-GENERAL
        record's last_signal_at (or last entry time) and `now`, replaying
        them through the state machine. Catches BUY/AVG/EXIT events that
        would have fired during scanner downtime.

        Returns the number of records that advanced state during replay.
        """
        if now is None:
            now = datetime.now(IST)
        if now.tzinfo is None:
            now = IST.localize(now)

        advanced = 0
        with self._lock:
            records = [r for r in self.state_store.all_records()
                       if r.state != S3State.GENERAL]

        if not records:
            return 0

        print(f"\n🔄 [S3] Replaying missed candles for {len(records)} non-GENERAL records...")

        for rec in records:
            # Determine from-time: prefer last entry's time; otherwise last_signal_at
            try:
                if rec.entries:
                    from_ts = datetime.fromisoformat(rec.entries[-1]["time"])
                elif rec.last_signal_at:
                    from_ts = datetime.fromisoformat(rec.last_signal_at)
                else:
                    continue
            except Exception:
                continue
            if from_ts.tzinfo is None:
                from_ts = IST.localize(from_ts)

            tf = rec.trigger_tf or self._trigger_tf_buy()
            try:
                tf_min = int(tf)
            except Exception:
                continue

            # Load candles, walk forward from from_ts
            try:
                all_candles = self.data_store.load_candles(rec.symbol, tf)
            except Exception:
                continue
            if not all_candles:
                continue

            initial_state    = rec.state
            initial_entries  = len(rec.entries)
            for c in all_candles:
                dt = c["datetime"]
                if dt.tzinfo is None:
                    dt = IST.localize(dt)
                # Only process candles AFTER from_ts and BEFORE now
                if dt <= from_ts:
                    continue
                if dt + timedelta(minutes=tf_min) > now:
                    break

                # Build minimal candle object for on_candle_close
                fake = {
                    "symbol":     rec.symbol,
                    "datetime":   dt,
                    "open":       c["open"],
                    "high":       c["high"],
                    "low":        c["low"],
                    "close":      c["close"],
                    "volume":     c.get("volume", 0),
                }
                # Call internal handler directly (skip the gate since we know
                # this is the right TF for this record)
                self._replay_one_candle(fake, tf)

                # Stop if the record left non-active states
                if rec.state == S3State.GENERAL:
                    break

            if rec.state != initial_state or len(rec.entries) != initial_entries:
                advanced += 1

        self.state_store.save()
        print(f"✅ [S3] Replay complete: {advanced}/{len(records)} records advanced.")
        return advanced

    def _replay_one_candle(self, candle_dict: dict, timeframe: str):
        """Process a single historical candle through the same state-machine
        logic as on_candle_close, but skipping the per-record TF gate (caller
        already confirmed). Called from replay_missed_candles."""
        symbol = candle_dict["symbol"]
        close  = float(candle_dict["close"])
        ct     = candle_dict["datetime"] + timedelta(minutes=int(timeframe))
        if hasattr(ct, "tzinfo") and ct.tzinfo is None:
            ct = IST.localize(ct)

        rec = self.state_store.get(symbol)
        if rec is None:
            return
        rec.current_price = close

        if rec.state == S3State.WATCHED_BUY:
            drop_pct = self._drop_pct_buy()
            if rec.ref_price and rec.ref_price > 0:
                drop = (rec.ref_price - close) / rec.ref_price * 100
                if drop >= drop_pct:
                    self._fire_entry(rec, close, ct, level=1)
                    rec.state = S3State.ACTIVE_BUY
            return

        if rec.state == S3State.WATCHED_SELL:
            rise_pct = self._rise_pct_sell()
            if rec.ref_price and rec.ref_price > 0:
                rise = (close - rec.ref_price) / rec.ref_price * 100
                if rise >= rise_pct:
                    self._fire_entry(rec, close, ct, level=1)
                    rec.state = S3State.ACTIVE_SELL
            return

        if rec.state == S3State.ACTIVE_BUY and rec.entries:
            avg_drop = self._avg_drop_pct_buy()
            if avg_drop > 0:
                last_price = rec.entries[-1]["price"]
                if last_price > 0:
                    if (last_price - close) / last_price * 100 >= avg_drop:
                        self._fire_entry(rec, close, ct,
                                          level=len(rec.entries) + 1)
                        return
            exit_pct = self._exit_pct_buy()
            avg_p    = sum(e["price"] for e in rec.entries) / len(rec.entries)
            target   = avg_p * (1 + exit_pct / 100)
            if close >= target:
                self._fire_exit(rec, close, ct, avg_p, target)
            return

        if rec.state == S3State.ACTIVE_SELL and rec.entries:
            avg_rise = self._avg_rise_pct_sell()
            if avg_rise > 0:
                last_price = rec.entries[-1]["price"]
                if last_price > 0:
                    if (close - last_price) / last_price * 100 >= avg_rise:
                        self._fire_entry(rec, close, ct,
                                          level=len(rec.entries) + 1)
                        return
            exit_pct = self._exit_pct_sell()
            avg_p    = sum(e["price"] for e in rec.entries) / len(rec.entries)
            target   = avg_p * (1 - exit_pct / 100)
            if close <= target:
                self._fire_exit(rec, close, ct, avg_p, target)
            return

    # ─── Firing helpers ────────────────────────────────────────────────
    def _fire_entry(self, rec, price, when, level):
        if level == 1:
            label = "BUY" if rec.side == "BUY" else "SELL"
        else:
            label = (f"Avg {level-1}" if rec.side == "BUY"
                     else f"Avg Short {level-1}")
        entry = {"price": float(price),
                 "time":  when.isoformat() if hasattr(when, "isoformat") else str(when),
                 "level": level,
                 "label": label}
        rec.entries.append(entry)
        print(f"   [S3] 🚀 {label} {rec.symbol} @ Rs.{price:.2f} "
              f"(level {level}, time {entry['time']})")
        # Log
        self.state_store.log_signal({
            "type":       "BUY" if rec.side == "BUY" and level == 1
                          else ("SELL" if rec.side == "SELL" and level == 1
                                else label),
            "label":      label,
            "stock":      rec.plain_name or rec.symbol,
            "plain_name": rec.plain_name,
            "symbol":     rec.symbol,
            "price":      float(price),
            "time":       entry["time"],
            "level":      level,
            "ref_price":  rec.ref_price,
            "trigger_tf": rec.trigger_tf,
            "side":       rec.side,
        })
        # Outgoing webhook
        if self.webhook_sender:
            try:
                if rec.side == "BUY":
                    if level == 1:
                        self.webhook_sender.send_buy(
                            symbol=rec.symbol, plain_name=rec.plain_name,
                            company_name=rec.company_name,
                            buy_price=float(price),
                            reference_price=rec.ref_price,
                            drop_pct=0.0, rsi_at_watch=0.0)
                    else:
                        self.webhook_sender.send_avg(
                            symbol=rec.symbol, plain_name=rec.plain_name,
                            company_name=rec.company_name,
                            avg_price=float(price),
                            drop_pct=0.0, avg_num=level-1)
                else:
                    if level == 1:
                        self.webhook_sender.send_sell(
                            symbol=rec.symbol, plain_name=rec.plain_name,
                            company_name=rec.company_name,
                            short_price=float(price),
                            reference_price=rec.ref_price,
                            rise_pct=0.0, rsi_at_watch=0.0)
                    else:
                        self.webhook_sender.send_sell_avg(
                            symbol=rec.symbol, plain_name=rec.plain_name,
                            company_name=rec.company_name,
                            avg_price=float(price),
                            rise_pct=0.0, avg_num=level-1)
            except Exception as e:
                print(f"   [S3] webhook send error ({label}): {e}")

    def _fire_exit(self, rec, exit_price, when, avg_price, target):
        side  = rec.side
        label = "EXIT_BUY" if side == "BUY" else "EXIT_SELL"
        # P&L: BUY = (exit - avg) / avg * 100;  SELL = (avg - exit) / avg * 100
        if side == "BUY":
            pnl_pct = (exit_price - avg_price) / avg_price * 100 if avg_price > 0 else 0
        else:
            pnl_pct = (avg_price - exit_price) / avg_price * 100 if avg_price > 0 else 0
        n_avgs = max(0, len(rec.entries) - 1)
        when_iso = when.isoformat() if hasattr(when, "isoformat") else str(when)
        print(f"   [S3] 🏁 {label} {rec.symbol} @ Rs.{exit_price:.2f}  "
              f"avg_entry={avg_price:.2f}  target={target:.2f}  "
              f"P&L={pnl_pct:+.2f}%  avgs={n_avgs}")
        self.state_store.log_signal({
            "type":         label,
            "label":        label,
            "stock":        rec.plain_name or rec.symbol,
            "plain_name":   rec.plain_name,
            "symbol":       rec.symbol,
            "price":        float(exit_price),
            "time":         when_iso,
            "entry_price":  float(avg_price),
            "target_price": float(target),
            "avg_count":    n_avgs,
            "pnl_pct":      round(pnl_pct, 2),
            "side":         side,
        })
        if self.webhook_sender:
            try:
                if side == "BUY":
                    self.webhook_sender.send_exit(
                        symbol=rec.symbol, plain_name=rec.plain_name,
                        company_name=rec.company_name,
                        exit_price=float(exit_price),
                        buy_price=float(avg_price),
                        rsi_at_exit=0.0,
                        avg_count=n_avgs)
                else:
                    self.webhook_sender.send_sell_exit(
                        symbol=rec.symbol, plain_name=rec.plain_name,
                        company_name=rec.company_name,
                        exit_price=float(exit_price),
                        short_price=float(avg_price),
                        rsi_at_exit=0.0,
                        avg_count=n_avgs)
            except Exception as e:
                print(f"   [S3] webhook send error ({label}): {e}")
        # Reset record to GENERAL — same symbol may re-enter on next webhook
        rec.reset_to_general()
