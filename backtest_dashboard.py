"""
backtest_dashboard.py
Backtest UI — data loaded once into RAM, results shown instantly.

How it works:
1. Dashboard opens → loads all historical data into RAM (one-time, ~2 min)
2. User selects parameters → clicks Run
3. Backtest runs against in-memory data → results appear in seconds
4. No .bat file, no separate process, no file polling
"""

import streamlit as st
import pandas as pd
import json
import os
import sys
from datetime import datetime
import pytz
import multiprocessing

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

IST = pytz.timezone("Asia/Kolkata")

st.set_page_config(
    page_title="Backtest — RSI Scanner",
    page_icon="📊",
    layout="wide"
)

st.title("📊 Backtest — RSI Drop + Averaging Strategy")
st.caption("636 stocks | 100 days intraday | 3 years daily | Results in seconds")
st.markdown("---")


# ─── Load engine once, keep in RAM forever ─────────────────────────────────────
# st.cache_resource keeps the object alive across page reruns.
# First visit: loads all CSVs (~2 min). Every visit after: instant.
@st.cache_resource(show_spinner=False)
def get_engine():
    from backtester.engine import BacktestEngine
    from core.data_store import DataStore
    from core.symbol_map import SymbolMapper

    mapper     = SymbolMapper()
    data_store = DataStore()
    symbols    = mapper.get_all_fyers_symbols()

    engine = BacktestEngine(data_store, mapper, symbols)
    engine.preload(
        timeframes=["5", "10", "15", "30", "60", "D", "W"],
        rsi_period=14
    )
    return engine


# Show loading bar on first open
if "engine_loaded" not in st.session_state:
    with st.spinner("Loading historical data into RAM — one-time cost, ~1 minute..."):
        engine = get_engine()
    st.session_state["engine_loaded"] = True
    st.rerun()
else:
    engine = get_engine()


# ══════════════════════════════════════════════════════════════════════════════
# PARAMETER SELECTION
# ══════════════════════════════════════════════════════════════════════════════
st.subheader("Select Parameters")

col1, col2, col3 = st.columns(3)

with col1:
    st.markdown("**RSI Settings**")
    rsi_entries = st.multiselect("RSI Entry",
        [10,15,20,25,30,35,40], default=[20,25,30])
    rsi_resets  = st.multiselect("RSI Reset",
        [55,60,65,70,75,80], default=[65,70])
    rsi_exits   = st.multiselect("RSI Exit",
        [55,60,65,70,75,80], default=[65,70])

with col2:
    st.markdown("**Drop & Avg %**")
    drop_pcts = st.multiselect("Drop % (BUY trigger)",
        [0.5,1.0,1.5,2.0,2.5,3.0,4.0,5.0], default=[1.0,2.0,3.0])
    avg_pcts  = st.multiselect("Avg Drop % (each avg step)",
        [1.0,1.5,2.0,2.5,3.0,4.0,5.0], default=[2.0,3.0])

with col3:
    st.markdown("**Timeframes**")
    scan_tfs = st.multiselect("Scan TF",
        ["5","10","15","30","60"], default=["5","15"])
    exit_tfs = st.multiselect("Exit TF",
        ["5","10","15","30","60"], default=["5","15","30"])
    st.markdown("**Higher TF Filters**")
    daily_opts  = st.multiselect("Daily RSI Filter",
        ["ON","OFF"], default=["ON","OFF"])
    weekly_opts = st.multiselect("Weekly RSI Filter",
        ["ON","OFF"], default=["OFF"])

walk_fwd = st.checkbox(
    "Walk-Forward Validation (70/30 split) — prevents overfitting",
    value=True
)

_cpu_max = max(1, (multiprocessing.cpu_count() or 1) - 1)
n_workers = st.slider(
    f"Parallel Workers — your machine has {multiprocessing.cpu_count() or 1} cores",
    min_value=1, max_value=min(_cpu_max, 8),
    value=1,
    help="1 worker = most reliable. More workers can speed things up if you have 16+ GB RAM, "
         "but may be slower on machines with limited memory due to data-transfer overhead."
)

# Combination count + time estimate
n = (max(1,len(rsi_entries)) * max(1,len(rsi_resets)) *
     max(1,len(rsi_exits))   * max(1,len(drop_pcts))  *
     max(1,len(avg_pcts))    * max(1,len(scan_tfs))   *
     max(1,len(exit_tfs))    * max(1,len(daily_opts)) *
     max(1,len(weekly_opts)))

n_wf      = n * 2 if walk_fwd else n   # full(1.0) + train(0.7) + test(0.3) = 2.0x not 3x
_workers  = min(max(1, (multiprocessing.cpu_count() or 1) - 1), 8)
est_secs  = max(2, round(n_wf * len(engine.symbols) / (400000 * _workers)))
est_label = f"~{est_secs}s" if est_secs < 60 else f"~{est_secs//60}m {est_secs%60}s"

st.info(f"**{n:,} combinations** | Estimated time: **{est_label}** | {_workers} workers | Data in RAM")

# Validate
missing = [x for x,v in [("RSI Entry",rsi_entries),
                           ("Drop %",drop_pcts),
                           ("Scan TF",scan_tfs),
                           ("Exit TF",exit_tfs)] if not v]
if missing:
    st.error(f"Select at least one value for: {', '.join(missing)}")
    st.stop()

# ── Run button ─────────────────────────────────────────────────────────────────
run_clicked = st.button("▶ Run Backtest", type="primary", use_container_width=False)

if run_clicked:
    param_grid = {
        "rsi_entry_threshold":       rsi_entries,
        "rsi_reset_threshold":       rsi_resets  or [70],
        "rsi_exit_threshold":        rsi_exits   or [70],
        "drop_percent":              drop_pcts,
        "avg_drop_percent":          avg_pcts    or [3.0],
        "scan_timeframe":            scan_tfs,
        "exit_timeframe":            exit_tfs,
        "daily_rsi_filter_enabled":  [o=="ON" for o in (daily_opts  or ["OFF"])],
        "weekly_rsi_filter_enabled": [o=="ON" for o in (weekly_opts or ["OFF"])],
        "daily_rsi_threshold":       [60],
        "weekly_rsi_threshold":      [60],
    }

    prog_bar  = st.progress(0.0, text="Running combinations...")
    done_box  = [0]
    total_box = [n_wf]

    def grid_cb(done, total):
        done_box[0]  = done
        total_box[0] = total
        prog_bar.progress(min(done / max(total, 1), 1.0),
                          text=f"Combination {done}/{total}...")

    with st.spinner(""):
        raw = engine.run_grid(
            param_grid,
            walk_forward=walk_fwd,
            progress_callback=grid_cb,
            n_workers=n_workers,
        )

    prog_bar.progress(1.0, text="Done!")

    # Serialize for session state
    def _ser(r):
        return {
            "params":     r.params,
            "period":     r.period,
            "summary":    r.summary_dict(),
            "trades":     [t.to_dict() for t in r.closed[:200]],
            "open_count": len(r.open),
        }

    st.session_state["bt_results"] = {
        "generated_at": datetime.now(IST).isoformat(),
        "total_tested": len(raw.get("full", [])),
        "full":  [_ser(r) for r in raw.get("full",  [])[:50]],
        "train": [_ser(r) for r in raw.get("train", [])[:50]],
        "test":  [_ser(r) for r in raw.get("test",  [])[:50]],
    }

st.markdown("---")

# ══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════════
if "bt_results" not in st.session_state:
    st.info("Select parameters above and click **Run Backtest** to see results.")
    st.stop()

data      = st.session_state["bt_results"]
full_raw  = data.get("full",  [])
train_raw = data.get("train", [])
test_raw  = data.get("test",  [])

qualified = [r for r in full_raw
             if r["summary"].get("Score", -999) not in (-999, "-999")]

st.subheader("Results")
st.caption(
    f"Generated: {data.get('generated_at','')[:16]} | "
    f"Tested: {data.get('total_tested', 0)} combinations | "
    f"Qualified (≥20 trades, PF>1): {len(qualified)}"
)

if not qualified:
    st.warning(
        "No combinations qualified. Try: lower RSI entry, "
        "smaller Drop %, disable Daily/Weekly filters."
    )
    st.stop()

top10 = qualified[:10]

# ── Summary table ──────────────────────────────────────────────────────────────
rows = [{**r["summary"], "Rank": i+1} for i, r in enumerate(top10)]
df   = pd.DataFrame(rows)
cols = ["Rank","Scan TF","Exit TF","RSI In","RSI Rst","RSI Exit",
        "Drop%","Avg%","D.Flt","W.Flt","Trades","Open",
        "Win%","Expect","PF","Avg P&L","AvgWin","AvgLoss",
        "Best","Worst","MaxDD","AvgAvgs","AvgDays","Score"]
st.dataframe(df[[c for c in cols if c in df.columns]],
             use_container_width=True, hide_index=True)

# ── Walk-forward table ─────────────────────────────────────────────────────────
if train_raw and test_raw:
    st.markdown("---")
    st.subheader("Walk-Forward Validation")
    st.caption("Train = first 70%  |  Test = last 30%  |  Good settings hold up on test data")

    wf_rows = []
    for i, r in enumerate(top10):
        tr = next((x for x in train_raw if x["params"] == r["params"]), None)
        te = next((x for x in test_raw  if x["params"] == r["params"]), None)

        tr_wr = tr["summary"].get("Win%", "-") if tr else "-"
        tr_pf = tr["summary"].get("PF",   "-") if tr else "-"
        te_wr = te["summary"].get("Win%", "-") if te else "-"
        te_pf = te["summary"].get("PF",   "-") if te else "-"

        try:
            full_wr = float(r["summary"]["Win%"].replace("%", ""))
            test_wr = float(te_wr.replace("%", "")) if te else 0
            verdict = "✅ Holds" if test_wr >= full_wr * 0.75 else "⚠️ Weak"
        except Exception:
            verdict = "-"

        wf_rows.append({
            "Rank":       i + 1,
            "Scan TF":    r["params"].get("scan_timeframe"),
            "RSI Entry":  r["params"].get("rsi_entry_threshold"),
            "Drop%":      r["params"].get("drop_percent"),
            "Full Win%":  r["summary"].get("Win%"),
            "Full PF":    r["summary"].get("PF"),
            "Train Win%": tr_wr, "Train PF": tr_pf,
            "Test Win%":  te_wr, "Test PF":  te_pf,
            "Verdict":    verdict,
        })

    st.dataframe(pd.DataFrame(wf_rows), use_container_width=True, hide_index=True)

# ── Detailed trade log ─────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("Detailed Trade Log")

opts = [
    f"Rank {i+1} — {r['params'].get('scan_timeframe')}m | "
    f"RSI {r['params'].get('rsi_entry_threshold')} | "
    f"Drop {r['params'].get('drop_percent')}% | "
    f"Win {r['summary'].get('Win%')} | "
    f"Expect {r['summary'].get('Expect')}"
    for i, r in enumerate(top10)
]
sel = st.selectbox("Select result to inspect:", opts)

if sel:
    idx    = opts.index(sel)
    result = top10[idx]
    s      = result["summary"]

    c1,c2,c3,c4,c5,c6,c7 = st.columns(7)
    c1.metric("Trades",  s.get("Trades"))
    c2.metric("Win%",    s.get("Win%"))
    c3.metric("Expect",  s.get("Expect"))
    c4.metric("PF",      s.get("PF"))
    c5.metric("Avg P&L", s.get("Avg P&L"))
    c6.metric("Max DD",  s.get("MaxDD"))
    c7.metric("Open",    result.get("open_count", 0))

    if result.get("open_count", 0) > 0:
        st.info(f"ℹ️ {result['open_count']} open trades not yet closed — excluded from stats.")

    trades = result.get("trades", [])
    if trades:
        rows = []
        for t in trades:
            pnl = t.get("pnl_pct")
            rows.append({
                "Stock":    t.get("plain_name", ""),
                "Entry":    (t.get("entry_time") or "")[:16],
                "Entry ₹":  f"₹{t.get('entry_price', 0):.2f}",
                "Ref ₹":    f"₹{t.get('ref_price', 0):.2f}",
                "RSI In":   t.get("rsi_at_entry"),
                "Avgs":     t.get("avg_count", 0),
                "Avg Cost": f"₹{t.get('avg_cost', 0):.2f}",
                "Exit":     (t.get("exit_time") or "")[:16],
                "Exit ₹":   f"₹{t.get('exit_price', 0):.2f}" if t.get("exit_price") else "-",
                "RSI Out":  t.get("rsi_at_exit"),
                "P&L%":     f"{'▲' if (pnl or 0) >= 0 else '▼'} {pnl:+.2f}%"
                            if pnl is not None else "-",
                "Days":     t.get("duration_days"),
            })

        df_t = pd.DataFrame(rows)
        st.dataframe(df_t, use_container_width=True, hide_index=True)

        csv = df_t.to_csv(index=False)
        st.download_button(
            "⬇️ Download Trade Log (CSV)", csv,
            f"trades_rank{idx+1}_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv",
            "text/csv"
        )

# ── Top 50 download ────────────────────────────────────────────────────────────
st.markdown("---")
if qualified:
    csv50 = pd.DataFrame([r["summary"] for r in qualified[:50]]).to_csv(index=False)
    st.download_button(
        "⬇️ Download Top 50 Summary (CSV)", csv50,
        f"backtest_top50_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv",
        "text/csv"
    )
