"""
state_store.py — Stock state tracking with full timestamps on all price events.
"""

import json
import os
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")


class StockState:
    GENERAL              = "GENERAL"
    # Buy side
    WATCHED              = "WATCHED"           # alias: WATCHED_BUY
    ACTIVE               = "ACTIVE_BUY"
    EXITED               = "EXITED"            # buy exit
    COOLING_BUY_STOPLOSS = "COOLING_BUY_STOPLOSS"
    # Sell side (Phase 2)
    WATCHED_SELL          = "WATCHED_SELL"
    ACTIVE_SELL           = "ACTIVE_SELL"
    EXITED_SELL           = "EXITED_SELL"
    COOLING_SELL_STOPLOSS = "COOLING_SELL_STOPLOSS"


class AvgEntry:
    def __init__(self, avg_num, price, drop_pct, signal_time):
        self.avg_num     = avg_num
        self.price       = price
        self.drop_pct    = drop_pct
        self.signal_time = signal_time

    def to_dict(self):
        return {
            "avg_num":     self.avg_num,
            "price":       self.price,
            "drop_pct":    self.drop_pct,
            "signal_time": self.signal_time.isoformat() if self.signal_time else None
        }

    @classmethod
    def from_dict(cls, d):
        st = None
        if d.get("signal_time"):
            try:
                st = datetime.fromisoformat(d["signal_time"])
            except:
                pass
        return cls(d["avg_num"], d["price"], d["drop_pct"], st)


class StockRecord:

    def __init__(self, symbol, plain_name, company_name=""):
        self.symbol       = symbol
        self.plain_name   = plain_name
        self.company_name = company_name
        self.state        = StockState.GENERAL
        # Direction: None when GENERAL; "BUY" or "SELL" when the stock is
        # working through a watch / active / cooling / exited cycle.
        # Field names below (reference_price, buy_price, drop_pct_at_buy,
        # avg_*) are shared between both directions — `side` tells you how
        # to interpret them (e.g. drop_pct_at_buy = rise_pct_at_short for
        # SELL side).
        self.side             = None

        # Watched
        self.watched_at       = None
        self.reference_price  = None
        self.reference_time   = None   # timestamp when ref price was locked
        self.rsi_at_watch     = None

        # Buy
        self.buy_signal_at    = None
        self.buy_price        = None
        self.buy_time         = None   # same as buy_signal_at but explicit
        self.drop_pct_at_buy  = None

        # Averaging
        self.avg_entries      = []
        self.last_avg_price   = None
        self.last_avg_time    = None   # timestamp of last avg
        self.avg_count        = 0

        # Exit
        self.exit_signal_at   = None
        self.exit_price       = None
        self.exit_time        = None
        self.rsi_at_exit      = None

        # Current display
        self.current_price    = None
        self.current_rsi_scan = None
        self.current_rsi_exit = None

        # Set True when state was restored by carry-forward (scanner was offline)
        self.gap_fill         = False

    def avg_label(self):
        return "BUY" if self.avg_count == 0 else f"Avg {self.avg_count}"

    def to_dict(self):
        def fmt(dt):
            return dt.isoformat() if dt else None
        return {
            "symbol":          self.symbol,
            "plain_name":      self.plain_name,
            "company_name":    self.company_name,
            "state":           self.state,
            "side":            self.side,
            "watched_at":      fmt(self.watched_at),
            "reference_price": self.reference_price,
            "reference_time":  fmt(self.reference_time),
            "rsi_at_watch":    self.rsi_at_watch,
            "buy_signal_at":   fmt(self.buy_signal_at),
            "buy_price":       self.buy_price,
            "buy_time":        fmt(self.buy_time),
            "drop_pct_at_buy": self.drop_pct_at_buy,
            "avg_entries":     [a.to_dict() for a in self.avg_entries],
            "last_avg_price":  self.last_avg_price,
            "last_avg_time":   fmt(self.last_avg_time),
            "avg_count":       self.avg_count,
            "exit_signal_at":  fmt(self.exit_signal_at),
            "exit_price":      self.exit_price,
            "exit_time":       fmt(self.exit_time),
            "rsi_at_exit":     self.rsi_at_exit,
            "current_price":   self.current_price,
            "current_rsi_scan":self.current_rsi_scan,
            "current_rsi_exit":self.current_rsi_exit,
            "gap_fill":        self.gap_fill,
        }

    @classmethod
    def from_dict(cls, d):
        rec = cls(d["symbol"], d["plain_name"], d.get("company_name",""))
        rec.state            = d.get("state", StockState.GENERAL)
        rec.side             = d.get("side")
        # Backfill `side` from legacy rows that predate Phase 2
        if rec.side is None and rec.state in (
                StockState.WATCHED, StockState.ACTIVE,
                StockState.EXITED, StockState.COOLING_BUY_STOPLOSS):
            rec.side = "BUY"
        rec.reference_price  = d.get("reference_price")
        rec.rsi_at_watch     = d.get("rsi_at_watch")
        rec.buy_price        = d.get("buy_price")
        rec.drop_pct_at_buy  = d.get("drop_pct_at_buy")
        rec.avg_entries      = [AvgEntry.from_dict(a) for a in d.get("avg_entries",[])]
        rec.last_avg_price   = d.get("last_avg_price")
        rec.avg_count        = d.get("avg_count", 0)
        rec.exit_price       = d.get("exit_price")
        rec.rsi_at_exit      = d.get("rsi_at_exit")
        rec.current_price    = d.get("current_price")
        rec.current_rsi_scan = d.get("current_rsi_scan")
        rec.current_rsi_exit = d.get("current_rsi_exit")
        rec.gap_fill         = d.get("gap_fill", False)

        def pdt(s):
            if s:
                try: return datetime.fromisoformat(s)
                except: pass
            return None

        rec.watched_at      = pdt(d.get("watched_at"))
        rec.reference_time  = pdt(d.get("reference_time")) or rec.watched_at
        rec.buy_signal_at   = pdt(d.get("buy_signal_at"))
        rec.buy_time        = pdt(d.get("buy_time")) or rec.buy_signal_at
        rec.last_avg_time   = pdt(d.get("last_avg_time"))
        rec.exit_signal_at  = pdt(d.get("exit_signal_at"))
        rec.exit_time       = pdt(d.get("exit_time")) or rec.exit_signal_at
        return rec


class StateStore:

    def __init__(self, state_file):
        self.state_file  = state_file
        self._records    = {}
        self._signal_log = []
        self.load()

    def get(self, symbol):
        return self._records.get(symbol)

    def get_or_create(self, symbol, plain_name, company_name=""):
        if symbol not in self._records:
            self._records[symbol] = StockRecord(symbol, plain_name, company_name)
        return self._records[symbol]

    def all_records(self):
        return list(self._records.values())

    def get_watched(self):
        return [r for r in self._records.values() if r.state == StockState.WATCHED]

    def get_active_buys(self):
        return [r for r in self._records.values() if r.state == StockState.ACTIVE]

    def get_exited(self):
        return [r for r in self._records.values() if r.state == StockState.EXITED]

    def get_cooling_stoploss(self):
        """Stocks that exited via live D-RSI stoploss; locked until RSI recovers."""
        return [r for r in self._records.values()
                if r.state == StockState.COOLING_BUY_STOPLOSS]

    # ─── Sell-side accessors ──────────────────────────────────────────────
    def get_watched_sells(self):
        return [r for r in self._records.values()
                if r.state == StockState.WATCHED_SELL]

    def get_active_sells(self):
        return [r for r in self._records.values()
                if r.state == StockState.ACTIVE_SELL]

    def get_exited_sells(self):
        return [r for r in self._records.values()
                if r.state == StockState.EXITED_SELL]

    def get_cooling_sell_stoploss(self):
        return [r for r in self._records.values()
                if r.state == StockState.COOLING_SELL_STOPLOSS]

    def get_signal_log(self):
        return list(reversed(self._signal_log))

    # ─── State transitions ─────────────────────────────────────────────────────
    def move_to_watched(self, symbol, reference_price, rsi_value, now=None,
                        gap_fill=False):
        rec = self._records.get(symbol)
        if not rec:
            return
        now = now or datetime.now(IST)
        rec.state           = StockState.WATCHED
        rec.side            = "BUY"
        rec.watched_at      = now
        rec.reference_price = reference_price
        rec.reference_time  = now
        rec.rsi_at_watch    = rsi_value
        rec.gap_fill        = gap_fill
        rec.buy_signal_at   = None
        rec.buy_price       = None
        rec.buy_time        = None
        rec.avg_entries     = []
        rec.last_avg_price  = None
        rec.last_avg_time   = None
        rec.avg_count       = 0
        rec.exit_signal_at  = None
        rec.exit_price      = None
        rec.exit_time       = None
        self.save()
        missed = " [MISSED]" if gap_fill else ""
        print(f"  👁️  WATCHED{missed}: {rec.plain_name} | "
              f"Ref: ₹{reference_price} @ {now.strftime('%d-%b %H:%M')} | "
              f"RSI: {rsi_value:.2f}")

    def move_to_active_buy(self, symbol, buy_price, drop_pct, now=None,
                           gap_fill=False):
        rec = self._records.get(symbol)
        if not rec:
            return
        now = now or datetime.now(IST)
        rec.state           = StockState.ACTIVE
        rec.side            = "BUY"
        rec.buy_signal_at   = now
        rec.buy_price       = buy_price
        rec.buy_time        = now
        rec.drop_pct_at_buy = drop_pct
        rec.last_avg_price  = buy_price
        rec.last_avg_time   = now
        rec.avg_count       = 0
        rec.avg_entries     = []
        rec.gap_fill        = gap_fill
        self._signal_log.append({
            "type":       "BUY",
            "symbol":     rec.symbol,
            "plain_name": rec.plain_name,
            "company":    rec.company_name,
            "label":      "BUY",
            "price":      buy_price,
            "ref_price":  rec.reference_price,
            "ref_time":   rec.reference_time.isoformat() if rec.reference_time else None,
            "drop_pct":   drop_pct,
            "rsi_watch":  rec.rsi_at_watch,
            "time":       now.isoformat(),
            "gap_fill":   gap_fill,
        })
        self.save()
        missed = " [MISSED]" if gap_fill else ""
        print(f"  🟢 BUY{missed}: {rec.plain_name} | ₹{buy_price} @ "
              f"{now.strftime('%d-%b %H:%M')} | Drop: {drop_pct}%")

    def add_avg(self, symbol, avg_price, drop_pct, now=None):
        rec = self._records.get(symbol)
        if not rec or rec.state != StockState.ACTIVE:
            return
        now = now or datetime.now(IST)
        rec.avg_count      += 1
        rec.last_avg_price  = avg_price
        rec.last_avg_time   = now
        entry = AvgEntry(rec.avg_count, avg_price, drop_pct, now)
        rec.avg_entries.append(entry)
        label = f"Avg {rec.avg_count}"
        self._signal_log.append({
            "type":       "AVG",
            "symbol":     rec.symbol,
            "plain_name": rec.plain_name,
            "company":    rec.company_name,
            "label":      label,
            "price":      avg_price,
            "drop_pct":   drop_pct,
            "avg_num":    rec.avg_count,
            "time":       now.isoformat(),
        })
        self.save()
        print(f"  🔵 {label}: {rec.plain_name} | ₹{avg_price} @ "
              f"{now.strftime('%d-%b %H:%M')} | Drop: {drop_pct:.2f}%")

    def move_to_exited(self, symbol, exit_price, rsi_value, now=None,
                       gap_fill=False):
        rec = self._records.get(symbol)
        if not rec:
            return
        now = now or datetime.now(IST)
        rec.state          = StockState.EXITED
        rec.exit_signal_at = now
        rec.exit_price     = exit_price
        rec.exit_time      = now
        rec.rsi_at_exit    = rsi_value
        self._signal_log.append({
            "type":       "EXIT",
            "symbol":     rec.symbol,
            "plain_name": rec.plain_name,
            "company":    rec.company_name,
            "label":      "EXIT",
            "price":      exit_price,
            "buy_price":  rec.buy_price,
            "buy_time":   rec.buy_time.isoformat() if rec.buy_time else None,
            "avg_count":  rec.avg_count,
            "rsi_exit":   rsi_value,
            "time":       now.isoformat(),
            "gap_fill":   gap_fill,
        })
        self.save()
        missed = " [MISSED]" if gap_fill else ""
        print(f"  🔴 EXIT{missed}: {rec.plain_name} | ₹{exit_price} @ "
              f"{now.strftime('%d-%b %H:%M')} | RSI: {rsi_value}")

    def move_to_cooling_buy_stoploss(self, symbol, exit_price, daily_rsi,
                                      now=None):
        """Stoploss exit: transition to COOLING_BUY_STOPLOSS and log it.
        Stays in this state until RSI rallies past stoploss_cooling_rsi_buy."""
        rec = self._records.get(symbol)
        if not rec:
            return
        now = now or datetime.now(IST)
        rec.state          = StockState.COOLING_BUY_STOPLOSS
        rec.exit_signal_at = now
        rec.exit_price     = exit_price
        rec.exit_time      = now
        rec.rsi_at_exit    = daily_rsi
        self._signal_log.append({
            "type":       "EXIT_STOPLOSS",
            "symbol":     rec.symbol,
            "plain_name": rec.plain_name,
            "company":    rec.company_name,
            "label":      "EXIT (Daily-RSI Stoploss)",
            "price":      exit_price,
            "buy_price":  rec.buy_price,
            "buy_time":   rec.buy_time.isoformat() if rec.buy_time else None,
            "avg_count":  rec.avg_count,
            "daily_rsi":  daily_rsi,
            "time":       now.isoformat(),
        })
        self.save()
        print(f"  🛑 STOPLOSS: {rec.plain_name} | ₹{exit_price} @ "
              f"{now.strftime('%d-%b %H:%M')} | D-RSI: {daily_rsi:.2f}")

    def reset_to_general(self, symbol, reason=""):
        rec = self._records.get(symbol)
        if not rec:
            return
        prev = rec.state
        rec.state           = StockState.GENERAL
        rec.side            = None
        rec.watched_at      = None
        rec.reference_price = None
        rec.reference_time  = None
        rec.rsi_at_watch    = None
        rec.buy_signal_at   = None
        rec.buy_price       = None
        rec.buy_time        = None
        rec.avg_entries     = []
        rec.last_avg_price  = None
        rec.last_avg_time   = None
        rec.avg_count       = 0
        rec.gap_fill        = False
        self.save()
        print(f"  🔄 RESET: {rec.plain_name} | Was: {prev} | {reason}")

    # ─── Sell-side transitions ────────────────────────────────────────────
    # Field reuse: reference_price / buy_price / drop_pct_at_buy / avg_*
    # hold sell-side data when side=="SELL". `side` makes the direction
    # explicit so the dashboard can label them correctly.
    def move_to_watched_sell(self, symbol, reference_price, rsi_value,
                              now=None, gap_fill=False):
        rec = self._records.get(symbol)
        if not rec:
            return
        now = now or datetime.now(IST)
        rec.state           = StockState.WATCHED_SELL
        rec.side            = "SELL"
        rec.watched_at      = now
        rec.reference_price = reference_price
        rec.reference_time  = now
        rec.rsi_at_watch    = rsi_value
        rec.gap_fill        = gap_fill
        rec.buy_signal_at   = None
        rec.buy_price       = None
        rec.buy_time        = None
        rec.avg_entries     = []
        rec.last_avg_price  = None
        rec.last_avg_time   = None
        rec.avg_count       = 0
        rec.exit_signal_at  = None
        rec.exit_price      = None
        rec.exit_time       = None
        self.save()
        print(f"  👁️  WATCHED-SELL: {rec.plain_name} | Ref: ₹{reference_price} "
              f"@ {now.strftime('%d-%b %H:%M')} | RSI: {rsi_value:.2f}")

    def move_to_active_sell(self, symbol, short_price, rise_pct, now=None,
                             gap_fill=False):
        rec = self._records.get(symbol)
        if not rec:
            return
        now = now or datetime.now(IST)
        rec.state            = StockState.ACTIVE_SELL
        rec.side             = "SELL"
        rec.buy_signal_at    = now
        rec.buy_price        = short_price
        rec.buy_time         = now
        rec.drop_pct_at_buy  = rise_pct   # field reused — % rise for sell
        rec.last_avg_price   = short_price
        rec.last_avg_time    = now
        rec.avg_count        = 0
        rec.avg_entries      = []
        rec.gap_fill         = gap_fill
        self._signal_log.append({
            "type":       "SELL",
            "symbol":     rec.symbol,
            "plain_name": rec.plain_name,
            "company":    rec.company_name,
            "label":      "SELL",
            "price":      short_price,
            "ref_price":  rec.reference_price,
            "ref_time":   rec.reference_time.isoformat() if rec.reference_time else None,
            "rise_pct":   rise_pct,
            "rsi_watch":  rec.rsi_at_watch,
            "side":       "SELL",
            "time":       now.isoformat(),
            "gap_fill":   gap_fill,
        })
        self.save()
        print(f"  🔻 SELL: {rec.plain_name} | ₹{short_price} @ "
              f"{now.strftime('%d-%b %H:%M')} | Rise: {rise_pct}%")

    def add_avg_sell(self, symbol, avg_price, rise_pct, now=None):
        rec = self._records.get(symbol)
        if not rec or rec.state != StockState.ACTIVE_SELL:
            return
        now = now or datetime.now(IST)
        rec.avg_count      += 1
        rec.last_avg_price  = avg_price
        rec.last_avg_time   = now
        entry = AvgEntry(rec.avg_count, avg_price, rise_pct, now)
        rec.avg_entries.append(entry)
        label = f"Avg Short {rec.avg_count}"
        self._signal_log.append({
            "type":       "AVG_SELL",
            "symbol":     rec.symbol,
            "plain_name": rec.plain_name,
            "company":    rec.company_name,
            "label":      label,
            "price":      avg_price,
            "rise_pct":   rise_pct,
            "avg_num":    rec.avg_count,
            "side":       "SELL",
            "time":       now.isoformat(),
        })
        self.save()
        print(f"  🔵 {label}: {rec.plain_name} | ₹{avg_price} @ "
              f"{now.strftime('%d-%b %H:%M')} | Rise: {rise_pct:.2f}%")

    def move_to_exited_sell(self, symbol, exit_price, rsi_value, now=None,
                             gap_fill=False):
        rec = self._records.get(symbol)
        if not rec:
            return
        now = now or datetime.now(IST)
        rec.state          = StockState.EXITED_SELL
        rec.exit_signal_at = now
        rec.exit_price     = exit_price
        rec.exit_time      = now
        rec.rsi_at_exit    = rsi_value
        self._signal_log.append({
            "type":         "EXIT_SELL",
            "symbol":       rec.symbol,
            "plain_name":   rec.plain_name,
            "company":      rec.company_name,
            "label":        "EXIT SELL",
            "price":        exit_price,
            "short_price":  rec.buy_price,
            "short_time":   rec.buy_time.isoformat() if rec.buy_time else None,
            "avg_count":    rec.avg_count,
            "rsi_exit":     rsi_value,
            "side":         "SELL",
            "time":         now.isoformat(),
            "gap_fill":     gap_fill,
        })
        self.save()
        missed = " [MISSED]" if gap_fill else ""
        print(f"  🟢 EXIT-SELL{missed}: {rec.plain_name} | ₹{exit_price} @ "
              f"{now.strftime('%d-%b %H:%M')} | RSI: {rsi_value}")

    def move_to_cooling_sell_stoploss(self, symbol, exit_price, daily_rsi,
                                       now=None):
        rec = self._records.get(symbol)
        if not rec:
            return
        now = now or datetime.now(IST)
        rec.state          = StockState.COOLING_SELL_STOPLOSS
        rec.exit_signal_at = now
        rec.exit_price     = exit_price
        rec.exit_time      = now
        rec.rsi_at_exit    = daily_rsi
        self._signal_log.append({
            "type":         "EXIT_SELL_STOPLOSS",
            "symbol":       rec.symbol,
            "plain_name":   rec.plain_name,
            "company":      rec.company_name,
            "label":        "EXIT SELL (Daily-RSI Stoploss)",
            "price":        exit_price,
            "short_price":  rec.buy_price,
            "short_time":   rec.buy_time.isoformat() if rec.buy_time else None,
            "avg_count":    rec.avg_count,
            "daily_rsi":    daily_rsi,
            "side":         "SELL",
            "time":         now.isoformat(),
        })
        self.save()
        print(f"  🛑 STOPLOSS-SELL: {rec.plain_name} | ₹{exit_price} @ "
              f"{now.strftime('%d-%b %H:%M')} | D-RSI: {daily_rsi:.2f}")

    def update_current_values(self, symbol, price=None,
                               rsi_scan=None, rsi_exit=None):
        rec = self._records.get(symbol)
        if not rec:
            return
        if price    is not None: rec.current_price    = price
        if rsi_scan is not None: rec.current_rsi_scan = rsi_scan
        if rsi_exit is not None: rec.current_rsi_exit = rsi_exit

    def save(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        data = {
            "records":    {s: r.to_dict() for s, r in self._records.items()},
            "signal_log": self._signal_log[-1000:],
            "saved_at":   datetime.now(IST).isoformat()
        }
        with open(self.state_file, "w") as f:
            json.dump(data, f, indent=2)

    def load(self):
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file, "r") as f:
                data = json.load(f)
            for sym, d in data.get("records", {}).items():
                self._records[sym] = StockRecord.from_dict(d)
            self._signal_log = data.get("signal_log", [])
            print(f"✅ State loaded: {len(self._records)} stocks, "
                  f"{len(self._signal_log)} signals")
        except Exception as e:
            print(f"⚠️  State load error: {e}")

    def summary(self):
        return {
            "total":          len(self._records),
            "watched":        len(self.get_watched()),
            "active":         len(self.get_active_buys()),
            "exited":         len(self.get_exited()),
            "watched_sell":   len(self.get_watched_sells()),
            "active_sell":    len(self.get_active_sells()),
            "exited_sell":    len(self.get_exited_sells()),
            "cooling_sl_buy": len(self.get_cooling_stoploss()),
            "cooling_sl_sell":len(self.get_cooling_sell_stoploss()),
            "general":        sum(1 for r in self._records.values()
                                   if r.state == StockState.GENERAL),
        }
