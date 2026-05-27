"""
rsi_drop.py — RSI Drop + Averaging Strategy

Entry : first scan_tf candle closing below rsi_entry (daily/weekly filters applied)
Watch : reference price locked at that candle — never updated
Reset : any scan_tf candle closing above rsi_reset, or higher-TF filters fail
Buy   : first trigger_tf candle closing drop_pct% below reference price
Avg   : each trigger_tf candle closing avg_pct% below last avg price
Exit  : first exit_tf candle closing above rsi_exit_threshold
"""

import os
import sys
from datetime import datetime
import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from strategies.base_strategy import BaseStrategy
from core.state_store import StockState

IST = pytz.timezone("Asia/Kolkata")


class RSIDropStrategy(BaseStrategy):

    def __init__(self, config: dict, stock_groups=None,
                 buy_stock_group: str = None, sell_stock_group: str = None):
        super().__init__(name="RSI Drop Strategy", config=config)
        # Per-side stock-group eligibility filters. None = no filtering for
        # that side. Active positions always process regardless (so an open
        # position can exit cleanly even if the universe was narrowed later).
        self.stock_groups     = stock_groups
        self.buy_stock_group  = buy_stock_group
        self.sell_stock_group = sell_stock_group

    def _eligible(self, symbol: str, side: str) -> bool:
        """True if this symbol can ENTER a new position on the given side."""
        if self.stock_groups is None:
            return True
        group = (self.buy_stock_group if side == "BUY"
                 else self.sell_stock_group)
        if not group:
            return True
        return self.stock_groups.in_group(symbol, group)

    def get_description(self) -> str:
        cfg = self.config
        d   = f" | Daily RSI>{cfg.get('daily_rsi_threshold',60)}" if cfg.get("daily_rsi_filter_enabled") else ""
        w   = f" | Weekly RSI>{cfg.get('weekly_rsi_threshold',60)}" if cfg.get("weekly_rsi_filter_enabled") else ""
        return (
            f"Watch: RSI({cfg.get('rsi_period',14)}) < {cfg.get('rsi_entry_threshold',20)} "
            f"on {cfg.get('scan_timeframe','5')}m{d}{w} | "
            f"Buy: {cfg.get('drop_percent',2.0)}% drop | "
            f"Avg: every {cfg.get('avg_drop_percent',3.0)}% | "
            f"Exit: RSI>{cfg.get('rsi_exit_threshold',70)} on {cfg.get('exit_timeframe','10')}m"
        )

    def on_candle_close(self, symbol, plain_name, candle, timeframe,
                        rsi_engine, state_store, webhook_sender):

        cfg        = self.config
        scan_tf    = str(cfg.get("scan_timeframe",    "5"))
        trigger_tf = str(cfg.get("trigger_timeframe", "1"))
        exit_tf    = str(cfg.get("exit_timeframe",    "10"))

        # Buy side
        rsi_entry  = float(cfg.get("rsi_entry_threshold",  20))
        rsi_reset  = float(cfg.get("rsi_reset_threshold",  70))
        rsi_exit_v = float(cfg.get("rsi_exit_threshold",   70))
        drop_pct   = float(cfg.get("drop_percent",          2.0))
        avg_pct    = float(cfg.get("avg_drop_percent",       3.0))

        # Sell side (Phase 2 — mirrors the buy parameters)
        rsi_entry_sell = float(cfg.get("rsi_entry_sell_threshold", 80))
        rsi_reset_sell = float(cfg.get("rsi_reset_sell_threshold", 30))
        rsi_exit_sell  = float(cfg.get("rsi_exit_sell_threshold",  32))
        rise_pct       = float(cfg.get("rise_percent",              2.0))
        avg_rise_pct   = float(cfg.get("avg_rise_percent",          3.0))

        daily_filter  = bool(cfg.get("daily_rsi_filter_enabled",  True))
        daily_thresh  = float(cfg.get("daily_rsi_threshold",       60))
        weekly_filter = bool(cfg.get("weekly_rsi_filter_enabled", True))
        weekly_thresh = float(cfg.get("weekly_rsi_threshold",      60))
        # Sell-side filters: RSI must be BELOW the sell threshold (mirror)
        daily_thresh_sell  = float(cfg.get("daily_rsi_threshold_sell",  40))
        weekly_thresh_sell = float(cfg.get("weekly_rsi_threshold_sell", 40))

        close_price = candle.close
        now         = candle.close_time   # candle close time, not wall-clock time
        rec         = state_store.get(symbol)
        if not rec:
            return

        daily_rsi  = rsi_engine.get_rsi(symbol, "D")
        weekly_rsi = rsi_engine.get_rsi(symbol, "W")

        # ── SCAN TIMEFRAME ────────────────────────────────────────────────────
        if timeframe == scan_tf:
            rsi_scan = rsi_engine.get_rsi(symbol, scan_tf)
            if rsi_scan is None:
                return

            state_store.update_current_values(symbol,
                                               price=close_price,
                                               rsi_scan=rsi_scan)

            # ── Live daily-RSI stoploss on ACTIVE positions ─────────────────
            # Replaces the previous tick-based stoploss. Live D-RSI is computed
            # by applying ONE Wilder step to yesterday's D state using this
            # candle's close as today's would-be daily close. Fires only on
            # candle close, so intra-candle wicks that recover are ignored.
            if rec.state in (StockState.ACTIVE, StockState.ACTIVE_SELL):
                live_d = self._live_daily_rsi(rsi_engine, symbol, close_price)
                if live_d is not None:
                    if rec.state == StockState.ACTIVE:
                        sl_buy = float(cfg.get("daily_rsi_stoploss_buy", 48))
                        if live_d < sl_buy:
                            state_store.move_to_cooling_buy_stoploss(
                                symbol=symbol, exit_price=close_price,
                                daily_rsi=live_d, now=now
                            )
                            if webhook_sender:
                                webhook_sender.send_stoploss_exit(
                                    symbol       = symbol,
                                    plain_name   = rec.plain_name,
                                    company_name = rec.company_name,
                                    exit_price   = close_price,
                                    buy_price    = rec.buy_price or close_price,
                                    daily_rsi    = live_d,
                                    avg_count    = rec.avg_count,
                                )
                            return
                    else:   # ACTIVE_SELL
                        sl_sell = float(cfg.get("daily_rsi_stoploss_sell", 55))
                        if live_d > sl_sell:
                            state_store.move_to_cooling_sell_stoploss(
                                symbol=symbol, exit_price=close_price,
                                daily_rsi=live_d, now=now
                            )
                            if webhook_sender:
                                webhook_sender.send_sell_stoploss_exit(
                                    symbol       = symbol,
                                    plain_name   = rec.plain_name,
                                    company_name = rec.company_name,
                                    exit_price   = close_price,
                                    short_price  = rec.buy_price or close_price,
                                    daily_rsi    = live_d,
                                    avg_count    = rec.avg_count,
                                )
                            return

            # ── COOLING_BUY_STOPLOSS: release back to GENERAL when scan RSI
            # rallies past the stricter stoploss-cooling threshold.
            if rec.state == StockState.COOLING_BUY_STOPLOSS:
                cool_thr = float(cfg.get("stoploss_cooling_rsi_buy", 68))
                if rsi_scan > cool_thr:
                    state_store.reset_to_general(
                        symbol,
                        reason=f"Stoploss cooling cleared: "
                               f"RSI({scan_tf}m) {rsi_scan:.1f} > {cool_thr}")
                return   # never run entry/exit logic on a cooling stock

            # ── COOLING_SELL_STOPLOSS: release back to GENERAL when scan RSI
            # drops below the (lower) sell-side stoploss-cooling threshold.
            if rec.state == StockState.COOLING_SELL_STOPLOSS:
                cool_thr_sell = float(cfg.get("stoploss_cooling_rsi_sell", 32))
                if rsi_scan < cool_thr_sell:
                    state_store.reset_to_general(
                        symbol,
                        reason=f"Sell stoploss cooling cleared: "
                               f"RSI({scan_tf}m) {rsi_scan:.1f} < {cool_thr_sell}")
                return

            # ── GENERAL: Check for entry ───────────────────────────────────
            if rec.state == StockState.GENERAL:
                if rsi_scan < rsi_entry and self._eligible(symbol, "BUY"):
                    # Check daily filter
                    if daily_filter:
                        if daily_rsi is None or daily_rsi <= daily_thresh:
                            return
                    # Check weekly filter
                    if weekly_filter:
                        if weekly_rsi is None or weekly_rsi <= weekly_thresh:
                            return
                    state_store.move_to_watched(
                        symbol=symbol,
                        reference_price=close_price,
                        rsi_value=rsi_scan,
                        now=now
                    )

            # ── WATCHED: Reference price locked — check reset or buy ──────
            elif rec.state == StockState.WATCHED:

                # Reset: any close above reset threshold
                if rsi_scan > rsi_reset:
                    state_store.reset_to_general(
                        symbol, reason=f"RSI({scan_tf}m) {rsi_scan:.1f} > {rsi_reset}")
                    return

                # Reset: Daily RSI drops below threshold
                if daily_filter and daily_rsi is not None:
                    if daily_rsi < daily_thresh:
                        state_store.reset_to_general(
                            symbol, reason=f"Daily RSI {daily_rsi:.1f} < {daily_thresh}")
                        return

                # Reset: Weekly RSI drops below threshold
                if weekly_filter and weekly_rsi is not None:
                    if weekly_rsi < weekly_thresh:
                        state_store.reset_to_general(
                            symbol, reason=f"Weekly RSI {weekly_rsi:.1f} < {weekly_thresh}")
                        return

                # When trigger timeframe == scan timeframe, check buy trigger here
                if trigger_tf == scan_tf and rec.reference_price:
                    drop = ((rec.reference_price - close_price) / rec.reference_price) * 100
                    if drop >= drop_pct:
                        state_store.move_to_active_buy(
                            symbol=symbol, buy_price=close_price,
                            drop_pct=round(drop, 2), now=now
                        )
                        webhook_sender.send_buy(
                            symbol=symbol, plain_name=rec.plain_name,
                            company_name=rec.company_name,
                            buy_price=close_price,
                            reference_price=rec.reference_price,
                            drop_pct=round(drop, 2),
                            rsi_at_watch=rec.rsi_at_watch or 0
                        )

            # When trigger/exit timeframes == scan timeframe, handle ACTIVE state here
            if (trigger_tf == scan_tf or exit_tf == scan_tf) and rec.state == StockState.ACTIVE:
                # Avg check
                if trigger_tf == scan_tf and rec.last_avg_price:
                    avg_drop = ((rec.last_avg_price - close_price) / rec.last_avg_price) * 100
                    if avg_drop >= avg_pct:
                        state_store.add_avg(
                            symbol=symbol, avg_price=close_price,
                            drop_pct=round(avg_drop, 2), now=now
                        )
                        webhook_sender.send_avg(
                            symbol=symbol, plain_name=rec.plain_name,
                            company_name=rec.company_name,
                            avg_price=close_price,
                            prev_avg_price=rec.last_avg_price,
                            drop_pct=round(avg_drop, 2),
                            avg_num=rec.avg_count
                        )
                # Exit check: any close above exit threshold
                if exit_tf == scan_tf:
                    state_store.update_current_values(symbol, rsi_exit=rsi_scan)
                    if rsi_scan > rsi_exit_v:
                        state_store.move_to_exited(
                            symbol=symbol, exit_price=close_price,
                            rsi_value=rsi_scan, now=now
                        )
                        webhook_sender.send_exit(
                            symbol=symbol, plain_name=rec.plain_name,
                            company_name=rec.company_name,
                            exit_price=close_price,
                            buy_price=rec.buy_price or close_price,
                            rsi_at_exit=rsi_scan,
                            avg_count=rec.avg_count
                        )

            # ╔══════════════════════════════════════════════════════════════╗
            # ║ SELL-SIDE LOGIC (mirror of buy)                              ║
            # ╚══════════════════════════════════════════════════════════════╝
            # GENERAL → WATCHED_SELL when RSI > rsi_entry_sell
            if rec.state == StockState.GENERAL:
                if rsi_scan > rsi_entry_sell and self._eligible(symbol, "SELL"):
                    if daily_filter:
                        if daily_rsi is None or daily_rsi >= daily_thresh_sell:
                            return
                    if weekly_filter:
                        if weekly_rsi is None or weekly_rsi >= weekly_thresh_sell:
                            return
                    state_store.move_to_watched_sell(
                        symbol=symbol,
                        reference_price=close_price,
                        rsi_value=rsi_scan,
                        now=now
                    )

            elif rec.state == StockState.WATCHED_SELL:
                # Reset: RSI dropped below sell-reset threshold
                if rsi_scan < rsi_reset_sell:
                    state_store.reset_to_general(
                        symbol, reason=f"RSI({scan_tf}m) {rsi_scan:.1f} < {rsi_reset_sell}")
                    return
                # Reset on filters flipping
                if daily_filter and daily_rsi is not None and daily_rsi > daily_thresh_sell:
                    state_store.reset_to_general(
                        symbol, reason=f"Daily RSI {daily_rsi:.1f} > {daily_thresh_sell}")
                    return
                if weekly_filter and weekly_rsi is not None and weekly_rsi > weekly_thresh_sell:
                    state_store.reset_to_general(
                        symbol, reason=f"Weekly RSI {weekly_rsi:.1f} > {weekly_thresh_sell}")
                    return
                # SELL trigger when scan_tf == trigger_tf
                if trigger_tf == scan_tf and rec.reference_price:
                    rise = ((close_price - rec.reference_price) / rec.reference_price) * 100
                    if rise >= rise_pct:
                        state_store.move_to_active_sell(
                            symbol=symbol, short_price=close_price,
                            rise_pct=round(rise, 2), now=now
                        )
                        webhook_sender.send_sell(
                            symbol=symbol, plain_name=rec.plain_name,
                            company_name=rec.company_name,
                            short_price=close_price,
                            reference_price=rec.reference_price,
                            rise_pct=round(rise, 2),
                            rsi_at_watch=rec.rsi_at_watch or 0
                        )

            # When scan TF == trigger/exit TF, handle ACTIVE_SELL here too
            if (trigger_tf == scan_tf or exit_tf == scan_tf) and rec.state == StockState.ACTIVE_SELL:
                if trigger_tf == scan_tf and rec.last_avg_price:
                    avg_rise = ((close_price - rec.last_avg_price) / rec.last_avg_price) * 100
                    if avg_rise >= avg_rise_pct:
                        state_store.add_avg_sell(
                            symbol=symbol, avg_price=close_price,
                            rise_pct=round(avg_rise, 2), now=now
                        )
                        webhook_sender.send_sell_avg(
                            symbol=symbol, plain_name=rec.plain_name,
                            company_name=rec.company_name,
                            avg_price=close_price,
                            prev_avg_price=rec.last_avg_price,
                            rise_pct=round(avg_rise, 2),
                            avg_num=rec.avg_count
                        )
                if exit_tf == scan_tf and rsi_scan < rsi_exit_sell:
                    state_store.move_to_exited_sell(
                        symbol=symbol, exit_price=close_price,
                        rsi_value=rsi_scan, now=now
                    )
                    webhook_sender.send_sell_exit(
                        symbol=symbol, plain_name=rec.plain_name,
                        company_name=rec.company_name,
                        exit_price=close_price,
                        short_price=rec.buy_price or close_price,
                        rsi_at_exit=rsi_scan,
                        avg_count=rec.avg_count
                    )

        # ── TRIGGER TIMEFRAME — BUY + AVG ─────────────────────────────────────
        elif timeframe == trigger_tf:
            state_store.update_current_values(symbol, price=close_price)

            if rec.state == StockState.WATCHED and rec.reference_price:
                drop = ((rec.reference_price - close_price) / rec.reference_price) * 100
                if drop >= drop_pct:
                    state_store.move_to_active_buy(
                        symbol=symbol,
                        buy_price=close_price,
                        drop_pct=round(drop, 2),
                        now=now
                    )
                    webhook_sender.send_buy(
                        symbol=symbol, plain_name=rec.plain_name,
                        company_name=rec.company_name,
                        buy_price=close_price,
                        reference_price=rec.reference_price,
                        drop_pct=round(drop, 2),
                        rsi_at_watch=rec.rsi_at_watch or 0
                    )

            elif rec.state == StockState.ACTIVE and rec.last_avg_price:
                avg_drop = ((rec.last_avg_price - close_price) / rec.last_avg_price) * 100
                if avg_drop >= avg_pct:
                    state_store.add_avg(
                        symbol=symbol,
                        avg_price=close_price,
                        drop_pct=round(avg_drop, 2),
                        now=now
                    )
                    webhook_sender.send_avg(
                        symbol=symbol, plain_name=rec.plain_name,
                        company_name=rec.company_name,
                        avg_price=close_price,
                        prev_avg_price=rec.last_avg_price,
                        drop_pct=round(avg_drop, 2),
                        avg_num=rec.avg_count
                    )

            # SELL-side trigger / avg
            elif rec.state == StockState.WATCHED_SELL and rec.reference_price:
                rise = ((close_price - rec.reference_price) / rec.reference_price) * 100
                if rise >= rise_pct:
                    state_store.move_to_active_sell(
                        symbol=symbol, short_price=close_price,
                        rise_pct=round(rise, 2), now=now
                    )
                    webhook_sender.send_sell(
                        symbol=symbol, plain_name=rec.plain_name,
                        company_name=rec.company_name,
                        short_price=close_price,
                        reference_price=rec.reference_price,
                        rise_pct=round(rise, 2),
                        rsi_at_watch=rec.rsi_at_watch or 0
                    )

            elif rec.state == StockState.ACTIVE_SELL and rec.last_avg_price:
                avg_rise = ((close_price - rec.last_avg_price) / rec.last_avg_price) * 100
                if avg_rise >= avg_rise_pct:
                    state_store.add_avg_sell(
                        symbol=symbol, avg_price=close_price,
                        rise_pct=round(avg_rise, 2), now=now
                    )
                    webhook_sender.send_sell_avg(
                        symbol=symbol, plain_name=rec.plain_name,
                        company_name=rec.company_name,
                        avg_price=close_price,
                        prev_avg_price=rec.last_avg_price,
                        rise_pct=round(avg_rise, 2),
                        avg_num=rec.avg_count
                    )

        # ── EXIT TIMEFRAME ─────────────────────────────────────────────────────
        elif timeframe == exit_tf:
            rsi_exit_val = rsi_engine.get_rsi(symbol, exit_tf)
            if rsi_exit_val is None:
                return

            state_store.update_current_values(symbol, rsi_exit=rsi_exit_val)

            if rec.state == StockState.ACTIVE:
                if rsi_exit_val > rsi_exit_v:
                    state_store.move_to_exited(
                        symbol=symbol,
                        exit_price=close_price,
                        rsi_value=rsi_exit_val,
                        now=now
                    )
                    webhook_sender.send_exit(
                        symbol=symbol, plain_name=rec.plain_name,
                        company_name=rec.company_name,
                        exit_price=close_price,
                        buy_price=rec.buy_price or close_price,
                        rsi_at_exit=rsi_exit_val,
                        avg_count=rec.avg_count
                    )

            # SELL-side exit
            elif rec.state == StockState.ACTIVE_SELL:
                if rsi_exit_val < rsi_exit_sell:
                    state_store.move_to_exited_sell(
                        symbol=symbol,
                        exit_price=close_price,
                        rsi_value=rsi_exit_val,
                        now=now
                    )
                    webhook_sender.send_sell_exit(
                        symbol=symbol, plain_name=rec.plain_name,
                        company_name=rec.company_name,
                        exit_price=close_price,
                        short_price=rec.buy_price or close_price,
                        rsi_at_exit=rsi_exit_val,
                        avg_count=rec.avg_count
                    )

        # ── DAILY CANDLE CLOSE ─────────────────────────────────────────────────
        elif timeframe == "D":
            # RSI already updated by scanner_manager before this method is called.
            # Do NOT call rsi_engine.update here — that would apply Wilder's
            # smoothing twice per D candle, causing avg_gain/avg_loss to decay
            # with a phantom zero-change entry every trading day.
            if rec.state == StockState.WATCHED:
                d_rsi = rsi_engine.get_rsi(symbol, "D")
                if daily_filter and d_rsi is not None and d_rsi < daily_thresh:
                    state_store.reset_to_general(
                        symbol, reason=f"Daily RSI {d_rsi:.1f} < {daily_thresh}")

        # ── WEEKLY CANDLE CLOSE ────────────────────────────────────────────────
        elif timeframe == "W":
            # Same reason as D — do NOT call rsi_engine.update here.
            if rec.state == StockState.WATCHED:
                w_rsi = rsi_engine.get_rsi(symbol, "W")
                if weekly_filter and w_rsi is not None and w_rsi < weekly_thresh:
                    state_store.reset_to_general(
                        symbol, reason=f"Weekly RSI {w_rsi:.1f} < {weekly_thresh}")

    def on_tick(self, symbol, price, rsi_engine, state_store, webhook_sender=None):
        """
        Updates the live price for display only.

        Stoploss check moved to on_candle_close (scan_tf candles) so that
        intra-candle wicks that recover before close don't trigger false
        exits. This also makes live behavior bit-identical to historical
        replay through the shared replay engine.
        """
        state_store.update_current_values(symbol, price=price)

    @staticmethod
    def _live_daily_rsi(rsi_engine, symbol, current_price):
        """Apply one Wilder step to yesterday's D state using current_price
        as today's would-be close.  Returns None if no D calculator yet."""
        d_calcs = rsi_engine._calculators.get(symbol)
        if not d_calcs:
            return None
        calc = d_calcs.get("D")
        if calc is None or not calc.initialized or calc.rsi is None:
            return None
        closes = list(calc.closes)
        if not closes:
            return None
        last_close = closes[-1]
        diff = current_price - last_close
        gain = max(0.0,  diff)
        loss = max(0.0, -diff)
        p   = calc.period
        ag  = (calc.avg_gain * (p - 1) + gain) / p
        al  = (calc.avg_loss * (p - 1) + loss) / p
        if al == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    def on_market_open(self, state_store, rsi_engine):
        # Reset both buy- and sell-side EXITED stocks at market open.
        # COOLING_*_STOPLOSS deliberately stays — those require RSI recovery
        # not a calendar reset.
        exited      = state_store.get_exited() + state_store.get_exited_sells()
        for rec in exited:
            state_store.reset_to_general(rec.symbol, reason="New market day")
        print(f"🌅 Market open: {len(exited)} exited stocks reset.")

    def on_market_close(self, state_store):
        print(f"\n🔔 Market closed. {state_store.summary()}")
