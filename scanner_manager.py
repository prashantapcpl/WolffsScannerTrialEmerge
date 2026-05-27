"""
scanner_manager.py
Manages multiple independent scanner instances.
"""

import os
import sys
import json

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.state_store    import StateStore
from core.rsi_engine     import RSIEngine
from core.carry_forward  import CarryForwardEngine
from core.stock_groups   import get_stock_groups
from strategies.rsi_drop import RSIDropStrategy
from signals.webhook_sender import WebhookSender


def load_config():
    with open(os.path.join(ROOT, "config.json"), "r") as f:
        return json.load(f)


class ScannerInstance:

    def __init__(self, scanner_id, scanner_cfg,
                 rsi_cache, mapper, all_symbols):
        self.scanner_id = scanner_id
        self.config     = scanner_cfg
        self.name       = scanner_cfg.get("name", scanner_id)
        self.active     = scanner_cfg.get("active", False)

        if not self.active:
            self.state_store    = None
            self.rsi_engine     = None
            self.strategy       = None
            self.webhook_sender = None
            return

        # Scanners with a custom strategy_type run independently of
        # ScannerManager (e.g. Strategy 4 momentum runs from main.py with its
        # own state store / RSI engine).  Mark them inactive here so the
        # default RSIDropStrategy pipeline doesn't clobber their state files.
        if scanner_cfg.get("strategy_type") == "momentum":
            self.active         = False
            self.state_store    = None
            self.rsi_engine     = None
            self.strategy       = None
            self.webhook_sender = None
            return

        settings   = scanner_cfg.get("settings", {})
        state_file = os.path.join(ROOT, scanner_cfg.get(
            "state_file", f"data/{scanner_id}_state.json"))

        self.state_store    = StateStore(state_file)
        self.rsi_engine     = RSIEngine(period=int(settings.get("rsi_period", 14)))
        # Strategy gets group info so it can gate buy/sell ENTRY transitions
        # by per-side group membership (active positions always process).
        self.strategy       = RSIDropStrategy(
            config           = settings,
            stock_groups     = get_stock_groups(),
            buy_stock_group  = scanner_cfg.get("buy_stock_group",  "nifty650"),
            sell_stock_group = scanner_cfg.get("sell_stock_group", "nifty650"),
        )
        self.webhook_sender = WebhookSender(
            scanner_id   = scanner_id,
            webhooks_cfg = scanner_cfg.get("webhooks", {})
        )
        self.rsi_cache   = rsi_cache
        self.mapper      = mapper
        self.all_symbols = all_symbols

        # Stock-group filter — Scanner 1/2 now trade BOTH sides. Symbol is
        # processed if it's in the buy OR sell group. Open positions always
        # get processed regardless so they can exit cleanly.
        self.stock_groups     = get_stock_groups()
        self.buy_stock_group  = scanner_cfg.get("buy_stock_group",  "nifty650")
        self.sell_stock_group = scanner_cfg.get("sell_stock_group", "nifty650")
        if self.buy_stock_group and self.buy_stock_group not in self.stock_groups.list():
            print(f"  ⚠️  [{scanner_id}] buy_stock_group '{self.buy_stock_group}' "
                  f"not found — falling back to no buy filter.")
            self.buy_stock_group = None
        if self.sell_stock_group and self.sell_stock_group not in self.stock_groups.list():
            print(f"  ⚠️  [{scanner_id}] sell_stock_group '{self.sell_stock_group}' "
                  f"not found — falling back to no sell filter.")
            self.sell_stock_group = None

        # Register all stocks
        for sym in all_symbols:
            plain   = mapper.get_plain_name(sym)
            company = mapper.get_company_name(plain)
            self.state_store.get_or_create(sym, plain, company)
        self.state_store.save()

    def seed_rsi(self, timeframes):
        if not self.active:
            return
        self.rsi_cache.seed_rsi_engine(
            self.rsi_engine, self.all_symbols, timeframes
        )

    def run_carry_forward(self, fire_webhooks: bool = False):
        if not self.active:
            return 0
        carry = CarryForwardEngine(
            rsi_cache       = self.rsi_cache,
            state_store     = self.state_store,
            strategy_config = self.config.get("settings", {})
        )
        # Only pass webhook_sender during genuine gap recovery (mid-session
        # reconnect).  On normal startup fire_webhooks=False so carry-forward
        # restores states silently without sending duplicate/stale signals.
        n = carry.run(self.all_symbols, self.mapper,
                      webhook_sender=self.webhook_sender if fire_webhooks else None)
        carry.seed_prev_rsi(self.all_symbols)
        self.state_store.save()
        return n

    def on_candle_close(self, candle, timeframe):
        if not self.active:
            return
        symbol = candle.symbol

        # Stock-group filter — skip GENERAL symbols outside BOTH buy and
        # sell groups. Active / WATCHED / COOLING records always run so an
        # open position can exit cleanly regardless of group changes.
        if self.buy_stock_group or self.sell_stock_group:
            rec = self.state_store.get(symbol)
            from core.state_store import StockState
            if rec is None or rec.state == StockState.GENERAL:
                in_buy  = (self.buy_stock_group  is not None
                           and self.stock_groups.in_group(symbol, self.buy_stock_group))
                in_sell = (self.sell_stock_group is not None
                           and self.stock_groups.in_group(symbol, self.sell_stock_group))
                # If no group configured, treat the side as "any symbol"
                if self.buy_stock_group  is None: in_buy  = True
                if self.sell_stock_group is None: in_sell = True
                if not (in_buy or in_sell):
                    self.rsi_engine.update(symbol, timeframe, candle.close)
                    return

        plain_name = self.mapper.get_plain_name(symbol)
        self.rsi_engine.update(symbol, timeframe, candle.close)
        try:
            self.strategy.on_candle_close(
                symbol         = symbol,
                plain_name     = plain_name,
                candle         = candle,
                timeframe      = timeframe,
                rsi_engine     = self.rsi_engine,
                state_store    = self.state_store,
                webhook_sender = self.webhook_sender
            )
        except Exception as e:
            print(f"⚠️  [{self.scanner_id}] {symbol}: {e}")

    def on_tick(self, symbol, price):
        if not self.active:
            return
        self.state_store.update_current_values(symbol, price=price)
        # Route tick to strategy so live D-RSI stoploss can fire
        try:
            self.strategy.on_tick(symbol, price, self.rsi_engine,
                                   self.state_store, self.webhook_sender)
        except TypeError:
            # Backward-compat: older strategies use the 4-arg signature
            self.strategy.on_tick(symbol, price, self.rsi_engine,
                                   self.state_store)
        except Exception as e:
            print(f"⚠️  [{self.scanner_id}] tick {symbol}: {e}")

    def on_market_open(self, rsi_engine=None):
        if not self.active:
            return
        self.strategy.on_market_open(self.state_store, self.rsi_engine)

    def on_market_close(self):
        if not self.active:
            return
        self.strategy.on_market_close(self.state_store)

    def update_config(self, new_settings):
        if not self.active:
            return
        self.strategy.update_config(new_settings)

    def summary(self):
        if not self.active:
            return {"total":0,"watched":0,"active":0,"exited":0}
        return self.state_store.summary()


class ScannerManager:

    def __init__(self, rsi_cache, mapper, all_symbols):
        self.rsi_cache   = rsi_cache
        self.mapper      = mapper
        self.all_symbols = all_symbols
        self.scanners    = {}
        self._load_scanners()

    def _load_scanners(self):
        config = load_config()
        for sid, scfg in config.get("scanners", {}).items():
            status = "active" if scfg.get("active") else "inactive"
            print(f"  📊 {sid}: {scfg.get('name','?')} ({status})")
            self.scanners[sid] = ScannerInstance(
                scanner_id  = sid,
                scanner_cfg = scfg,
                rsi_cache   = self.rsi_cache,
                mapper      = self.mapper,
                all_symbols = self.all_symbols
            )

    def get(self, scanner_id):
        return self.scanners.get(scanner_id)

    def get_active(self):
        return [s for s in self.scanners.values() if s.active]

    def seed_all_rsi(self, timeframes):
        for scanner in self.get_active():
            print(f"  📊 Seeding RSI for {scanner.name}...")
            scanner.seed_rsi(timeframes)

    def run_all_carry_forward(self, fire_webhooks: bool = False):
        for scanner in self.get_active():
            print(f"\n🔄 Carry-forward for {scanner.name}...")
            scanner.run_carry_forward(fire_webhooks=fire_webhooks)

    def on_candle_close(self, candle, timeframe):
        for scanner in self.get_active():
            scanner.on_candle_close(candle, timeframe)

    def on_tick(self, symbol, price):
        for scanner in self.get_active():
            scanner.on_tick(symbol, price)

    def on_market_open(self):
        for scanner in self.get_active():
            scanner.on_market_open()

    def on_market_close(self):
        for scanner in self.get_active():
            scanner.on_market_close()

    def reload_configs(self):
        config = load_config()
        for sid, scfg in config.get("scanners", {}).items():
            scanner = self.scanners.get(sid)
            if scanner and scanner.active:
                scanner.update_config(scfg.get("settings", {}))

    def summary(self):
        result = {}
        for sid, scanner in self.scanners.items():
            result[sid] = {
                "name":   scanner.name,
                "active": scanner.active,
                **scanner.summary()
            }
        return result
