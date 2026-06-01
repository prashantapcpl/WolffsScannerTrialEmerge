"""
carry_forward.py
Restores scanner state using RSI cache.

Gap-fill simulation:
- If scanner was off and buy/avg/exit should have fired
- Simulate those signals from stored candle history
- Fire webhooks with gap_fill=True flag
- Restore stock to correct current state
"""

import os
import sys
from datetime import datetime, timedelta
import pytz

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

IST = pytz.timezone("Asia/Kolkata")


class CarryForwardEngine:

    def __init__(self, rsi_cache, state_store, strategy_config):
        self.rsi_cache   = rsi_cache
        self.state_store = state_store
        self.config      = strategy_config

    def seed_prev_rsi(self, symbols: list):
        """No-op: strategy uses simple closes (not crossovers), so prev_rsi is not needed."""
        pass

    def _point_in_time_rsi(self, symbol: str, tf: str,
                           target_dt: datetime) -> float | None:
        """
        Return the RSI value for (symbol, tf) as it was at target_dt.
        For daily: latest D candle whose close (15:30 same day) <= target_dt.
        For weekly: latest W candle whose close (Fri 15:30) <= target_dt.
        Returns None if no completed bar exists by target_dt or no data.
        """
        if target_dt is None:
            return self.rsi_cache.get_last_rsi(symbol, tf)
        if target_dt.tzinfo is None:
            target_dt = IST.localize(target_dt)

        series = self.rsi_cache.get_rsi_series(symbol, tf)
        dates  = self.rsi_cache.get_datetimes(symbol, tf)
        if not series or not dates:
            return None

        # Walk in reverse: dates are open-times, we want close-time <= target.
        # Daily close = same-day 15:30 IST. Weekly close = Fri 15:30 of that week.
        for i in range(len(dates) - 1, -1, -1):
            try:
                open_dt = datetime.strptime(dates[i], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            open_dt = IST.localize(open_dt) if open_dt.tzinfo is None else open_dt
            if tf == "D":
                close_dt = open_dt.replace(hour=15, minute=30,
                                            second=0, microsecond=0)
            elif tf == "W":
                days_to_fri = (4 - open_dt.weekday()) % 7
                close_dt = (open_dt + timedelta(days=days_to_fri)).replace(
                    hour=15, minute=30, second=0, microsecond=0)
            else:
                # Should not be called with intraday TF; fall back
                close_dt = open_dt
            if close_dt <= target_dt:
                return series[i]
        return None

    def run(self, symbols: list, mapper,
            webhook_sender=None) -> int:
        """
        Main carry-forward + gap-fill.
        webhook_sender: if provided, fires webhooks for missed signals
        """
        cfg           = self.config
        scan_tf       = str(cfg.get("scan_timeframe",            "5"))
        trigger_tf    = str(cfg.get("trigger_timeframe",         "1"))
        exit_tf       = str(cfg.get("exit_timeframe",            "10"))
        rsi_entry     = float(cfg.get("rsi_entry_threshold",     20))
        rsi_reset     = float(cfg.get("rsi_reset_threshold",     70))
        rsi_exit_v    = float(cfg.get("rsi_exit_threshold",      68))
        rsi_period    = int(cfg.get("rsi_period",                14))
        drop_pct      = float(cfg.get("drop_percent",            2.0))
        avg_pct       = float(cfg.get("avg_drop_percent",        3.0))
        daily_filter  = bool(cfg.get("daily_rsi_filter_enabled", True))
        daily_thresh  = float(cfg.get("daily_rsi_threshold",     60))
        weekly_filter = bool(cfg.get("weekly_rsi_filter_enabled",True))
        weekly_thresh = float(cfg.get("weekly_rsi_threshold",    60))

        # Minutes per timeframe — mirrors CandleEngine
        _TF_MIN = {"1":1,"2":2,"5":5,"10":10,"15":15,"30":30,"60":60,"D":375,"W":1875}

        def _close_dt(open_dt, tf):
            """Return close time for a candle given its open datetime and timeframe."""
            return open_dt + timedelta(minutes=_TF_MIN.get(tf, 1))

        def _close_dt_str(open_dt_str, tf):
            """Convert candle open-time string → close-time string."""
            dt = parse_dt(open_dt_str)
            if dt is None:
                return open_dt_str
            return _close_dt(dt, tf).strftime("%Y-%m-%d %H:%M:%S")

        print(f"\n🔄 Running carry-forward + gap-fill...")
        print(f"   Scan TF: {scan_tf}m | Entry:<{rsi_entry} | Reset:>{rsi_reset} | Exit:>{rsi_exit_v}")

        restored  = 0
        gap_filled= 0
        checked   = 0

        # Bulk save coalescing — every move_to_*, add_avg, reset_to_general
        # below would otherwise trigger an atomic os.replace, fighting the
        # dashboard's read lock and producing the WinError 32 storm. Bulk
        # mode marks state dirty in-memory and flushes ONCE at the end.
        # Manual bulk_depth increment so we don't have to re-indent the
        # entire 400-line body; the matching decrement + force-flush sits
        # next to the final self.state_store.save() call.
        self.state_store._bulk_depth += 1

        # ── Helpers (defined once, reused across all symbols) ────────────────
        def is_market_hours(dt_str):
            try:
                dt = datetime.strptime(dt_str[:16], "%Y-%m-%d %H:%M")
                h, m = dt.hour, dt.minute
                return (h == 9 and m >= 15) or (10 <= h <= 14) or (h == 15 and m <= 30)
            except:
                return False

        def _outside_market(ts):
            """True if ts is a non-None timestamp that falls outside market hours."""
            if ts is None:
                return False
            try:
                if hasattr(ts, "hour"):
                    h, m = ts.hour, ts.minute
                else:
                    parsed = datetime.strptime(str(ts)[:16], "%Y-%m-%d %H:%M")
                    h, m = parsed.hour, parsed.minute
                return not ((h == 9 and m >= 15) or (10 <= h <= 14) or (h == 15 and m <= 30))
            except:
                return False

        # ── Ghost cleanup: reset states created outside market hours ────────
        ghosts_cleared = 0
        for symbol in symbols:
            rec = self.state_store.get(symbol)
            if not rec:
                continue
            if rec.state == "WATCHED" and _outside_market(rec.watched_at):
                self.state_store.reset_to_general(
                    symbol, reason="Ghost: watched_at outside market hours")
                ghosts_cleared += 1
            elif rec.state == "ACTIVE_BUY" and _outside_market(rec.buy_time):
                self.state_store.reset_to_general(
                    symbol, reason="Ghost: buy_time outside market hours")
                ghosts_cleared += 1
        if ghosts_cleared:
            print(f"   🧹 Ghost states cleared: {ghosts_cleared}")

        def parse_dt(s):
            if not s:
                return None
            try:
                dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
                return IST.localize(dt)
            except:
                return None

        def scan_exit_tf(after_dt_str):
            """
            Scan exit_tf RSI for the first crossover above rsi_exit_v
            that occurs after after_dt_str. Returns (exit_price, exit_time)
            or (None, None) if no crossover found.
            """
            ex_rsi_s   = self.rsi_cache.get_rsi_series(symbol, exit_tf)
            ex_close_s = self.rsi_cache.get_closes(symbol, exit_tf)
            ex_date_s  = self.rsi_cache.get_datetimes(symbol, exit_tf)
            if not ex_rsi_s:
                return None, None
            start = next(
                (k for k, d in enumerate(ex_date_s)
                 if _close_dt_str(d, exit_tf) > after_dt_str),
                len(ex_date_s)
            )
            prev_ex = None
            for ex_rsi, ex_close, ex_dt in zip(
                    ex_rsi_s[start:], ex_close_s[start:], ex_date_s[start:]):
                if not is_market_hours(ex_dt):
                    continue
                if ex_rsi is None:
                    continue
                if ex_rsi > rsi_exit_v and (prev_ex is None or prev_ex <= rsi_exit_v):
                    return ex_close, _close_dt_str(ex_dt, exit_tf)
                prev_ex = ex_rsi
            return None, None

        for symbol in symbols:
            rec = self.state_store.get(symbol)
            if not rec:
                continue

            # ACTIVE_BUY: check if exit fired while scanner was off
            if rec.state == "ACTIVE_BUY":
                if rec.buy_time:
                    # buy_time must be AFTER reference_time — if they match, it means
                    # run_rescan() used wall-clock time (datetime.now) for move_to_active_buy
                    # and it coincided with carry_forward's reference_time. Recover the real
                    # buy candle by scanning RSI closes after the reference candle.
                    if (rec.reference_time and rec.reference_price and
                            rec.buy_time == rec.reference_time):
                        ref_ts_str = rec.reference_time.strftime("%Y-%m-%d %H:%M:%S")
                        closes_s = self.rsi_cache.get_closes(symbol, scan_tf)
                        dt_s     = self.rsi_cache.get_datetimes(symbol, scan_tf)
                        for close, dt_str in zip(closes_s, dt_s):
                            if dt_str < ref_ts_str:
                                continue
                            if not is_market_hours(dt_str):
                                continue
                            if ((rec.reference_price - close) / rec.reference_price) * 100 >= drop_pct:
                                corrected = parse_dt(_close_dt_str(dt_str, scan_tf))
                                if corrected:
                                    rec.buy_time      = corrected
                                    rec.buy_signal_at = corrected
                                break

                    buy_ts = (rec.buy_time.strftime("%Y-%m-%d %H:%M:%S")
                              if hasattr(rec.buy_time, "strftime")
                              else str(rec.buy_time)[:19])

                    # Find exit first — needed to bound the avg scan below.
                    e_price, e_time = scan_exit_tf(buy_ts)

                    # Scan for avg entries that triggered while scanner was offline.
                    # Uses scan_tf closes as proxy (trigger_tf is often 1m which isn't
                    # in the cache). Starts from the last recorded avg time/price so
                    # already-known avgs aren't double-counted.
                    # Stops at exit_time — can't average into an already-closed position.
                    avg_ref_price = rec.last_avg_price or rec.buy_price
                    avg_ref_time  = rec.last_avg_time  or rec.buy_time
                    if avg_ref_price and avg_ref_time:
                        base_ts = (avg_ref_time.strftime("%Y-%m-%d %H:%M:%S")
                                   if hasattr(avg_ref_time, "strftime")
                                   else str(avg_ref_time)[:19])
                        sc_closes = self.rsi_cache.get_closes(symbol, scan_tf)
                        sc_dates  = self.rsi_cache.get_datetimes(symbol, scan_tf)
                        for sc_close, sc_dt in zip(sc_closes, sc_dates):
                            if sc_dt <= base_ts:
                                continue
                            if not is_market_hours(sc_dt):
                                continue
                            # Stop if this candle closes at or after the exit time
                            if e_time and _close_dt_str(sc_dt, scan_tf) >= e_time:
                                break
                            a_drop = ((avg_ref_price - sc_close) / avg_ref_price) * 100
                            if a_drop >= avg_pct:
                                avg_dt = parse_dt(_close_dt_str(sc_dt, scan_tf))
                                self.state_store.add_avg(
                                    symbol=symbol,
                                    avg_price=sc_close,
                                    drop_pct=round(a_drop, 2),
                                    now=avg_dt
                                )
                                if webhook_sender:
                                    webhook_sender.send_avg(
                                        symbol=symbol,
                                        plain_name=mapper.get_plain_name(symbol),
                                        company_name=rec.company_name,
                                        avg_price=sc_close,
                                        prev_avg_price=avg_ref_price,
                                        drop_pct=round(a_drop, 2),
                                        avg_num=rec.avg_count,
                                        gap_fill=True,
                                        signal_time=_close_dt_str(sc_dt, scan_tf)
                                    )
                                avg_ref_price = sc_close
                    if e_price is not None:
                        plain_name = mapper.get_plain_name(symbol)
                        exit_dt    = parse_dt(e_time)
                        self.state_store.move_to_exited(
                            symbol=symbol, exit_price=e_price,
                            rsi_value=rsi_exit_v, now=exit_dt
                        )
                        print(f"   📋 GAP-FILL EXIT (active buy missed): {plain_name} | "
                              f"Buy:₹{rec.buy_price:.2f} → Exit:₹{e_price:.2f} "
                              f"@ {e_time}")
                        gap_filled += 1
                        if webhook_sender:
                            webhook_sender.send_exit(
                                symbol=symbol, plain_name=plain_name,
                                company_name=rec.company_name,
                                exit_price=e_price, buy_price=rec.buy_price,
                                rsi_at_exit=rsi_exit_v,
                                avg_count=rec.avg_count,
                                gap_fill=True, signal_time=e_time
                            )
                continue

            # EXITED: re-verify exit time against the fresh RSI cache.
            # The live scanner may have recorded a wrong exit time when it was
            # seeded from incomplete data (e.g. missing intraday candles pushed
            # RSI above threshold at 15:00 instead of the real 12:xx crossover).
            # Use the stored buy_time to scan for the FIRST genuine crossover —
            # same logic as ACTIVE_BUY but without firing a webhook.
            if rec.state == "EXITED" and rec.buy_time:
                buy_ts = (rec.buy_time.strftime("%Y-%m-%d %H:%M:%S")
                          if hasattr(rec.buy_time, "strftime")
                          else str(rec.buy_time)[:19])
                e_price, e_time = scan_exit_tf(buy_ts)
                if e_price is not None:
                    existing = (rec.exit_time.strftime("%Y-%m-%d %H:%M:%S")
                               if rec.exit_time else "")
                    if e_time != existing:
                        plain_name = mapper.get_plain_name(symbol)
                        self.state_store.move_to_exited(
                            symbol=symbol, exit_price=e_price,
                            rsi_value=rsi_exit_v, now=parse_dt(e_time)
                        )
                        print(f"   🔧 EXIT CORRECTED: {plain_name} | "
                              f"₹{e_price:.2f} @ {e_time} "
                              f"(was {existing})")
                continue

            # Already WATCHED from saved state — verify filters still valid
            if rec.state == "WATCHED" and rec.reference_price:
                # Check if scan_tf RSI has reset above rsi_reset since watched_at.
                # If it has, the watch is stale — run full GENERAL replay instead.
                rsi_reset_since_watch = False
                if rec.watched_at:
                    try:
                        wt_str = (rec.watched_at.strftime("%Y-%m-%d %H:%M:%S")
                                  if hasattr(rec.watched_at, "strftime")
                                  else str(rec.watched_at)[:19])
                        rsi_s = self.rsi_cache.get_rsi_series(symbol, scan_tf)
                        dt_s  = self.rsi_cache.get_datetimes(symbol, scan_tf)
                        rsi_reset_since_watch = any(
                            r is not None and r > rsi_reset
                            for r, d in zip(rsi_s, dt_s) if d > wt_str
                        )
                    except Exception:
                        pass

                if not rsi_reset_since_watch:
                    valid = True
                    if daily_filter:
                        d_rsi = self.rsi_cache.get_last_rsi(symbol, "D")
                        if d_rsi is not None and d_rsi < daily_thresh:
                            self.state_store.reset_to_general(
                                symbol, reason=f"Daily RSI {d_rsi:.1f} < {daily_thresh}")
                            valid = False
                    if valid and weekly_filter:
                        w_rsi = self.rsi_cache.get_last_rsi(symbol, "W")
                        if w_rsi is not None and w_rsi < weekly_thresh:
                            self.state_store.reset_to_general(
                                symbol, reason=f"Weekly RSI {w_rsi:.1f} < {weekly_thresh}")
                            valid = False
                    if valid:
                        restored += 1
                    continue

                # RSI reset above rsi_reset since watched_at — stale watch, re-run from scratch
                self.state_store.reset_to_general(
                    symbol, reason="RSI reset above threshold since watched_at")
                # Fall through to GENERAL replay below

            # GENERAL state — full replay
            rsi_series = self.rsi_cache.get_rsi_series(symbol, scan_tf)
            closes     = self.rsi_cache.get_closes(symbol, scan_tf)
            date_strs  = self.rsi_cache.get_datetimes(symbol, scan_tf)

            if len(rsi_series) < rsi_period + 2:
                continue

            checked += 1

            # ── Replay RSI as a real state-machine ─────────────────────────
            # OLD bug: this loop only tracked WATCH and RESET. If a stock
            # got WATCH at T1, BUY at T2 (drop% reached), then RSI later
            # crossed rsi_reset at T3, the OLD loop wrongly RESET watch
            # and FORGOT about the BUY -- losing the whole T1..exit cycle.
            # Also: the old code BROKE at the first buy, so any later
            # cycle (watch→buy→exit→new_watch) was missed.
            #
            # FIX (2026-05-31): delegate the cycle walk to the 8-scenario-
            # tested state machine in core.carry_forward_state_machine.
            # It returns the LATEST in-progress state across the full
            # series. We map its result back to the watch_*/preres_buy_*
            # variables the downstream gap-fill code expects.
            from core.carry_forward_state_machine import replay_cycles

            # Pre-compute exit events from exit_tf so the state machine
            # can close ACTIVE positions when an exit-tf RSI crossover
            # above rsi_exit_threshold occurs after the buy time.
            _exit_rsis  = self.rsi_cache.get_rsi_series(symbol, exit_tf) or []
            _exit_cls   = self.rsi_cache.get_closes(symbol, exit_tf) or []
            _exit_dts   = self.rsi_cache.get_datetimes(symbol, exit_tf) or []
            exit_events = [
                (str(d), float(c))
                for r, c, d in zip(_exit_rsis, _exit_cls, _exit_dts)
                if r is not None and r > rsi_exit_v
            ]

            _str_dts = [str(d) for d in date_strs]
            sm = replay_cycles(
                rsi_series  = rsi_series, closes = closes, date_strs = _str_dts,
                exit_events = exit_events,
                rsi_entry   = rsi_entry, rsi_reset = rsi_reset,
                drop_pct    = drop_pct, avg_pct   = avg_pct,
            )

            # Map state-machine result → variables downstream code expects.
            watch_price      = None
            watch_rsi        = None
            watch_time       = None
            watch_idx        = None
            preres_buy_price = None
            preres_buy_time  = None
            preres_buy_idx   = None

            if sm.final_state in ("watched", "active") and sm.ref_time:
                watch_price = sm.ref_price
                watch_time  = sm.ref_time
                # Find the index of the watch in the original date_strs
                try:
                    watch_idx = _str_dts.index(sm.ref_time)
                    if watch_idx < len(rsi_series):
                        watch_rsi = rsi_series[watch_idx]
                except ValueError:
                    watch_idx = None

            if sm.final_state == "active" and sm.buy_time:
                preres_buy_price = sm.buy_price
                preres_buy_time  = sm.buy_time
                try:
                    preres_buy_idx = _str_dts.index(sm.buy_time)
                except ValueError:
                    preres_buy_idx = None

            if watch_price is None or watch_idx is None:
                continue

            # Check daily/weekly filters — point-in-time at watch_time, NOT
            # today's RSI. Previously used get_last_rsi which made historical
            # watch entries get rejected/accepted under today's D/W RSI
            # instead of what they actually were at the moment of the watch.
            _watch_dt = parse_dt(watch_time)
            if daily_filter:
                d_rsi = self._point_in_time_rsi(symbol, "D", _watch_dt)
                if d_rsi is not None and d_rsi < daily_thresh:
                    continue
            if weekly_filter:
                w_rsi = self._point_in_time_rsi(symbol, "W", _watch_dt)
                if w_rsi is not None and w_rsi < weekly_thresh:
                    continue

            # ── Gap-fill: check if buy/avg/exit happened after watch entry ──
            plain_name = mapper.get_plain_name(symbol)

            # Find if drop happened. Seed with the pre-resolved BUY found
            # during the state-machine walk above (handles the WATCH→BUY→
            # high-RSI case where the OLD code lost the entire cycle).
            buy_price   = preres_buy_price
            buy_time    = preres_buy_time
            buy_idx     = preres_buy_idx
            avg_entries = []
            last_avg_price = buy_price   # avg distances measure from BUY price
            exit_price  = None
            exit_time   = None
            exited      = False

            candles_after = closes[watch_idx:]
            times_after   = date_strs[watch_idx:]

            for j, (close, dt_str) in enumerate(zip(candles_after, times_after)):
                actual_idx = watch_idx + j

                # Skip non-market-hours candles
                if not is_market_hours(dt_str):
                    continue

                if buy_price is None:
                    # Check buy trigger
                    drop = ((watch_price - close) / watch_price) * 100
                    if drop >= drop_pct:
                        buy_price      = close
                        buy_time       = _close_dt_str(dt_str, scan_tf)
                        buy_idx        = actual_idx
                        last_avg_price = close
                else:
                    # Check avg trigger
                    avg_drop = ((last_avg_price - close) / last_avg_price) * 100
                    if avg_drop >= avg_pct:
                        avg_entries.append({
                            "avg_num":     len(avg_entries) + 1,
                            "price":       close,
                            "drop_pct":    round(avg_drop, 2),
                            "signal_time": _close_dt_str(dt_str, scan_tf)
                        })
                        last_avg_price = close

            # ── Exit detection using exit_tf RSI (correct timeframe) ───────
            if buy_price is not None:
                exit_price, exit_time = scan_exit_tf(buy_time)
                exited = exit_price is not None
                # Trim avg entries at or after the exit — can't average into a
                # position that was already closed by the time those candles fired.
                if exited and exit_time:
                    avg_entries = [a for a in avg_entries
                                   if a["signal_time"] < exit_time]
                    for k, a in enumerate(avg_entries):
                        a["avg_num"] = k + 1

            # ── Determine final state and restore ─────────────────────────
            if buy_price is None:
                # No buy happened — restore to watched (gap_fill: RSI was missed)
                watch_dt = parse_dt(watch_time)
                self.state_store.move_to_watched(
                    symbol=symbol, reference_price=watch_price,
                    rsi_value=watch_rsi, now=watch_dt, gap_fill=True
                )
                restored += 1

            elif exited:
                # Full cycle completed while off — restore as EXITED
                # Will reset to GENERAL on next market open
                buy_dt  = parse_dt(buy_time)
                exit_dt = parse_dt(exit_time)

                # Intermediate transitions (not final state — no gap_fill badge)
                self.state_store.move_to_watched(
                    symbol=symbol, reference_price=watch_price,
                    rsi_value=watch_rsi, now=parse_dt(watch_time)
                )
                self.state_store.move_to_active_buy(
                    symbol=symbol, buy_price=buy_price,
                    drop_pct=round(((watch_price-buy_price)/watch_price)*100,2),
                    now=buy_dt
                )
                # Add avg entries
                rec2 = self.state_store.get(symbol)
                if rec2:
                    from core.state_store import AvgEntry
                    for avg in avg_entries:
                        rec2.avg_entries.append(
                            AvgEntry(avg["avg_num"], avg["price"],
                                     avg["drop_pct"], parse_dt(avg["signal_time"]))
                        )
                        rec2.avg_count      = avg["avg_num"]
                        rec2.last_avg_price = avg["price"]

                self.state_store.move_to_exited(
                    symbol=symbol, exit_price=exit_price,
                    rsi_value=rsi_exit_v, now=exit_dt
                )

                print(f"   📋 GAP-FILL EXIT: {plain_name} | "
                      f"Buy:₹{buy_price:.2f} → Exit:₹{exit_price:.2f} "
                      f"(while scanner was off)")
                gap_filled += 1

                # Fire webhook with gap_fill flag
                if webhook_sender:
                    webhook_sender.send_exit(
                        symbol=symbol, plain_name=plain_name,
                        company_name=rec.company_name,
                        exit_price=exit_price, buy_price=buy_price,
                        rsi_at_exit=rsi_exit_v, avg_count=len(avg_entries),
                        gap_fill=True, signal_time=exit_time
                    )

            else:
                # Buy happened, not yet exited — restore to ACTIVE_BUY
                buy_dt = parse_dt(buy_time)

                # Intermediate watched transition (not the final state)
                self.state_store.move_to_watched(
                    symbol=symbol, reference_price=watch_price,
                    rsi_value=watch_rsi, now=parse_dt(watch_time)
                )
                # Final state: ACTIVE_BUY with gap_fill badge
                self.state_store.move_to_active_buy(
                    symbol=symbol, buy_price=buy_price,
                    drop_pct=round(((watch_price-buy_price)/watch_price)*100,2),
                    now=buy_dt, gap_fill=True
                )

                # Add avg entries to record
                rec2 = self.state_store.get(symbol)
                if rec2 and avg_entries:
                    from core.state_store import AvgEntry
                    for avg in avg_entries:
                        rec2.avg_entries.append(
                            AvgEntry(avg["avg_num"], avg["price"],
                                     avg["drop_pct"], parse_dt(avg["signal_time"]))
                        )
                        rec2.avg_count      = avg["avg_num"]
                        rec2.last_avg_price = avg["price"]

                print(f"   📋 GAP-FILL BUY: {plain_name} | "
                      f"Ref:₹{watch_price:.2f} → Buy:₹{buy_price:.2f} "
                      f"@ {buy_time} | Avgs: {len(avg_entries)} "
                      f"(MISSED — scanner was off)")
                gap_filled += 1

                # Fire BUY webhook with gap_fill flag
                if webhook_sender:
                    webhook_sender.send_buy(
                        symbol=symbol, plain_name=plain_name,
                        company_name=rec.company_name,
                        buy_price=buy_price, reference_price=watch_price,
                        drop_pct=round(((watch_price-buy_price)/watch_price)*100,2),
                        rsi_at_watch=watch_rsi or 0,
                        gap_fill=True, signal_time=buy_time
                    )
                    # Fire avg webhooks
                    prev_price = buy_price
                    for avg in avg_entries:
                        webhook_sender.send_avg(
                            symbol=symbol, plain_name=plain_name,
                            company_name=rec.company_name,
                            avg_price=avg["price"],
                            prev_avg_price=prev_price,
                            drop_pct=avg["drop_pct"],
                            avg_num=avg["avg_num"],
                            gap_fill=True, signal_time=avg["signal_time"]
                        )
                        prev_price = avg["price"]

                restored += 1

        # Exit bulk mode → triggers one atomic flush instead of N writes.
        self.state_store._bulk_depth = max(0, self.state_store._bulk_depth - 1)
        self.state_store.save_now()

        print(f"\n✅ Carry-forward complete:")
        print(f"   Watched restored : {restored}")
        print(f"   Gap-filled       : {gap_filled} (missed while scanner was off)\n")
        return restored + gap_filled
