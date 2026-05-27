"""
backtester/engine.py

Architecture: Load everything into RAM once, then run all combinations
against in-memory data. No disk reads during backtest run.

Steps:
1. preload() — loads all candles + builds RSI series for all symbols
2. run_grid() — runs all parameter combinations against preloaded data
               Uses multiprocessing (symbol-chunk parallelism) for speed.
               Symbols are split across CPU cores; each core runs all combos
               against its chunk. Results are merged in the main process.
"""

import os
import sys
from datetime import datetime, timedelta
from itertools import product
import pytz

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, ROOT)

IST = pytz.timezone("Asia/Kolkata")


def to_dt(val):
    """Convert anything to timezone-aware datetime."""
    if val is None:
        return None
    if isinstance(val, str):
        try:
            val = datetime.strptime(val[:19], "%Y-%m-%d %H:%M:%S")
        except:
            return None
    if isinstance(val, datetime):
        return val if val.tzinfo else IST.localize(val)
    return None


# ─── Trade ─────────────────────────────────────────────────────────────────────
class Trade:
    __slots__ = ["symbol", "plain_name", "entry_price", "entry_time",
                 "ref_price", "rsi_at_entry", "avg_prices", "avg_times",
                 "exit_price", "exit_time", "rsi_at_exit", "is_open",
                 "_prev_exit_rsi"]

    def __init__(self, symbol, plain_name, entry_price, entry_time,
                 ref_price, rsi_at_entry):
        self.symbol        = symbol
        self.plain_name    = plain_name
        self.entry_price   = entry_price
        self.entry_time    = entry_time
        self.ref_price     = ref_price
        self.rsi_at_entry  = rsi_at_entry
        self.avg_prices    = []
        self.avg_times     = []
        self.exit_price    = None
        self.exit_time     = None
        self.rsi_at_exit   = None
        self.is_open       = True
        self._prev_exit_rsi= None

    @property
    def avg_count(self):
        return len(self.avg_prices)

    @property
    def avg_cost(self):
        prices = [self.entry_price] + self.avg_prices
        return sum(prices) / len(prices)

    @property
    def pnl_pct(self):
        if self.exit_price is None or self.is_open:
            return None
        return round((self.exit_price - self.avg_cost) / self.avg_cost * 100, 2)

    @property
    def duration_days(self):
        if self.entry_time and self.exit_time:
            et = (self.entry_time if isinstance(self.entry_time, datetime)
                  else datetime.strptime(str(self.entry_time)[:19], "%Y-%m-%d %H:%M:%S"))
            xt = (self.exit_time if isinstance(self.exit_time, datetime)
                  else datetime.strptime(str(self.exit_time)[:19], "%Y-%m-%d %H:%M:%S"))
            return round((xt - et).total_seconds() / 86400, 1)
        return None

    def to_dict(self):
        return {
            "symbol":        self.symbol,
            "plain_name":    self.plain_name,
            "entry_price":   round(self.entry_price, 2),
            "entry_time":    str(self.entry_time)[:16] if self.entry_time else None,
            "ref_price":     round(self.ref_price, 2),
            "rsi_at_entry":  round(self.rsi_at_entry, 2) if self.rsi_at_entry else None,
            "avg_count":     self.avg_count,
            "avg_prices":    [round(p, 2) for p in self.avg_prices],
            "avg_cost":      round(self.avg_cost, 2),
            "exit_price":    round(self.exit_price, 2) if self.exit_price else None,
            "exit_time":     str(self.exit_time)[:16] if self.exit_time else None,
            "rsi_at_exit":   round(self.rsi_at_exit, 2) if self.rsi_at_exit else None,
            "pnl_pct":       self.pnl_pct,
            "duration_days": self.duration_days,
            "is_open":       self.is_open,
        }


# ─── Result ────────────────────────────────────────────────────────────────────
class BacktestResult:
    def __init__(self, params, trades, period="full"):
        self.params  = params
        self.period  = period
        self.trades  = trades
        self.closed  = [t for t in trades
                        if not t.is_open and t.pnl_pct is not None]
        self.open    = [t for t in trades if t.is_open]

    @property
    def total_trades(self):  return len(self.closed)

    @property
    def wins(self):   return [t for t in self.closed if t.pnl_pct > 0]

    @property
    def losses(self): return [t for t in self.closed if t.pnl_pct <= 0]

    @property
    def win_rate(self):
        return round(len(self.wins)/len(self.closed)*100, 1) if self.closed else 0

    @property
    def avg_pnl(self):
        p = [t.pnl_pct for t in self.closed]
        return round(sum(p)/len(p), 2) if p else 0

    @property
    def avg_win(self):
        p = [t.pnl_pct for t in self.wins]
        return round(sum(p)/len(p), 2) if p else 0

    @property
    def avg_loss(self):
        p = [t.pnl_pct for t in self.losses]
        return round(sum(p)/len(p), 2) if p else 0

    @property
    def expectancy(self):
        if not self.closed: return 0
        wr = len(self.wins)/len(self.closed)
        lr = len(self.losses)/len(self.closed)
        return round(wr*self.avg_win + lr*self.avg_loss, 2)

    @property
    def profit_factor(self):
        gain = sum(t.pnl_pct for t in self.wins)
        loss = abs(sum(t.pnl_pct for t in self.losses))
        if loss == 0: return 999 if gain > 0 else 0
        return round(gain/loss, 2)

    @property
    def max_drawdown(self):
        losses = [t.pnl_pct for t in self.losses]
        return round(min(losses), 2) if losses else 0

    @property
    def best_trade(self):
        p = [t.pnl_pct for t in self.closed]
        return round(max(p), 2) if p else 0

    @property
    def worst_trade(self):
        p = [t.pnl_pct for t in self.closed]
        return round(min(p), 2) if p else 0

    @property
    def avg_avgs(self):
        if not self.closed: return 0
        return round(sum(t.avg_count for t in self.closed)/len(self.closed), 1)

    @property
    def avg_duration(self):
        d = [t.duration_days for t in self.closed if t.duration_days is not None]
        return round(sum(d)/len(d), 1) if d else 0

    @property
    def score(self):
        if self.total_trades < 20: return -999
        if self.profit_factor <= 1: return -999
        return round(self.expectancy * self.profit_factor, 3)

    def summary_dict(self):
        p = self.params
        return {
            "Period":   self.period,
            "Scan TF":  f"{p.get('scan_timeframe')}m",
            "Exit TF":  f"{p.get('exit_timeframe')}m",
            "RSI In":   p.get("rsi_entry_threshold"),
            "RSI Rst":  p.get("rsi_reset_threshold"),
            "RSI Exit": p.get("rsi_exit_threshold"),
            "Drop%":    p.get("drop_percent"),
            "Avg%":     p.get("avg_drop_percent"),
            "D.Flt":    "Y" if p.get("daily_rsi_filter_enabled") else "N",
            "W.Flt":    "Y" if p.get("weekly_rsi_filter_enabled") else "N",
            "Trades":   self.total_trades,
            "Open":     len(self.open),
            "Win%":     f"{self.win_rate}%",
            "Expect":   f"{self.expectancy:+.2f}%",
            "PF":       self.profit_factor,
            "Avg P&L":  f"{self.avg_pnl:+.2f}%",
            "AvgWin":   f"{self.avg_win:+.2f}%",
            "AvgLoss":  f"{self.avg_loss:+.2f}%",
            "Best":     f"{self.best_trade:+.2f}%",
            "Worst":    f"{self.worst_trade:+.2f}%",
            "MaxDD":    f"{self.max_drawdown:.2f}%",
            "AvgAvgs":  self.avg_avgs,
            "AvgDays":  self.avg_duration,
            "Score":    self.score,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Module-level functions for multiprocessing workers
# These must be at module level (not inside a class) so they can be pickled
# on Windows (which uses "spawn" to create worker processes).
# ══════════════════════════════════════════════════════════════════════════════

# Worker-global data store — populated once per worker process via _worker_init
_W: dict = {}


def _bsearch_rsi(series: list, target_dt) -> float | None:
    """Binary search for RSI value at or before target_dt. O(log n)."""
    if not series:
        return None
    lo, hi, idx = 0, len(series) - 1, -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= target_dt:
            idx = mid
            lo  = mid + 1
        else:
            hi  = mid - 1
    return series[idx][3] if idx >= 0 else None


def _htf_rsi(rsi_map: dict, date_str: str) -> float | None:
    """Daily/weekly RSI lookup — falls back up to 10 calendar days.
    Parses date_str only when the exact date is missing (weekends/holidays).
    """
    val = rsi_map.get(date_str)
    if val is not None:
        return val
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    for d in range(1, 11):
        prev = (dt - timedelta(days=d)).strftime("%Y-%m-%d")
        val  = rsi_map.get(prev)
        if val is not None:
            return val
    return None


def _run_for_params(params: dict, tf_slice: float, slice_end: bool) -> list:
    """
    Inner backtest loop. Uses worker-global _W data.
    Returns list of Trade objects (only closed trades).
    """
    scan_tf   = str(params["scan_timeframe"])
    exit_tf   = str(params["exit_timeframe"])
    rsi_entry = float(params["rsi_entry_threshold"])
    rsi_reset = float(params["rsi_reset_threshold"])
    rsi_exit  = float(params["rsi_exit_threshold"])
    drop_pct  = float(params["drop_percent"])
    avg_pct   = float(params["avg_drop_percent"])
    daily_on  = bool(params.get("daily_rsi_filter_enabled",  False))
    weekly_on = bool(params.get("weekly_rsi_filter_enabled", False))
    daily_th  = float(params.get("daily_rsi_threshold",  60))
    weekly_th = float(params.get("weekly_rsi_threshold", 60))

    rsi_series  = _W["rsi_series"]
    daily_rsi   = _W["daily_rsi"]
    weekly_rsi  = _W["weekly_rsi"]
    symbols     = _W["symbols"]
    plain_names = _W["plain_names"]

    all_trades = []

    for symbol in symbols:
        series = rsi_series.get(symbol, {}).get(scan_tf)
        if not series or len(series) < 19:
            continue

        if tf_slice < 1.0:
            n = int(len(series) * tf_slice)
            series = series[:n] if slice_end else series[n:]
            if not series:
                continue

        daily_map  = daily_rsi.get(symbol,  {})
        weekly_map = weekly_rsi.get(symbol, {})
        plain_name = plain_names.get(symbol, symbol)
        ex_series  = (rsi_series.get(symbol, {}).get(exit_tf, [])
                      if exit_tf != scan_tf else None)

        watch_price    = None
        watch_rsi      = None
        current_trade  = None
        last_avg_price = None

        for (dt, ds, close, rsi, _) in series:
            if rsi is None:
                continue

            if current_trade:
                exit_rsi_val = (rsi if exit_tf == scan_tf
                                else (_bsearch_rsi(ex_series, dt) or rsi))

                if exit_rsi_val > rsi_exit:
                    current_trade.exit_price  = close
                    current_trade.exit_time   = dt
                    current_trade.rsi_at_exit = exit_rsi_val
                    current_trade.is_open     = False
                    all_trades.append(current_trade)
                    current_trade = last_avg_price = watch_price = watch_rsi = None
                    continue

                if last_avg_price:
                    d = ((last_avg_price - close) / last_avg_price) * 100
                    if d >= avg_pct:
                        current_trade.avg_prices.append(close)
                        current_trade.avg_times.append(dt)
                        last_avg_price = close
                continue

            if watch_price is None:
                if rsi < rsi_entry:
                    if daily_on:
                        dr = _htf_rsi(daily_map, ds)
                        if dr is not None and dr <= daily_th:
                            continue
                    if weekly_on:
                        wr = _htf_rsi(weekly_map, ds)
                        if wr is not None and wr <= weekly_th:
                            continue
                    watch_price = close
                    watch_rsi   = rsi
            else:
                if rsi > rsi_reset:
                    watch_price = watch_rsi = None
                    continue
                if daily_on:
                    dr = _htf_rsi(daily_map, ds)
                    if dr is not None and dr < daily_th:
                        watch_price = watch_rsi = None
                        continue
                if weekly_on:
                    wr = _htf_rsi(weekly_map, ds)
                    if wr is not None and wr < weekly_th:
                        watch_price = watch_rsi = None
                        continue
                drop = ((watch_price - close) / watch_price) * 100
                if drop >= drop_pct:
                    current_trade  = Trade(symbol, plain_name, close, dt,
                                           watch_price, watch_rsi)
                    last_avg_price = close
                    watch_price    = watch_rsi = None

    return all_trades


def _symbol_chunk_task(args: tuple) -> list:
    """
    Worker entry point. Receives a symbol chunk and runs ALL param combos
    against that chunk. Returns list of (combo_idx, full, train, test).

    Must be a top-level function for Windows multiprocessing (spawn).
    """
    global _W
    chunk_data, all_combos, walk_forward = args
    _W = chunk_data  # populate worker-global data for this chunk

    results = []
    for idx, params in enumerate(all_combos):
        full  = _run_for_params(params, 1.0, True)
        train = _run_for_params(params, 0.7, True)  if walk_forward else []
        test  = _run_for_params(params, 0.7, False) if walk_forward else []
        results.append((idx, full, train, test))
    return results


# ─── Engine ────────────────────────────────────────────────────────────────────
class BacktestEngine:

    def __init__(self, data_store, mapper, symbols):
        self.data_store = data_store
        self.mapper     = mapper
        self.symbols    = symbols

        # Preloaded data — populated by preload()
        self._candles     = {}   # symbol → tf → [{"dt":dt,"o":o,"h":h,"l":l,"c":c}]
        self._rsi_series  = {}   # symbol → tf → [(dt, date_str, close, rsi, prev_rsi)]
        self._daily_rsi   = {}   # symbol → date_str → rsi
        self._weekly_rsi  = {}   # symbol → date_str → rsi
        self._preloaded   = False

    # ── Step 1: Preload everything into RAM ────────────────────────────────────
    def preload(self, timeframes: list, rsi_period: int = 14,
                progress_callback=None):
        """
        Load all RSI series into RAM.
        Fast path: reads from rsi_cache.json (one file, ~30s).
        Fallback:  reads individual CSVs and computes RSI (~30 min).
        """
        from core.rsi_cache import get_rsi_cache
        rsi_cache = get_rsi_cache()

        if rsi_cache.load():
            self._preload_from_cache(timeframes, rsi_cache, progress_callback)
        else:
            print("⚠️  RSI cache not found — falling back to CSV preload (slow)")
            self._preload_from_csv(timeframes, rsi_period, progress_callback)

    def _preload_from_cache(self, timeframes: list, rsi_cache,
                            progress_callback=None):
        """Fast preload: reshape RSI cache data into engine format.

        Series tuples store datetime strings, not datetime objects, so
        pickling tasks to worker processes is fast (no datetime overhead).
        _bsearch_rsi / _htf_rsi work correctly with ISO-format strings.
        """
        total = len(self.symbols)
        print(f"\n📥 Loading from RSI cache: {total} symbols × {timeframes}...")

        intraday_tfs = [tf for tf in timeframes if tf not in ("D", "W")]

        for i, symbol in enumerate(self.symbols):
            self._rsi_series[symbol] = {}
            self._daily_rsi[symbol]  = {}
            self._weekly_rsi[symbol] = {}

            # ── Intraday RSI series ────────────────────────────────────────
            for tf in intraday_tfs:
                rsi_vals  = rsi_cache.get_rsi_series(symbol, tf)
                closes    = rsi_cache.get_closes(symbol, tf)
                datetimes = rsi_cache.get_datetimes(symbol, tf)
                if not rsi_vals or not closes or not datetimes:
                    continue

                series   = []
                prev_rsi = None
                for dt_str, close, rsi in zip(datetimes, closes, rsi_vals):
                    series.append((dt_str, dt_str[:10], close, rsi, prev_rsi))
                    if rsi is not None:
                        prev_rsi = rsi
                self._rsi_series[symbol][tf] = series

            # ── Daily RSI series (also needed for scan/exit TF) ───────────
            d_rsi_vals  = rsi_cache.get_rsi_series(symbol, "D")
            d_closes    = rsi_cache.get_closes(symbol, "D")
            d_datetimes = rsi_cache.get_datetimes(symbol, "D")
            if d_rsi_vals and d_closes and d_datetimes:
                if "D" in timeframes:
                    series   = []
                    prev_rsi = None
                    for dt_str, close, rsi in zip(d_datetimes, d_closes, d_rsi_vals):
                        series.append((dt_str, dt_str[:10], close, rsi, prev_rsi))
                        if rsi is not None:
                            prev_rsi = rsi
                    self._rsi_series[symbol]["D"] = series

                last_rsi = None
                for dt_str, rsi in zip(d_datetimes, d_rsi_vals):
                    if rsi is not None:
                        last_rsi = rsi
                    self._daily_rsi[symbol][dt_str[:10]] = last_rsi

            # ── Weekly RSI map ─────────────────────────────────────────────
            # Store only actual week-start entries; _htf_rsi handles lookback
            # for intra-week dates (no need to fill-forward every calendar day).
            w_rsi_vals  = rsi_cache.get_rsi_series(symbol, "W")
            w_closes    = rsi_cache.get_closes(symbol, "W")
            w_datetimes = rsi_cache.get_datetimes(symbol, "W")
            if w_rsi_vals and w_datetimes:
                if "W" in timeframes and w_closes:
                    series   = []
                    prev_rsi = None
                    for dt_str, close, rsi in zip(w_datetimes, w_closes, w_rsi_vals):
                        series.append((dt_str, dt_str[:10], close, rsi, prev_rsi))
                        if rsi is not None:
                            prev_rsi = rsi
                    self._rsi_series[symbol]["W"] = series

                last_rsi = None
                for dt_str, rsi in zip(w_datetimes, w_rsi_vals):
                    if rsi is not None:
                        last_rsi = rsi
                    self._weekly_rsi[symbol][dt_str[:10]] = last_rsi

            if progress_callback:
                progress_callback(i + 1, total, "preload")

        self._preloaded = True
        print(f"✅ Preload from cache complete: {total} symbols ready in RAM.\n")

    def _preload_from_csv(self, timeframes: list, rsi_period: int = 14,
                          progress_callback=None):
        """Slow fallback: reads individual CSV files and computes RSI from scratch."""
        from core.rsi_engine import RSICalculator

        total = len(self.symbols)
        print(f"\n📥 Preloading from CSVs: {total} symbols × {timeframes} ...")

        for i, symbol in enumerate(self.symbols):
            self._candles[symbol]    = {}
            self._rsi_series[symbol] = {}

            for tf in timeframes:
                raw = self.data_store.load_candles(symbol, tf)
                if not raw:
                    continue

                candles = []
                for c in raw:
                    dt = to_dt(c["datetime"])
                    if dt:
                        candles.append({"dt": dt, "c": c["close"],
                                        "ds": dt.strftime("%Y-%m-%d")})
                self._candles[symbol][tf] = candles

                calc     = RSICalculator(period=rsi_period)
                series   = []
                prev_rsi = None
                for c in candles:
                    rsi = calc.update(c["c"])
                    series.append((c["dt"], c["ds"], c["c"], rsi, prev_rsi))
                    if rsi is not None:
                        prev_rsi = rsi
                self._rsi_series[symbol][tf] = series

            self._daily_rsi[symbol] = {}
            daily_raw = self.data_store.load_candles(symbol, "D")
            if daily_raw:
                calc     = RSICalculator(period=rsi_period)
                last_rsi = None
                for c in daily_raw:
                    rsi = calc.update(c["close"])
                    if rsi is not None:
                        last_rsi = rsi
                    dt = to_dt(c["datetime"])
                    if dt:
                        self._daily_rsi[symbol][dt.strftime("%Y-%m-%d")] = last_rsi

            self._weekly_rsi[symbol] = {}
            weekly_raw = self.data_store.load_candles(symbol, "W")
            if not weekly_raw:
                weekly_raw = daily_raw
            if weekly_raw:
                calc     = RSICalculator(period=rsi_period)
                last_rsi = None
                date_rsi = {}
                for c in weekly_raw:
                    rsi = calc.update(c["close"])
                    if rsi is not None:
                        last_rsi = rsi
                    dt = to_dt(c["datetime"])
                    if dt:
                        date_rsi[dt.strftime("%Y-%m-%d")] = last_rsi
                if date_rsi:
                    sorted_dates = sorted(date_rsi.keys())
                    start = datetime.strptime(sorted_dates[0], "%Y-%m-%d")
                    end   = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
                    cur   = start
                    last_rsi = None
                    while cur <= end:
                        ds = cur.strftime("%Y-%m-%d")
                        if ds in date_rsi and date_rsi[ds] is not None:
                            last_rsi = date_rsi[ds]
                        self._weekly_rsi[symbol][ds] = last_rsi
                        cur += timedelta(days=1)

            if progress_callback:
                progress_callback(i + 1, total, "preload")

            if (i + 1) % 100 == 0:
                print(f"   ⏳ Preloaded {i+1}/{total} symbols")

        self._preloaded = True
        print(f"✅ CSV preload complete: {total} symbols ready in RAM.\n")

    # ── Step 2: Run full grid ──────────────────────────────────────────────────
    def run_grid(self, param_grid: dict, walk_forward: bool = False,
                 progress_callback=None, n_workers: int = 0) -> dict:
        """
        Run all parameter combinations against preloaded data.

        n_workers: number of parallel worker processes.
                   0 (default) = auto-detect (cpu_count - 1).
                   1 = single-threaded (for debugging).
        """
        if not self._preloaded:
            raise RuntimeError("Call preload() before run_grid()")

        keys   = list(param_grid.keys())
        values = list(param_grid.values())
        combos = [dict(zip(keys, c)) for c in product(*values)]
        total  = len(combos)

        workers = (n_workers if n_workers > 0
                   else max(1, (os.cpu_count() or 1) - 1))
        workers = min(workers, 8)  # cap — beyond 8 cores the overhead grows

        print(f"\n🔁 Running {total} combinations "
              f"({'with' if walk_forward else 'without'} walk-forward) "
              f"| {workers} worker(s) ...")

        # Build plain_names dict once
        plain_names = {s: self.mapper.get_plain_name(s) for s in self.symbols}

        # Adaptive chunk size: target ~90s per task regardless of combo count.
        # More combos → fewer symbols per chunk to keep task duration stable.
        # Floor at 3 symbols (avoid too many tiny tasks), cap at 20.
        n = len(self.symbols)
        target_iter   = 180_000_000          # ~90s at ~2M Python iter/sec
        iter_per_sym  = len(combos) * 2 * 5_000   # combos × walk-fwd × candles
        ideal_size    = max(3, target_iter // max(iter_per_sym, 1))
        chunk_size    = max(3, min(20, ideal_size))
        chunks        = [self.symbols[i:i+chunk_size]
                         for i in range(0, n, chunk_size)]

        tasks = []
        for chunk in chunks:
            chunk_data = {
                "rsi_series":  {s: self._rsi_series.get(s, {}) for s in chunk},
                "daily_rsi":   {s: self._daily_rsi.get(s,  {}) for s in chunk},
                "weekly_rsi":  {s: self._weekly_rsi.get(s, {}) for s in chunk},
                "plain_names": {s: plain_names[s] for s in chunk},
                "symbols":     chunk,
            }
            tasks.append((chunk_data, combos, walk_forward))

        n_tasks = len(tasks)

        # Accumulators: combo_idx → list[Trade]
        combo_full  = [[] for _ in range(total)]
        combo_train = [[] for _ in range(total)]
        combo_test  = [[] for _ in range(total)]

        done_chunks = [0]

        def _merge_chunk(chunk_results):
            for (idx, full, train, test) in chunk_results:
                combo_full[idx].extend(full)
                combo_train[idx].extend(train)
                combo_test[idx].extend(test)
            done_chunks[0] += 1
            if progress_callback:
                frac = done_chunks[0] / n_tasks
                progress_callback(min(int(frac * total), total), total)

        if workers <= 1 or n_tasks <= 1:
            # Single-threaded
            for task in tasks:
                _merge_chunk(_symbol_chunk_task(task))
        else:
            # joblib/loky avoids the Windows + Streamlit __main__ reimport problem
            # that breaks concurrent.futures.ProcessPoolExecutor.
            try:
                from joblib import Parallel, delayed
                all_chunk_results = Parallel(
                    n_jobs=workers,
                    backend="loky",
                    timeout=None,             # no per-task timeout — tasks run to completion
                    return_as="generator_unordered",
                )(delayed(_symbol_chunk_task)(t) for t in tasks)
                for chunk_results in all_chunk_results:
                    _merge_chunk(chunk_results)
            except Exception as e:
                print(f"⚠️  Parallel failed ({e}) — running single-threaded")
                for task in tasks:
                    _merge_chunk(_symbol_chunk_task(task))

        # Build BacktestResult objects from merged trades
        full_results  = []
        train_results = []
        test_results  = []

        for i, params in enumerate(combos):
            params_final = {**params, "rsi_period": 14}

            r = BacktestResult(params=params_final, trades=combo_full[i])
            r.period = "full"
            if r.total_trades > 0:
                full_results.append(r)

            if walk_forward:
                tr = BacktestResult(params=params_final, trades=combo_train[i])
                tr.period = "train"
                if tr.total_trades > 0:
                    train_results.append(tr)

                te = BacktestResult(params=params_final, trades=combo_test[i])
                te.period = "test"
                if te.total_trades > 0:
                    test_results.append(te)

        full_results.sort(key=lambda r: r.score, reverse=True)
        train_results.sort(key=lambda r: r.score, reverse=True)
        test_results.sort(key=lambda r: r.score, reverse=True)

        qualified = sum(1 for r in full_results if r.score > -999)
        print(f"\n✅ Done: {qualified} qualifying combinations (≥20 trades, PF>1)")
        if full_results and full_results[0].score > -999:
            b = full_results[0]
            print(f"   Best: Win {b.win_rate}% | "
                  f"Expect {b.expectancy:+.2f}% | "
                  f"PF {b.profit_factor} | Trades {b.total_trades}")

        return {"full": full_results,
                "train": train_results,
                "test": test_results}
