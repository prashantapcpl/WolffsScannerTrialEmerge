"""
strategy3_webhook.py — Webhook Signal Mirror (Scanner 3)

Receives BUY/SELL/EXIT signals from external sources (e.g. Chartink) via a
Flask webhook server.  Applies entry/exit/averaging logic on Fyers live price
data, then fires outgoing webhooks to the user's app.

Completely independent of Scanner 1 and Scanner 2.
"""

import json
import os
import threading
import time
import requests
from datetime import datetime
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


# ─── S3Record ───────────────────────────────────────────────────────────────────
class S3Record:
    """
    State for a single stock in Strategy 3.
    States:
      GENERAL      — no active position
      WATCHED_BUY  — BUY signal received, waiting for drop trigger
      WATCHED_SELL — SELL signal received, waiting for rise trigger
      ACTIVE_BUY   — long position entered, monitoring for avg / exit signal
      ACTIVE_SELL  — short position entered, monitoring for avg / exit signal
      EXITING_BUY  — EXIT-BUY signal received, waiting for rise above exit_ref
      EXITING_SELL — EXIT-SELL signal received, waiting for drop below exit_ref
    """

    __slots__ = (
        "symbol", "plain_name", "company_name",
        "side", "state",
        "ref_price", "ref_time",
        "entry_price", "entry_time",
        "last_avg_price", "avg_count", "avg_entries",
        "exit_ref_price", "exit_ref_time",
        "current_price",
    )

    def __init__(self, symbol: str, plain_name: str, company_name: str = ""):
        self.symbol       = symbol
        self.plain_name   = plain_name
        self.company_name = company_name
        self.side         = None   # "BUY" | "SELL"
        self.state        = "GENERAL"
        self.ref_price    = None
        self.ref_time     = None
        self.entry_price  = None
        self.entry_time   = None
        self.last_avg_price  = None
        self.avg_count    = 0
        self.avg_entries  = []   # list of dicts
        self.exit_ref_price = None
        self.exit_ref_time  = None
        self.current_price  = None

    def reset(self):
        self.side           = None
        self.state          = "GENERAL"
        self.ref_price      = None
        self.ref_time       = None
        self.entry_price    = None
        self.entry_time     = None
        self.last_avg_price = None
        self.avg_count      = 0
        self.avg_entries    = []
        self.exit_ref_price = None
        self.exit_ref_time  = None

    def to_dict(self) -> dict:
        def _fmt(dt):
            return dt.isoformat() if dt and hasattr(dt, "isoformat") else str(dt) if dt else None

        return {
            "symbol":         self.symbol,
            "plain_name":     self.plain_name,
            "company_name":   self.company_name,
            "side":           self.side,
            "state":          self.state,
            "ref_price":      self.ref_price,
            "ref_time":       _fmt(self.ref_time),
            "entry_price":    self.entry_price,
            "entry_time":     _fmt(self.entry_time),
            "last_avg_price": self.last_avg_price,
            "avg_count":      self.avg_count,
            "avg_entries":    [
                {k: (_fmt(v) if k == "signal_time" else v)
                 for k, v in e.items()}
                for e in self.avg_entries
            ],
            "exit_ref_price": self.exit_ref_price,
            "exit_ref_time":  _fmt(self.exit_ref_time),
            "current_price":  self.current_price,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "S3Record":
        rec = cls(d["symbol"], d.get("plain_name", ""), d.get("company_name", ""))
        rec.side            = d.get("side")
        rec.state           = d.get("state", "GENERAL")
        rec.ref_price       = d.get("ref_price")
        rec.ref_time        = _parse_dt(d.get("ref_time"))
        rec.entry_price     = d.get("entry_price")
        rec.entry_time      = _parse_dt(d.get("entry_time"))
        rec.last_avg_price  = d.get("last_avg_price")
        rec.avg_count       = d.get("avg_count", 0)
        rec.avg_entries     = d.get("avg_entries", [])
        rec.exit_ref_price  = d.get("exit_ref_price")
        rec.exit_ref_time   = _parse_dt(d.get("exit_ref_time"))
        rec.current_price   = d.get("current_price")
        return rec


# ─── S3StateStore ────────────────────────────────────────────────────────────────
class S3StateStore:
    """
    Persistence layer for Strategy 3.  Completely separate from
    scanner_1 / scanner_2 state files.
    """

    def __init__(self, state_file: str):
        self._file    = os.path.join(ROOT, state_file)
        self._records: dict[str, S3Record] = {}
        self._log: list[dict] = []
        self._lock    = threading.Lock()
        self._load()

    # ── record access ────────────────────────────────────────────────────────
    def get_or_create(self, symbol: str, plain_name: str,
                      company_name: str = "") -> S3Record:
        with self._lock:
            if symbol not in self._records:
                self._records[symbol] = S3Record(symbol, plain_name, company_name)
            return self._records[symbol]

    def get(self, symbol: str):
        return self._records.get(symbol)

    def all_active(self) -> list:
        return [r for r in self._records.values() if r.state != "GENERAL"]

    def get_watched(self) -> list:
        return [r for r in self._records.values()
                if r.state in ("WATCHED_BUY", "WATCHED_SELL")]

    def get_active_positions(self) -> list:
        return [r for r in self._records.values()
                if r.state in ("ACTIVE_BUY", "ACTIVE_SELL",
                               "EXITING_BUY", "EXITING_SELL")]

    # ── signal log ───────────────────────────────────────────────────────────
    def log_signal(self, entry: dict):
        with self._lock:
            self._log.append(entry)
            if len(self._log) > 500:
                self._log = self._log[-500:]

    # ── persistence ──────────────────────────────────────────────────────────
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
            print(f"   ⚠️  S3 state save failed: {e}")

    def _load(self):
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r") as f:
                data = json.load(f)
            for sym, d in data.get("records", {}).items():
                self._records[sym] = S3Record.from_dict(d)
            self._log = data.get("signal_log", [])
            active = len(self.all_active())
            print(f"   ✅ S3 state loaded: {active} active positions")
        except Exception as e:
            print(f"   ⚠️  S3 state load failed: {e}")


# ─── S3WebhookSender ────────────────────────────────────────────────────────────
class S3WebhookSender:
    """Fires outgoing webhooks for Strategy 3 signals."""

    def __init__(self, scanner_id: str = "scanner_3"):
        self.scanner_id = scanner_id

    def _load_urls(self) -> dict:
        try:
            config_path = os.path.join(ROOT, "config.json")
            with open(config_path, "r") as f:
                cfg = json.load(f)
            return (cfg.get("scanners", {})
                       .get(self.scanner_id, {})
                       .get("outgoing_webhooks", {}))
        except Exception:
            return {}

    def _post(self, url: str, payload: dict, label: str):
        if not url or not url.strip():
            return
        def _do():
            for attempt in range(1, 4):
                try:
                    r = requests.post(url, json=payload, timeout=10)
                    if r.status_code in (200, 201, 202, 204):
                        print(f"  📡 [s3] {label} → [{r.status_code}]")
                        return
                except Exception as e:
                    print(f"  ⚠️  [s3] {label} attempt {attempt}: {e}")
                if attempt < 3:
                    time.sleep(2)
        threading.Thread(target=_do, daemon=True).start()

    def send_entry(self, side: str, symbol: str, plain_name: str,
                   company_name: str, entry_price: float, ref_price: float,
                   change_pct: float, now: datetime):
        urls = self._load_urls()
        url  = urls.get("buy_url" if side == "BUY" else "sell_url", "")
        self._post(url, {
            "signal":      side,
            "scanner":     self.scanner_id,
            "label":       f"{plain_name} ({side})",
            "symbol":      symbol,
            "stock":       plain_name,
            "company":     company_name,
            "entry_price": entry_price,
            "ref_price":   ref_price,
            "change_pct":  change_pct,
            "signal_time": now.isoformat() if now else datetime.now(IST).isoformat(),
            "sent_time":   datetime.now(IST).isoformat(),
            "source":      f"FyersScanner/{self.scanner_id}",
        }, side)

    def send_avg(self, side: str, symbol: str, plain_name: str,
                 company_name: str, avg_price: float, prev_price: float,
                 change_pct: float, avg_num: int, now: datetime):
        urls  = self._load_urls()
        url   = urls.get("buy_url" if side == "BUY" else "sell_url", "")
        label = f"Avg {avg_num}" if side == "BUY" else f"Avg Short {avg_num}"
        self._post(url, {
            "signal":     f"AVG_{side}",
            "scanner":    self.scanner_id,
            "label":      f"{plain_name} ({label})",
            "symbol":     symbol,
            "stock":      plain_name,
            "company":    company_name,
            "avg_price":  avg_price,
            "prev_price": prev_price,
            "change_pct": change_pct,
            "avg_number": avg_num,
            "signal_time":now.isoformat() if now else datetime.now(IST).isoformat(),
            "sent_time":  datetime.now(IST).isoformat(),
            "source":     f"FyersScanner/{self.scanner_id}",
        }, label)

    def send_exit(self, side: str, symbol: str, plain_name: str,
                  company_name: str, exit_price: float, entry_price: float,
                  pnl_pct: float, avg_count: int, now: datetime):
        urls  = self._load_urls()
        url   = urls.get("exit_buy_url" if side == "BUY" else "exit_sell_url", "")
        label = f"EXIT-{side}"
        self._post(url, {
            "signal":      f"EXIT_{side}",
            "scanner":     self.scanner_id,
            "label":       f"{plain_name} ({label})",
            "symbol":      symbol,
            "stock":       plain_name,
            "company":     company_name,
            "exit_price":  exit_price,
            "entry_price": entry_price,
            "pnl_pct":     pnl_pct,
            "avg_count":   avg_count,
            "signal_time": now.isoformat() if now else datetime.now(IST).isoformat(),
            "sent_time":   datetime.now(IST).isoformat(),
            "source":      f"FyersScanner/{self.scanner_id}",
        }, label)


# ─── Strategy3WebhookMirror ──────────────────────────────────────────────────────
class Strategy3WebhookMirror:
    """
    Strategy 3 — Webhook Signal Mirror.

    Entry : external BUY/SELL signal sets ref_price = current live price.
    Buy   : first trigger_tf candle closing drop_pct% below ref (BUY side)
            first trigger_tf candle closing rise_pct% above ref (SELL side)
    Avg   : each avg_pct% move from last avg price
    Exit  : external EXIT-BUY / EXIT-SELL sets exit_ref_price = current live
            price; then exit when candle closes exit_offset_pct% past that ref.
    """

    def __init__(self, config: dict, state_store: S3StateStore,
                 webhook_sender: S3WebhookSender, mapper):
        self.config         = config
        self.state_store    = state_store
        self.webhook_sender = webhook_sender
        self.mapper         = mapper
        self._lock          = threading.Lock()

    # ── Incoming signals (called from Flask thread) ───────────────────────────
    def process_incoming_signal(self, signal_type: str, symbol: str,
                                 price: float, now: datetime):
        """
        signal_type : "BUY" | "SELL" | "EXIT_BUY" | "EXIT_SELL"
        symbol      : Fyers format — NSE:RELIANCE-EQ
        price       : current live price from price_store
        """
        plain_name   = self.mapper.get_plain_name(symbol)
        company_name = self.mapper.get_company_name(plain_name) if plain_name else ""
        rec = self.state_store.get_or_create(symbol, plain_name, company_name)

        with self._lock:
            if signal_type == "BUY":
                if rec.state != "GENERAL":
                    print(f"  ⚠️  S3 BUY duplicate ignored: {plain_name} ({rec.state})")
                    return
                rec.side     = "BUY"
                rec.state    = "WATCHED_BUY"
                rec.ref_price = price
                rec.ref_time  = now
                print(f"  📡 S3 BUY signal: {plain_name} | Ref ₹{price:.2f}")

            elif signal_type == "SELL":
                if rec.state != "GENERAL":
                    print(f"  ⚠️  S3 SELL duplicate ignored: {plain_name} ({rec.state})")
                    return
                rec.side     = "SELL"
                rec.state    = "WATCHED_SELL"
                rec.ref_price = price
                rec.ref_time  = now
                print(f"  📡 S3 SELL signal: {plain_name} | Ref ₹{price:.2f}")

            elif signal_type == "EXIT_BUY":
                if rec.state != "ACTIVE_BUY":
                    print(f"  ⚠️  S3 EXIT_BUY ignored: {plain_name} not in ACTIVE_BUY ({rec.state})")
                    return
                rec.state          = "EXITING_BUY"
                rec.exit_ref_price = price
                rec.exit_ref_time  = now
                print(f"  📡 S3 EXIT_BUY signal: {plain_name} | Exit ref ₹{price:.2f}")

            elif signal_type == "EXIT_SELL":
                if rec.state != "ACTIVE_SELL":
                    print(f"  ⚠️  S3 EXIT_SELL ignored: {plain_name} not in ACTIVE_SELL ({rec.state})")
                    return
                rec.state          = "EXITING_SELL"
                rec.exit_ref_price = price
                rec.exit_ref_time  = now
                print(f"  📡 S3 EXIT_SELL signal: {plain_name} | Exit ref ₹{price:.2f}")

            else:
                return

        self.state_store.save()

    # ── Candle close (called from main.py candle engine) ─────────────────────
    def on_candle_close(self, candle, timeframe: str):
        trigger_tf = str(self.config.get("trigger_timeframe", "5"))
        if timeframe != trigger_tf:
            return

        drop_pct            = float(self.config.get("drop_pct",            2.0))
        rise_pct            = float(self.config.get("rise_pct",            2.0))
        avg_pct_buy         = float(self.config.get("avg_pct_buy",         3.0))
        avg_pct_sell        = float(self.config.get("avg_pct_sell",        3.0))
        exit_offset_pct_buy = float(self.config.get("exit_offset_pct_buy", 1.0))
        exit_offset_pct_sell= float(self.config.get("exit_offset_pct_sell",1.0))

        symbol     = candle.symbol
        close_px   = candle.close
        close_time = candle.close_time

        rec = self.state_store.get(symbol)
        if rec is None or rec.state == "GENERAL":
            return

        # Update live price regardless of state
        rec.current_price = close_px

        fired = False
        with self._lock:
            state = rec.state

            # ── WATCHED_BUY: price dropped enough → enter long ────────────
            if state == "WATCHED_BUY" and rec.ref_price:
                drop = ((rec.ref_price - close_px) / rec.ref_price) * 100
                if drop >= drop_pct:
                    rec.state          = "ACTIVE_BUY"
                    rec.entry_price    = close_px
                    rec.entry_time     = close_time
                    rec.last_avg_price = close_px
                    chg = round(drop, 2)
                    print(f"  🟢 S3 BUY entry: {rec.plain_name} | ₹{close_px:.2f} "
                          f"({chg:.2f}% below ref)")
                    self.state_store.log_signal({
                        "type":       "BUY",
                        "label":      f"{rec.plain_name} (BUY)",
                        "symbol":     symbol,
                        "plain_name": rec.plain_name,
                        "price":      close_px,
                        "ref_price":  rec.ref_price,
                        "drop_pct":   chg,
                        "time":       close_time.isoformat() if close_time else None,
                    })
                    self.webhook_sender.send_entry(
                        "BUY", symbol, rec.plain_name, rec.company_name,
                        close_px, rec.ref_price, chg, close_time)
                    fired = True

            # ── WATCHED_SELL: price rose enough → enter short ─────────────
            elif state == "WATCHED_SELL" and rec.ref_price:
                rise = ((close_px - rec.ref_price) / rec.ref_price) * 100
                if rise >= rise_pct:
                    rec.state          = "ACTIVE_SELL"
                    rec.entry_price    = close_px
                    rec.entry_time     = close_time
                    rec.last_avg_price = close_px
                    chg = round(rise, 2)
                    print(f"  🔴 S3 SELL entry: {rec.plain_name} | ₹{close_px:.2f} "
                          f"({chg:.2f}% above ref)")
                    self.state_store.log_signal({
                        "type":       "SELL",
                        "label":      f"{rec.plain_name} (SELL)",
                        "symbol":     symbol,
                        "plain_name": rec.plain_name,
                        "price":      close_px,
                        "ref_price":  rec.ref_price,
                        "rise_pct":   chg,
                        "time":       close_time.isoformat() if close_time else None,
                    })
                    self.webhook_sender.send_entry(
                        "SELL", symbol, rec.plain_name, rec.company_name,
                        close_px, rec.ref_price, chg, close_time)
                    fired = True

            # ── ACTIVE_BUY: averaging ─────────────────────────────────────
            elif state == "ACTIVE_BUY" and rec.last_avg_price:
                avg_drop = ((rec.last_avg_price - close_px) / rec.last_avg_price) * 100
                if avg_drop >= avg_pct_buy:
                    rec.avg_count += 1
                    prev            = rec.last_avg_price
                    rec.last_avg_price = close_px
                    rec.avg_entries.append({
                        "avg_num":    rec.avg_count,
                        "price":      close_px,
                        "change_pct": round(avg_drop, 2),
                        "signal_time":close_time.isoformat() if close_time else None,
                    })
                    n = rec.avg_count
                    print(f"  🟡 S3 Avg {n} (BUY): {rec.plain_name} | ₹{close_px:.2f}")
                    self.state_store.log_signal({
                        "type":       "AVG_BUY",
                        "label":      f"{rec.plain_name} (Avg {n})",
                        "symbol":     symbol,
                        "plain_name": rec.plain_name,
                        "avg_price":  close_px,
                        "avg_number": n,
                        "time":       close_time.isoformat() if close_time else None,
                    })
                    self.webhook_sender.send_avg(
                        "BUY", symbol, rec.plain_name, rec.company_name,
                        close_px, prev, round(avg_drop, 2), n, close_time)
                    fired = True

            # ── ACTIVE_SELL: averaging ────────────────────────────────────
            elif state == "ACTIVE_SELL" and rec.last_avg_price:
                avg_rise = ((close_px - rec.last_avg_price) / rec.last_avg_price) * 100
                if avg_rise >= avg_pct_sell:
                    rec.avg_count += 1
                    prev            = rec.last_avg_price
                    rec.last_avg_price = close_px
                    rec.avg_entries.append({
                        "avg_num":    rec.avg_count,
                        "price":      close_px,
                        "change_pct": round(avg_rise, 2),
                        "signal_time":close_time.isoformat() if close_time else None,
                    })
                    n = rec.avg_count
                    print(f"  🟡 S3 Avg {n} (SELL): {rec.plain_name} | ₹{close_px:.2f}")
                    self.state_store.log_signal({
                        "type":       "AVG_SELL",
                        "label":      f"{rec.plain_name} (Avg Short {n})",
                        "symbol":     symbol,
                        "plain_name": rec.plain_name,
                        "avg_price":  close_px,
                        "avg_number": n,
                        "time":       close_time.isoformat() if close_time else None,
                    })
                    self.webhook_sender.send_avg(
                        "SELL", symbol, rec.plain_name, rec.company_name,
                        close_px, prev, round(avg_rise, 2), n, close_time)
                    fired = True

            # ── EXITING_BUY: close rose above exit_ref + offset → exit ────
            elif state == "EXITING_BUY" and rec.exit_ref_price:
                rise_from_ref = ((close_px - rec.exit_ref_price) / rec.exit_ref_price) * 100
                if rise_from_ref >= exit_offset_pct_buy:
                    pnl = (round(((close_px - rec.entry_price) / rec.entry_price) * 100, 2)
                           if rec.entry_price else 0.0)
                    print(f"  🔴 S3 EXIT-BUY: {rec.plain_name} | ₹{close_px:.2f} "
                          f"(P&L {pnl:+.2f}%)")
                    self.state_store.log_signal({
                        "type":        "EXIT_BUY",
                        "label":       f"{rec.plain_name} (EXIT-BUY)",
                        "symbol":      symbol,
                        "plain_name":  rec.plain_name,
                        "price":       close_px,
                        "entry_price": rec.entry_price,
                        "pnl_pct":     pnl,
                        "avg_count":   rec.avg_count,
                        "time":        close_time.isoformat() if close_time else None,
                    })
                    self.webhook_sender.send_exit(
                        "BUY", symbol, rec.plain_name, rec.company_name,
                        close_px, rec.entry_price or 0, pnl, rec.avg_count, close_time)
                    rec.reset()
                    fired = True

            # ── EXITING_SELL: close fell below exit_ref − offset → exit ───
            elif state == "EXITING_SELL" and rec.exit_ref_price:
                drop_from_ref = ((rec.exit_ref_price - close_px) / rec.exit_ref_price) * 100
                if drop_from_ref >= exit_offset_pct_sell:
                    pnl = (round(((rec.entry_price - close_px) / rec.entry_price) * 100, 2)
                           if rec.entry_price else 0.0)
                    print(f"  🟢 S3 EXIT-SELL: {rec.plain_name} | ₹{close_px:.2f} "
                          f"(P&L {pnl:+.2f}%)")
                    self.state_store.log_signal({
                        "type":        "EXIT_SELL",
                        "label":       f"{rec.plain_name} (EXIT-SELL)",
                        "symbol":      symbol,
                        "plain_name":  rec.plain_name,
                        "price":       close_px,
                        "entry_price": rec.entry_price,
                        "pnl_pct":     pnl,
                        "avg_count":   rec.avg_count,
                        "time":        close_time.isoformat() if close_time else None,
                    })
                    self.webhook_sender.send_exit(
                        "SELL", symbol, rec.plain_name, rec.company_name,
                        close_px, rec.entry_price or 0, pnl, rec.avg_count, close_time)
                    rec.reset()
                    fired = True

        if fired:
            self.state_store.save()

    # ── Tick handler ──────────────────────────────────────────────────────────
    def on_tick(self, symbol: str, price: float):
        rec = self.state_store.get(symbol)
        if rec is not None and rec.state != "GENERAL":
            rec.current_price = price

    def save_state(self):
        self.state_store.save()
