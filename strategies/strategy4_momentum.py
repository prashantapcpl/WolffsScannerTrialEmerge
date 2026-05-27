"""
strategy4_momentum.py — Multi-Timeframe RSI Momentum (Scanner 4)

Self-contained strategy module — completely independent of Scanner 1/2/3.

States per stock (BUY side):
  GENERAL → FLAGGED_BUY → BUY1_ACTIVE → BUY2_ACTIVE → BUY3_ACTIVE → BUY4_ACTIVE → COOLING_BUY
  (FLAGGED_BUY can be reached directly from GENERAL; BUY1_ACTIVE can also be
   reached directly from GENERAL on path A.)

States per stock (SELL side, mirror image):
  GENERAL → FLAGGED_SELL → SELL1_ACTIVE → SELL2_ACTIVE → SELL3_ACTIVE → SELL4_ACTIVE → COOLING_SELL

Entry / target / exit logic operates on 15m candle closes.  Daily and Weekly
RSI are read live from the strategy's own RSIEngine instance (which the caller
seeds from rsi_cache at startup).

A daily-RSI exit override fires once per day at exit_check_time (15:25 IST)
via start_exit_check_thread().

Webhook payload fields:
  signal, scanner, label, symbol, stock, company, price, time, state,
  target_price, pnl_pct, source.
"""

import json
import os
import threading
import time
import requests
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.dirname(__file__))
IST  = pytz.timezone("Asia/Kolkata")


# ─── Helpers ────────────────────────────────────────────────────────────────────
def _parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
        if dt.tzinfo is None:
            dt = IST.localize(dt)
        return dt
    except Exception:
        return None


def _fmt_dt(dt):
    return dt.isoformat() if dt and hasattr(dt, "isoformat") else None


# ─── S4Record ───────────────────────────────────────────────────────────────────
class S4Record:
    """
    State for a single stock in Strategy 4.

    side          : None | "BUY" | "SELL"
    state         : one of GENERAL, FLAGGED_BUY, BUY1_ACTIVE..BUY4_ACTIVE,
                    COOLING_BUY, FLAGGED_SELL, SELL1_ACTIVE..SELL4_ACTIVE,
                    COOLING_SELL
    entry_level   : 0 (no active level), 1..4 (current active level)
    entry_price   : price at which current active level fired (target reference)
    entry_time    : candle-close time of current active level
    levels        : history list of {level, price, time}
    flagged_at    : when stock entered FLAGGED_BUY / FLAGGED_SELL
    current_price : updated on every tick
    """

    __slots__ = (
        "symbol", "plain_name", "company_name",
        "side", "state",
        "entry_level", "entry_price", "entry_time",
        "levels",
        "flagged_at", "flagged_rsi",
        "current_price",
    )

    def __init__(self, symbol: str, plain_name: str, company_name: str = ""):
        self.symbol       = symbol
        self.plain_name   = plain_name
        self.company_name = company_name
        self.side         = None
        self.state        = "GENERAL"
        self.entry_level  = 0
        self.entry_price  = None
        self.entry_time   = None
        self.levels       = []
        self.flagged_at   = None
        self.flagged_rsi  = None
        self.current_price = None

    def reset(self):
        self.side         = None
        self.state        = "GENERAL"
        self.entry_level  = 0
        self.entry_price  = None
        self.entry_time   = None
        self.levels       = []
        self.flagged_at   = None
        self.flagged_rsi  = None

    def to_dict(self) -> dict:
        return {
            "symbol":        self.symbol,
            "plain_name":    self.plain_name,
            "company_name":  self.company_name,
            "side":          self.side,
            "state":         self.state,
            "entry_level":   self.entry_level,
            "entry_price":   self.entry_price,
            "entry_time":    _fmt_dt(self.entry_time),
            "levels":        [
                {"level": l.get("level"),
                 "price": l.get("price"),
                 "time":  _fmt_dt(l.get("time")) if hasattr(l.get("time"), "isoformat") else l.get("time")}
                for l in self.levels
            ],
            "flagged_at":    _fmt_dt(self.flagged_at),
            "flagged_rsi":   self.flagged_rsi,
            "current_price": self.current_price,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "S4Record":
        rec = cls(d["symbol"], d.get("plain_name", ""), d.get("company_name", ""))
        rec.side          = d.get("side")
        rec.state         = d.get("state", "GENERAL")
        rec.entry_level   = d.get("entry_level", 0)
        rec.entry_price   = d.get("entry_price")
        rec.entry_time    = _parse_dt(d.get("entry_time"))
        rec.levels = []
        for l in d.get("levels", []):
            rec.levels.append({
                "level": l.get("level"),
                "price": l.get("price"),
                "time":  _parse_dt(l.get("time")) if isinstance(l.get("time"), str) else l.get("time"),
            })
        rec.flagged_at    = _parse_dt(d.get("flagged_at"))
        rec.flagged_rsi   = d.get("flagged_rsi")
        rec.current_price = d.get("current_price")
        return rec


# ─── S4StateStore ───────────────────────────────────────────────────────────────
class S4StateStore:

    def __init__(self, state_file: str):
        self._file    = os.path.join(ROOT, state_file)
        self._records: dict[str, S4Record] = {}
        self._log: list[dict] = []
        self._lock    = threading.Lock()
        self._load()

    def get_or_create(self, symbol: str, plain_name: str,
                      company_name: str = "") -> S4Record:
        with self._lock:
            if symbol not in self._records:
                self._records[symbol] = S4Record(symbol, plain_name, company_name)
            return self._records[symbol]

    def get(self, symbol: str):
        return self._records.get(symbol)

    def all_records(self) -> list:
        return list(self._records.values())

    def get_buy_active(self) -> list:
        return [r for r in self._records.values()
                if r.state in ("BUY1_ACTIVE", "BUY2_ACTIVE",
                               "BUY3_ACTIVE", "BUY4_ACTIVE")]

    def get_sell_active(self) -> list:
        return [r for r in self._records.values()
                if r.state in ("SELL1_ACTIVE", "SELL2_ACTIVE",
                               "SELL3_ACTIVE", "SELL4_ACTIVE")]

    def get_flagged(self) -> list:
        return [r for r in self._records.values()
                if r.state in ("FLAGGED_BUY", "FLAGGED_SELL")]

    def get_cooling(self) -> list:
        return [r for r in self._records.values()
                if r.state in ("COOLING_BUY", "COOLING_SELL",
                               "COOLING_BUY_STOPLOSS", "COOLING_SELL_STOPLOSS")]

    def get_stoploss_cooling(self) -> list:
        """Stocks that exited via daily-RSI stoploss and are waiting for a
        more demanding RSI recovery before returning to GENERAL."""
        return [r for r in self._records.values()
                if r.state in ("COOLING_BUY_STOPLOSS", "COOLING_SELL_STOPLOSS")]

    def log_signal(self, entry: dict):
        with self._lock:
            self._log.append(entry)
            if len(self._log) > 1000:
                self._log = self._log[-1000:]

    def signal_log(self) -> list:
        return list(self._log)

    def save(self):
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        with self._lock:
            records_to_save = {
                sym: rec.to_dict()
                for sym, rec in self._records.items()
                if rec.state != "GENERAL"
            }
            log_snapshot = list(self._log)
        data = {"records": records_to_save, "signal_log": log_snapshot}
        try:
            with open(self._file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"   ⚠️  S4 state save failed: {e}")

    def _load(self):
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r") as f:
                data = json.load(f)
            for sym, d in data.get("records", {}).items():
                self._records[sym] = S4Record.from_dict(d)
            self._log = data.get("signal_log", [])
            print(f"✅ [scanner_4] State loaded: {len(self._records)} stocks, "
                  f"{len(self._log)} signals")
        except Exception as e:
            print(f"⚠️  S4 state load error: {e}")


# ─── S4WebhookSender ────────────────────────────────────────────────────────────
class S4WebhookSender:
    """
    Single BUY URL for all buy-side signals (Buy 1..4, Exit Buy, Exit Buy Daily RSI)
    Single SELL URL for all sell-side signals (Sell 1..4, Exit Sell, Exit Sell Daily RSI)
    URLs are re-read from config.json on every send so dashboard edits take effect live.
    """

    SCANNER_ID = "scanner_4"

    def __init__(self):
        self._lock = threading.Lock()

    def _urls(self) -> dict:
        try:
            with open(os.path.join(ROOT, "config.json"), "r") as f:
                cfg = json.load(f)
            return cfg.get("scanners", {}).get(self.SCANNER_ID, {}).get("webhooks", {})
        except Exception:
            return {}

    def _send(self, url: str, payload: dict, label: str, retries: int = 3):
        if not url or not url.strip():
            return
        headers = {"Content-Type": "application/json"}
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(url, data=json.dumps(payload),
                                  headers=headers, timeout=10)
                if r.status_code in (200, 201, 202, 204):
                    print(f"  📡 [scanner_4] {label} → [{r.status_code}]")
                    return
            except Exception as e:
                print(f"  ⚠️  [scanner_4] {label} attempt {attempt}: {e}")
            if attempt < retries:
                time.sleep(2)

    # Mapping: category → config key for the destination URL
    _URL_KEYS = {
        "BUY_ENTRY":  "buy_webhook_url",
        "BUY_EXIT":   "buy_exit_webhook_url",
        "SELL_ENTRY": "sell_webhook_url",
        "SELL_EXIT":  "sell_exit_webhook_url",
    }

    def send(self, category: str, payload: dict, label: str):
        """
        category: one of BUY_ENTRY, BUY_EXIT, SELL_ENTRY, SELL_EXIT
                  — routes to the matching URL in config.scanners.scanner_4.webhooks.
        """
        urls = self._urls()
        key  = self._URL_KEYS.get(category)
        if key is None:
            print(f"  ⚠️  [scanner_4] unknown webhook category: {category}")
            return
        url  = urls.get(key, "")
        threading.Thread(
            target=self._send,
            args=(url, payload, label),
            daemon=True
        ).start()


# ─── Strategy4Momentum ──────────────────────────────────────────────────────────
class Strategy4Momentum:

    SCANNER_ID = "scanner_4"

    def __init__(self, config: dict, state_store: S4StateStore,
                 webhook_sender: S4WebhookSender, rsi_engine, mapper,
                 stock_groups=None, buy_stock_group: str = None,
                 sell_stock_group: str = None):
        self.config         = config
        self.state_store    = state_store
        self.webhook_sender = webhook_sender
        self.rsi_engine     = rsi_engine
        self.mapper         = mapper
        self.stock_groups    = stock_groups
        self.buy_stock_group  = buy_stock_group
        self.sell_stock_group = sell_stock_group
        self._lock          = threading.Lock()
        self._exit_thread   = None
        self._stop_event    = threading.Event()
        self._last_exit_check_date = None
        # When True, _fire_* methods skip webhook sends — used during
        # carry-forward replay so we restore state without re-firing signals
        # that have already passed their moment.
        self._silent        = False
        # When True, suppress per-signal print spam (set by carry-forward)
        self._quiet         = False

    def update_config(self, new_config: dict):
        # Replace, not merge, so settings removed on disk are removed here too.
        self.config = dict(new_config)

    # ─── Helpers ────────────────────────────────────────────────────────────
    def _signal_tf(self) -> str:
        return str(self.config.get("signal_timeframe", "15"))

    def _target_pct_for_level(self, level: int) -> float:
        if level == 1:
            return float(self.config.get("buy1_target_pct", 5.0))
        return float(self.config.get("buy23_target_pct", 6.0))

    def _sell_target_pct_for_level(self, level: int) -> float:
        if level == 1:
            return float(self.config.get("sell1_target_pct", 5.0))
        return float(self.config.get("sell23_target_pct", 6.0))

    def _state_label(self, state: str) -> str:
        mapping = {
            "BUY1_ACTIVE":  "Buy 1",
            "BUY2_ACTIVE":  "Buy 2",
            "BUY3_ACTIVE":  "Buy 3",
            "BUY4_ACTIVE":  "Buy 4",
            "SELL1_ACTIVE": "Sell 1",
            "SELL2_ACTIVE": "Sell 2",
            "SELL3_ACTIVE": "Sell 3",
            "SELL4_ACTIVE": "Sell 4",
            "FLAGGED_BUY":  "Flagged (Buy)",
            "FLAGGED_SELL": "Flagged (Sell)",
            "COOLING_BUY":           "Cooling (Buy)",
            "COOLING_SELL":          "Cooling (Sell)",
            "COOLING_BUY_STOPLOSS":  "Cooling (Buy — Stoploss)",
            "COOLING_SELL_STOPLOSS": "Cooling (Sell — Stoploss)",
            "GENERAL":      "General",
        }
        return mapping.get(state, state)

    # ─── Main candle entry point ────────────────────────────────────────────
    def on_candle_close(self, candle, timeframe: str):
        symbol = candle.symbol
        close  = candle.close
        when   = candle.close_time

        # Update RSI in our own engine for all timeframes we care about
        if timeframe in (self._signal_tf(), "D", "W"):
            self.rsi_engine.update(symbol, timeframe, close)

        # Signals fire on the configured signal timeframe only
        if timeframe != self._signal_tf():
            return

        plain   = self.mapper.get_plain_name(symbol)
        company = self.mapper.get_company_name(plain)
        rec     = self.state_store.get_or_create(symbol, plain, company)
        rec.current_price = close

        rsi   = self.rsi_engine.get_rsi(symbol, self._signal_tf())
        d_rsi = self.rsi_engine.get_rsi(symbol, "D")
        w_rsi = self.rsi_engine.get_rsi(symbol, "W")
        if rsi is None:
            return

        with self._lock:
            self._process(rec, close, when, rsi, d_rsi, w_rsi)

    def _process(self, rec: S4Record, price: float, when, rsi: float,
                 d_rsi, w_rsi):
        st = rec.state

        if st == "GENERAL":
            self._check_general(rec, price, when, rsi, d_rsi, w_rsi)
        elif st == "FLAGGED_BUY":
            self._check_flagged_buy(rec, price, when, rsi, d_rsi, w_rsi)
        elif st == "FLAGGED_SELL":
            self._check_flagged_sell(rec, price, when, rsi, d_rsi, w_rsi)
        elif st in ("BUY1_ACTIVE", "BUY2_ACTIVE", "BUY3_ACTIVE", "BUY4_ACTIVE"):
            self._check_buy_active(rec, price, when, rsi, d_rsi, w_rsi)
        elif st in ("SELL1_ACTIVE", "SELL2_ACTIVE", "SELL3_ACTIVE", "SELL4_ACTIVE"):
            self._check_sell_active(rec, price, when, rsi, d_rsi, w_rsi)
        elif st == "COOLING_BUY":
            self._check_cooling_buy(rec, rsi)
        elif st == "COOLING_SELL":
            self._check_cooling_sell(rec, rsi)
        elif st == "COOLING_BUY_STOPLOSS":
            self._check_cooling_buy_stoploss(rec, rsi)
        elif st == "COOLING_SELL_STOPLOSS":
            self._check_cooling_sell_stoploss(rec, rsi)

    # ─── GENERAL: try to enter buy or sell side ─────────────────────────────
    def _check_general(self, rec, price, when, rsi, d_rsi, w_rsi):
        cfg = self.config

        # Stock-group filtering: only evaluate buy or sell side if the symbol
        # is part of the configured group for that side. If stock_groups isn't
        # wired up (older startup path), both sides evaluate as before.
        eval_buy  = True
        eval_sell = True
        if self.stock_groups is not None:
            if self.buy_stock_group:
                eval_buy  = self.stock_groups.in_group(rec.symbol, self.buy_stock_group)
            if self.sell_stock_group:
                eval_sell = self.stock_groups.in_group(rec.symbol, self.sell_stock_group)
        if not (eval_buy or eval_sell):
            return

        b1_lo  = float(cfg.get("buy1_rsi_lower", 68))
        b1_hi  = float(cfg.get("buy1_rsi_upper", 75))
        d_buy  = float(cfg.get("daily_rsi_buy_filter", 65))
        w_buy  = float(cfg.get("weekly_rsi_buy_filter", 65))

        s1_hi  = float(cfg.get("sell1_rsi_upper", 32))
        s1_lo  = float(cfg.get("sell1_rsi_lower", 25))
        d_sell = float(cfg.get("daily_rsi_sell_filter", 40))
        w_sell = float(cfg.get("weekly_rsi_sell_filter", 40))

        # ── BUY side ────────────────────────────────────────────────
        if not eval_buy:
            pass   # skip the buy block entirely
        # Path A: direct entry — RSI in (b1_lo, b1_hi)
        elif b1_lo < rsi < b1_hi:
            if d_rsi is not None and w_rsi is not None and \
               d_rsi > d_buy and w_rsi > w_buy:
                self._fire_buy(rec, level=1, price=price, when=when,
                               rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)
                return

        # Path B: delayed flag — RSI > b1_hi
        if eval_buy and rsi > b1_hi:
            if d_rsi is not None and w_rsi is not None and \
               d_rsi > d_buy and w_rsi > w_buy:
                rec.side         = "BUY"
                rec.state        = "FLAGGED_BUY"
                rec.flagged_at   = when
                rec.flagged_rsi  = rsi
                self.state_store.log_signal({
                    "type":       "FLAGGED_BUY",
                    "stock":      rec.plain_name,
                    "symbol":     rec.symbol,
                    "label":      f"{rec.plain_name} (Flagged Buy)",
                    "price":      price,
                    "rsi_15m":    self._round_or_none(rsi),
                    "daily_rsi":  self._round_or_none(d_rsi),
                    "weekly_rsi": self._round_or_none(w_rsi),
                    "time":       _fmt_dt(when),
                    "state":      "FLAGGED_BUY",
                })
                self.state_store.save()
                if not self._quiet:
                    print(f"  🚩 [scanner_4] {rec.plain_name} FLAGGED BUY "
                          f"| RSI={rsi:.2f} @ ₹{price}")
                return

        # ── SELL side ───────────────────────────────────────────────
        # Path A: direct entry — RSI in (s1_lo, s1_hi)
        if eval_sell and s1_lo < rsi < s1_hi:
            if d_rsi is not None and w_rsi is not None and \
               d_rsi < d_sell and w_rsi < w_sell:
                self._fire_sell(rec, level=1, price=price, when=when,
                                rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)
                return

        # Path B: delayed flag — RSI < s1_lo
        if eval_sell and rsi < s1_lo:
            if d_rsi is not None and w_rsi is not None and \
               d_rsi < d_sell and w_rsi < w_sell:
                rec.side         = "SELL"
                rec.state        = "FLAGGED_SELL"
                rec.flagged_at   = when
                rec.flagged_rsi  = rsi
                self.state_store.log_signal({
                    "type":       "FLAGGED_SELL",
                    "stock":      rec.plain_name,
                    "symbol":     rec.symbol,
                    "label":      f"{rec.plain_name} (Flagged Sell)",
                    "price":      price,
                    "rsi_15m":    self._round_or_none(rsi),
                    "daily_rsi":  self._round_or_none(d_rsi),
                    "weekly_rsi": self._round_or_none(w_rsi),
                    "time":       _fmt_dt(when),
                    "state":      "FLAGGED_SELL",
                })
                self.state_store.save()
                if not self._quiet:
                    print(f"  🚩 [scanner_4] {rec.plain_name} FLAGGED SELL "
                          f"| RSI={rsi:.2f} @ ₹{price}")
                return

    # ─── FLAGGED_BUY: wait for cooldown ─────────────────────────────────────
    def _check_flagged_buy(self, rec, price, when, rsi, d_rsi, w_rsi):
        cd     = float(self.config.get("buy1_cooldown_rsi", 60))
        d_buy  = float(self.config.get("daily_rsi_buy_filter", 65))
        w_buy  = float(self.config.get("weekly_rsi_buy_filter", 65))

        if rsi < cd:
            if d_rsi is not None and w_rsi is not None and \
               d_rsi > d_buy and w_rsi > w_buy:
                self._fire_buy(rec, level=1, price=price, when=when,
                               rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)
        # else: keep waiting

    # ─── FLAGGED_SELL ──────────────────────────────────────────────────────
    def _check_flagged_sell(self, rec, price, when, rsi, d_rsi, w_rsi):
        cd     = float(self.config.get("sell1_cooldown_rsi", 40))
        d_sell = float(self.config.get("daily_rsi_sell_filter", 40))
        w_sell = float(self.config.get("weekly_rsi_sell_filter", 40))

        if rsi > cd:
            if d_rsi is not None and w_rsi is not None and \
               d_rsi < d_sell and w_rsi < w_sell:
                self._fire_sell(rec, level=1, price=price, when=when,
                                rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)

    # ─── Live daily-RSI helper ──────────────────────────────────────────────
    # Apply ONE Wilder step to yesterday's D state using current intraday
    # close as today's would-be daily close. Mirrors the same calculation
    # TradingView uses for the unsmoothed live bar. Used for intra-day
    # stoploss reactivity without per-tick noise.
    def _live_daily_rsi(self, symbol, current_close):
        d_calcs = self.rsi_engine._calculators.get(symbol)
        if not d_calcs:
            return None
        calc = d_calcs.get("D")
        if calc is None or not calc.initialized or calc.rsi is None:
            return None
        closes = list(calc.closes)
        if not closes:
            return None
        diff = current_close - closes[-1]
        gain = max(0.0,  diff)
        loss = max(0.0, -diff)
        p   = calc.period
        ag  = (calc.avg_gain * (p - 1) + gain) / p
        al  = (calc.avg_loss * (p - 1) + loss) / p
        if al == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    # ─── BUY active states: target → next level → idle ─────────────────────
    def _check_buy_active(self, rec, price, when, rsi, d_rsi, w_rsi):
        cfg   = self.config
        level = rec.entry_level
        entry = rec.entry_price or price

        # Daily-RSI stoploss — uses LIVE daily-RSI (one Wilder step from
        # current close) so the check is reactive intraday on signal-TF
        # candle closes, not delayed until tomorrow morning. Falls back to
        # the engine's last completed D-RSI if live can't be computed.
        live_d = self._live_daily_rsi(rec.symbol, price)
        sl_d   = live_d if live_d is not None else d_rsi
        sl_buy = float(cfg.get("daily_rsi_stoploss_buy", 48))
        if sl_d is not None and sl_d < sl_buy:
            target_pct   = self._target_pct_for_level(level)
            target_price = entry * (1.0 + target_pct / 100.0)
            self._fire_exit_buy(rec, price=price, when=when,
                                target_price=target_price,
                                reason="daily_rsi_stoploss",
                                rsi_15m=rsi, d_rsi=sl_d, w_rsi=w_rsi)
            return

        # Target check (always first after stoploss)
        target_pct  = self._target_pct_for_level(level)
        target_price = entry * (1.0 + target_pct / 100.0)
        if price >= target_price:
            self._fire_exit_buy(rec, price=price, when=when,
                                target_price=target_price, reason="target",
                                rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)
            return

        # Next level trigger
        if level >= 4:
            return   # no Buy 5

        d_buy = float(cfg.get("daily_rsi_buy_filter", 65))
        # BUY 2 trigger: RSI < buy2_rsi_trigger
        if level == 1:
            trig = float(cfg.get("buy2_rsi_trigger", 30))
            if rsi < trig:
                if d_rsi is not None and d_rsi > d_buy:
                    self._fire_buy(rec, level=2, price=price, when=when,
                                   rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)
            return

        # BUY 3 / BUY 4 trigger: drop from previous buy by buy3_drop_pct
        drop_pct = float(cfg.get("buy3_drop_pct", 3.0))
        drop     = ((entry - price) / entry) * 100.0 if entry else 0.0
        if drop >= drop_pct:
            if d_rsi is not None and d_rsi > d_buy:
                self._fire_buy(rec, level=level + 1, price=price, when=when,
                               rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)

    # ─── SELL active states ────────────────────────────────────────────────
    def _check_sell_active(self, rec, price, when, rsi, d_rsi, w_rsi):
        cfg   = self.config
        level = rec.entry_level
        entry = rec.entry_price or price

        # Daily-RSI stoploss (mirror of BUY) — uses LIVE daily-RSI on signal-
        # TF candle closes for intraday reactivity. Falls back to engine D-RSI.
        live_d  = self._live_daily_rsi(rec.symbol, price)
        sl_d    = live_d if live_d is not None else d_rsi
        sl_sell = float(cfg.get("daily_rsi_stoploss_sell", 55))
        if sl_d is not None and sl_d > sl_sell:
            target_pct   = self._sell_target_pct_for_level(level)
            target_price = entry * (1.0 - target_pct / 100.0)
            self._fire_exit_sell(rec, price=price, when=when,
                                 target_price=target_price,
                                 reason="daily_rsi_stoploss",
                                 rsi_15m=rsi, d_rsi=sl_d, w_rsi=w_rsi)
            return

        target_pct   = self._sell_target_pct_for_level(level)
        target_price = entry * (1.0 - target_pct / 100.0)
        if price <= target_price:
            self._fire_exit_sell(rec, price=price, when=when,
                                 target_price=target_price, reason="target",
                                 rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)
            return

        if level >= 4:
            return

        d_sell = float(cfg.get("daily_rsi_sell_filter", 40))
        if level == 1:
            trig = float(cfg.get("sell2_rsi_trigger", 68))
            if rsi > trig:
                if d_rsi is not None and d_rsi < d_sell:
                    self._fire_sell(rec, level=2, price=price, when=when,
                                    rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)
            return

        rise_pct = float(cfg.get("sell3_rise_pct", 3.0))
        rise     = ((price - entry) / entry) * 100.0 if entry else 0.0
        if rise >= rise_pct:
            if d_rsi is not None and d_rsi < d_sell:
                self._fire_sell(rec, level=level + 1, price=price, when=when,
                                rsi_15m=rsi, d_rsi=d_rsi, w_rsi=w_rsi)

    # ─── COOLING ───────────────────────────────────────────────────────────
    def _check_cooling_buy(self, rec, rsi):
        thr = float(self.config.get("cooling_rsi_buy", 40))
        if rsi < thr:
            rec.reset()
            self.state_store.save()
            print(f"  ❄️→🟢 [scanner_4] {rec.plain_name} COOLING_BUY → GENERAL "
                  f"| RSI={rsi:.2f} < {thr}")

    def _check_cooling_sell(self, rec, rsi):
        thr = float(self.config.get("cooling_rsi_sell", 60))
        if rsi > thr:
            rec.reset()
            self.state_store.save()
            print(f"  ❄️→🟢 [scanner_4] {rec.plain_name} COOLING_SELL → GENERAL "
                  f"| RSI={rsi:.2f} > {thr}")

    # ── Stoploss-cooling (tighter exit gate after a daily-RSI stoploss) ─────
    # After a stoploss exit, the stock has to recover MORE before re-entry.
    # For BUY-side stoploss: signal-TF RSI must rise ABOVE stoploss_cooling_rsi_buy
    # (default 68) before going back to GENERAL.
    # Mirror for SELL: RSI must drop BELOW stoploss_cooling_rsi_sell (default 32).
    def _check_cooling_buy_stoploss(self, rec, rsi):
        thr = float(self.config.get("stoploss_cooling_rsi_buy", 68))
        if rsi > thr:
            rec.reset()
            self.state_store.save()
            print(f"  ❄️🛑→🟢 [scanner_4] {rec.plain_name} "
                  f"COOLING_BUY_STOPLOSS → GENERAL | RSI={rsi:.2f} > {thr}")

    def _check_cooling_sell_stoploss(self, rec, rsi):
        thr = float(self.config.get("stoploss_cooling_rsi_sell", 32))
        if rsi < thr:
            rec.reset()
            self.state_store.save()
            print(f"  ❄️🛑→🟢 [scanner_4] {rec.plain_name} "
                  f"COOLING_SELL_STOPLOSS → GENERAL | RSI={rsi:.2f} < {thr}")

    # ─── Fire helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _round_or_none(v):
        return round(v, 2) if v is not None else None

    def _fire_buy(self, rec, level: int, price: float, when,
                  rsi_15m=None, d_rsi=None, w_rsi=None):
        new_state = f"BUY{level}_ACTIVE"
        rec.side        = "BUY"
        rec.state       = new_state
        rec.entry_level = level
        rec.entry_price = price
        rec.entry_time  = when
        rec.flagged_at  = None
        rec.flagged_rsi = None
        rec.levels.append({"level": level, "price": price, "time": when})

        target_pct   = self._target_pct_for_level(level)
        target_price = round(price * (1.0 + target_pct / 100.0), 2)
        label = f"{rec.plain_name} (Buy {level})"

        log_entry = {
            "type":         f"BUY{level}",
            "stock":        rec.plain_name,
            "symbol":       rec.symbol,
            "label":        label,
            "price":        price,
            "target_price": target_price,
            "rsi_15m":      self._round_or_none(rsi_15m),
            "daily_rsi":    self._round_or_none(d_rsi),
            "weekly_rsi":   self._round_or_none(w_rsi),
            "time":         _fmt_dt(when),
            "state":        new_state,
        }
        self.state_store.log_signal(log_entry)
        self.state_store.save()

        if not self._silent:
            payload = {
                "signal":       f"BUY{level}",
                "scanner":      self.SCANNER_ID,
                "label":        label,
                "symbol":       rec.symbol,
                "stock":        rec.plain_name,
                "company":      rec.company_name,
                "price":        price,
                "time":         _fmt_dt(when) or datetime.now(IST).isoformat(),
                "state":        new_state,
                "target_price": target_price,
                "pnl_pct":      0.0,
                "rsi_15m":      self._round_or_none(rsi_15m),
                "daily_rsi":    self._round_or_none(d_rsi),
                "weekly_rsi":   self._round_or_none(w_rsi),
                "source":       "FyersScanner/scanner_4",
            }
            self.webhook_sender.send("BUY_ENTRY", payload, label)
        if not self._quiet:
            print(f"  🟢 [scanner_4] {label} @ ₹{price} | target ₹{target_price}")

    def _fire_sell(self, rec, level: int, price: float, when,
                   rsi_15m=None, d_rsi=None, w_rsi=None):
        new_state = f"SELL{level}_ACTIVE"
        rec.side        = "SELL"
        rec.state       = new_state
        rec.entry_level = level
        rec.entry_price = price
        rec.entry_time  = when
        rec.flagged_at  = None
        rec.flagged_rsi = None
        rec.levels.append({"level": level, "price": price, "time": when})

        target_pct   = self._sell_target_pct_for_level(level)
        target_price = round(price * (1.0 - target_pct / 100.0), 2)
        label = f"{rec.plain_name} (Sell {level})"

        log_entry = {
            "type":         f"SELL{level}",
            "stock":        rec.plain_name,
            "symbol":       rec.symbol,
            "label":        label,
            "price":        price,
            "target_price": target_price,
            "rsi_15m":      self._round_or_none(rsi_15m),
            "daily_rsi":    self._round_or_none(d_rsi),
            "weekly_rsi":   self._round_or_none(w_rsi),
            "time":         _fmt_dt(when),
            "state":        new_state,
        }
        self.state_store.log_signal(log_entry)
        self.state_store.save()

        if not self._silent:
            payload = {
                "signal":       f"SELL{level}",
                "scanner":      self.SCANNER_ID,
                "label":        label,
                "symbol":       rec.symbol,
                "stock":        rec.plain_name,
                "company":      rec.company_name,
                "price":        price,
                "time":         _fmt_dt(when) or datetime.now(IST).isoformat(),
                "state":        new_state,
                "target_price": target_price,
                "pnl_pct":      0.0,
                "rsi_15m":      self._round_or_none(rsi_15m),
                "daily_rsi":    self._round_or_none(d_rsi),
                "weekly_rsi":   self._round_or_none(w_rsi),
                "source":       "FyersScanner/scanner_4",
            }
            self.webhook_sender.send("SELL_ENTRY", payload, label)
        if not self._quiet:
            print(f"  🔴 [scanner_4] {label} @ ₹{price} | target ₹{target_price}")

    def _fire_exit_buy(self, rec, price, when, target_price, reason,
                       rsi_15m=None, d_rsi=None, w_rsi=None):
        entry  = rec.entry_price or price
        pnl    = round(((price - entry) / entry) * 100.0, 2) if entry else 0.0
        if reason == "daily_rsi":
            label = f"{rec.plain_name} (Exit Buy — Daily RSI)"
            signal_type = "EXIT_BUY_DAILY_RSI"
        elif reason == "daily_rsi_stoploss":
            label = f"{rec.plain_name} (Exit Buy — Daily RSI Stoploss)"
            signal_type = "EXIT_BUY_STOPLOSS"
        else:
            label = f"{rec.plain_name} (Exit Buy)"
            signal_type = "EXIT_BUY"

        log_entry = {
            "type":         signal_type,
            "stock":        rec.plain_name,
            "symbol":       rec.symbol,
            "label":        label,
            "price":        price,
            "entry_price":  entry,
            "entry_level":  rec.entry_level,
            "target_price": round(target_price, 2),
            "pnl_pct":      pnl,
            "rsi_15m":      self._round_or_none(rsi_15m),
            "daily_rsi":    self._round_or_none(d_rsi),
            "weekly_rsi":   self._round_or_none(w_rsi),
            "time":         _fmt_dt(when),
            "state":        "COOLING_BUY",
        }
        self.state_store.log_signal(log_entry)

        if not self._silent:
            payload = {
                "signal":       signal_type,
                "scanner":      self.SCANNER_ID,
                "label":        label,
                "symbol":       rec.symbol,
                "stock":        rec.plain_name,
                "company":      rec.company_name,
                "price":        price,
                "time":         _fmt_dt(when) or datetime.now(IST).isoformat(),
                "state":        "COOLING_BUY",
                "target_price": round(target_price, 2),
                "pnl_pct":      pnl,
                "rsi_15m":      self._round_or_none(rsi_15m),
                "daily_rsi":    self._round_or_none(d_rsi),
                "weekly_rsi":   self._round_or_none(w_rsi),
                "source":       "FyersScanner/scanner_4",
            }
            self.webhook_sender.send("BUY_EXIT", payload, label)

        # Stoploss-exits get a stricter cooling gate (must rally to a higher
        # RSI before being re-eligible). All other buy exits use the regular
        # cooling state.
        rec.state = ("COOLING_BUY_STOPLOSS"
                     if reason == "daily_rsi_stoploss" else "COOLING_BUY")
        self.state_store.save()
        if not self._quiet:
            print(f"  🏁 [scanner_4] {label} @ ₹{price} | P&L {pnl:+.2f}%")

    def _fire_exit_sell(self, rec, price, when, target_price, reason,
                        rsi_15m=None, d_rsi=None, w_rsi=None):
        entry  = rec.entry_price or price
        pnl    = round(((entry - price) / entry) * 100.0, 2) if entry else 0.0
        if reason == "daily_rsi":
            label = f"{rec.plain_name} (Exit Sell — Daily RSI)"
            signal_type = "EXIT_SELL_DAILY_RSI"
        elif reason == "daily_rsi_stoploss":
            label = f"{rec.plain_name} (Exit Sell — Daily RSI Stoploss)"
            signal_type = "EXIT_SELL_STOPLOSS"
        else:
            label = f"{rec.plain_name} (Exit Sell)"
            signal_type = "EXIT_SELL"

        log_entry = {
            "type":         signal_type,
            "stock":        rec.plain_name,
            "symbol":       rec.symbol,
            "label":        label,
            "price":        price,
            "entry_price":  entry,
            "entry_level":  rec.entry_level,
            "target_price": round(target_price, 2),
            "pnl_pct":      pnl,
            "rsi_15m":      self._round_or_none(rsi_15m),
            "daily_rsi":    self._round_or_none(d_rsi),
            "weekly_rsi":   self._round_or_none(w_rsi),
            "time":         _fmt_dt(when),
            "state":        "COOLING_SELL",
        }
        self.state_store.log_signal(log_entry)

        if not self._silent:
            payload = {
                "signal":       signal_type,
                "scanner":      self.SCANNER_ID,
                "label":        label,
                "symbol":       rec.symbol,
                "stock":        rec.plain_name,
                "company":      rec.company_name,
                "price":        price,
                "time":         _fmt_dt(when) or datetime.now(IST).isoformat(),
                "state":        "COOLING_SELL",
                "target_price": round(target_price, 2),
                "pnl_pct":      pnl,
                "rsi_15m":      self._round_or_none(rsi_15m),
                "daily_rsi":    self._round_or_none(d_rsi),
                "weekly_rsi":   self._round_or_none(w_rsi),
                "source":       "FyersScanner/scanner_4",
            }
            self.webhook_sender.send("SELL_EXIT", payload, label)

        rec.state = ("COOLING_SELL_STOPLOSS"
                     if reason == "daily_rsi_stoploss" else "COOLING_SELL")
        self.state_store.save()
        if not self._quiet:
            print(f"  🏁 [scanner_4] {label} @ ₹{price} | P&L {pnl:+.2f}%")

    # ─── Tick handler — display update only ─────────────────────────────────
    # Stoploss check moved to on_candle_close (5m/signal-TF candles) so that
    # intra-candle wicks that recover before close don't trigger false exits.
    # This also makes live behavior bit-identical to historical replay.
    _BUY_ACTIVE_STATES  = ("BUY1_ACTIVE",  "BUY2_ACTIVE",
                           "BUY3_ACTIVE",  "BUY4_ACTIVE")
    _SELL_ACTIVE_STATES = ("SELL1_ACTIVE", "SELL2_ACTIVE",
                           "SELL3_ACTIVE", "SELL4_ACTIVE")

    def on_tick(self, symbol: str, price: float):
        rec = self.state_store.get(symbol)
        if not rec or rec.state == "GENERAL":
            return
        rec.current_price = price

    # ─── Daily-RSI exit override (runs once per day at exit_check_time) ────
    def run_daily_exit_check(self, when=None):
        when = when or datetime.now(IST)
        d_exit_buy  = float(self.config.get("daily_rsi_exit",       50))
        d_exit_sell = float(self.config.get("daily_rsi_exit_sell",  50))

        sig_tf = self._signal_tf()

        # BUY side: daily RSI < threshold → exit
        for rec in list(self.state_store.get_buy_active()):
            d_rsi = self.rsi_engine.get_rsi(rec.symbol, "D")
            if d_rsi is None:
                continue
            if d_rsi < d_exit_buy:
                price        = rec.current_price or rec.entry_price or 0.0
                target_pct   = self._target_pct_for_level(rec.entry_level)
                target_price = (rec.entry_price or price) * (1.0 + target_pct / 100.0)
                rsi_15m      = self.rsi_engine.get_rsi(rec.symbol, sig_tf)
                w_rsi        = self.rsi_engine.get_rsi(rec.symbol, "W")
                with self._lock:
                    self._fire_exit_buy(rec, price=price, when=when,
                                        target_price=target_price,
                                        reason="daily_rsi",
                                        rsi_15m=rsi_15m, d_rsi=d_rsi, w_rsi=w_rsi)

        # SELL side: daily RSI > threshold → exit
        for rec in list(self.state_store.get_sell_active()):
            d_rsi = self.rsi_engine.get_rsi(rec.symbol, "D")
            if d_rsi is None:
                continue
            if d_rsi > d_exit_sell:
                price        = rec.current_price or rec.entry_price or 0.0
                target_pct   = self._sell_target_pct_for_level(rec.entry_level)
                target_price = (rec.entry_price or price) * (1.0 - target_pct / 100.0)
                rsi_15m      = self.rsi_engine.get_rsi(rec.symbol, sig_tf)
                w_rsi        = self.rsi_engine.get_rsi(rec.symbol, "W")
                with self._lock:
                    self._fire_exit_sell(rec, price=price, when=when,
                                         target_price=target_price,
                                         reason="daily_rsi",
                                         rsi_15m=rsi_15m, d_rsi=d_rsi, w_rsi=w_rsi)

    # ─── Exit-check thread ─────────────────────────────────────────────────
    def _exit_check_loop(self):
        while not self._stop_event.is_set():
            try:
                now    = datetime.now(IST)
                target = self.config.get("exit_check_time", "15:25")
                hh, mm = (int(x) for x in str(target).split(":"))
                today  = now.date()
                if now.weekday() <= 4 \
                   and (now.hour, now.minute) >= (hh, mm) \
                   and self._last_exit_check_date != today:
                    print(f"\n⏰ [scanner_4] Daily RSI exit check @ "
                          f"{now.strftime('%H:%M')}")
                    self.run_daily_exit_check(when=now)
                    self._last_exit_check_date = today
            except Exception as e:
                print(f"  ⚠️  [scanner_4] exit-check error: {e}")
            self._stop_event.wait(30)

    # ─── Fresh RSI seeding from CSV (avoids cache-contamination bug) ───────
    def fresh_seed_from_csv(self, data_store, all_symbols, before_time=None):
        """
        Re-seed the RSI engine using only candles STRICTLY BEFORE `before_time`
        (default = today 09:15 IST).

        Why: rsi_cache.pkl is rebuilt at 15:31 every trading day.  After that,
        loading the cache gives an engine state that already includes today's
        candles.  If we then carry-forward by replaying today's candles, we
        double-count and get garbage RSI values.  Fresh-seeding from CSV with
        pre-today candles only guarantees the engine reflects "yesterday's
        last 15m close", so replay produces the same RSI sequence the live
        scanner would have seen running candle-by-candle since open.
        """
        if before_time is None:
            now         = datetime.now(IST)
            before_time = now.replace(hour=9, minute=15,
                                      second=0, microsecond=0)
        if before_time.tzinfo is None:
            before_time = IST.localize(before_time)

        sig_tf = self._signal_tf()
        seeded = 0
        for sym in all_symbols:
            # ── Signal timeframe (e.g. 15m) ───────────────────────────────
            try:
                candles    = data_store.load_candles(sym, sig_tf,
                                                      to_date=before_time)
                pre_closes = [c["close"] for c in candles
                              if c["datetime"] < before_time]
                if len(pre_closes) > 14:
                    # Last 200 closes is well past Wilder's smoothing convergence
                    self.rsi_engine.seed(sym, sig_tf, pre_closes[-200:])
                    seeded += 1
            except Exception:
                pass

            # ── Daily RSI (from 5m aggregation, matches Fyers chart) ──────
            try:
                d_candles = data_store.load_daily_from_5m(sym)
                d_closes  = [c["close"] for c in d_candles
                             if c["datetime"].date() < before_time.date()]
                if len(d_closes) > 14:
                    self.rsi_engine.seed(sym, "D", d_closes[-200:])
            except Exception:
                pass

            # ── Weekly RSI ─────────────────────────────────────────────────
            try:
                w_candles = data_store.load_candles(sym, "W")
                w_closes  = [c["close"] for c in w_candles
                             if c["datetime"].date() < before_time.date()]
                if len(w_closes) > 14:
                    self.rsi_engine.seed(sym, "W", w_closes[-200:])
            except Exception:
                pass

        return seeded

    # ─── Carry-forward replay ──────────────────────────────────────────────
    def run_carry_forward(self, data_store, all_symbols=None,
                          from_time=None, silent=True, quiet=True) -> int:
        """
        Replay signal-TF candles from `from_time` against the state machine,
        with POINT-IN-TIME Daily and Weekly RSI tracking (D advances at end of
        each trading day, W at Friday close) via the shared ReplayEngine.

        For a from_time well before today (e.g. now - 30 days), this restores
        each stock to the state it would have reached if the scanner had been
        running continuously since from_time -- not just today.

        Caller MUST have called fresh_seed_from_csv(before_time=from_time)
        before this. The replay continues from there.

        silent=True suppresses webhook sends. quiet=True suppresses per-signal
        prints. Returns the number of signals fired during replay.
        """
        from core.replay_engine import ReplayEngine

        if from_time is None:
            now       = datetime.now(IST)
            from_time = now.replace(hour=9, minute=15,
                                    second=0, microsecond=0)
        if from_time.tzinfo is None:
            from_time = IST.localize(from_time)

        sig_tf = self._signal_tf()

        symbols = all_symbols or [r.symbol for r in self.state_store.all_records()]
        if not symbols:
            return 0

        signals_before = len(self.state_store.signal_log())
        self._silent   = silent
        self._quiet    = quiet

        replay_eng = ReplayEngine(data_store, rsi_period=14)
        to_dt      = datetime.now(IST)

        def _make_cb(sym, rec):
            def _cb(candle, tf, rsi_engine, d_rsi, w_rsi):
                # Only act on the signal TF -- D/W updates happen internally
                # to advance d_rsi/w_rsi but don't trigger state transitions.
                if tf != sig_tf:
                    return
                close      = candle["close"]
                open_time  = candle["datetime"]
                close_time = open_time + timedelta(minutes=int(sig_tf))
                rsi        = rsi_engine.get_rsi(sym, sig_tf)
                if rsi is None:
                    return
                rec.current_price = close
                with self._lock:
                    self._process(rec, close, close_time, rsi, d_rsi, w_rsi)
            return _cb

        try:
            for sym in symbols:
                plain   = self.mapper.get_plain_name(sym)
                company = self.mapper.get_company_name(plain)
                rec     = self.state_store.get_or_create(sym, plain, company)
                replay_eng.replay(
                    symbol          = sym,
                    tfs             = [sig_tf],
                    from_dt         = from_time,
                    to_dt           = to_dt,
                    callback        = _make_cb(sym, rec),
                    external_engine = self.rsi_engine,
                )
        finally:
            self._silent = False
            self._quiet  = False

        signals_after = len(self.state_store.signal_log())
        self.state_store.save()
        return signals_after - signals_before

    def start_exit_check_thread(self):
        if self._exit_thread and self._exit_thread.is_alive():
            return
        self._exit_thread = threading.Thread(
            target=self._exit_check_loop, daemon=True)
        self._exit_thread.start()

    def stop(self):
        self._stop_event.set()

    def save_state(self):
        self.state_store.save()
