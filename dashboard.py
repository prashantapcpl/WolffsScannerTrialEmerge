"""
dashboard.py — Multi-Scanner Dashboard
Fixed: ##key not showing in labels, reduced flashing, gap-fill badge shown.
"""

import streamlit as st
import json
import os
import sys
import time
import socket
import pandas as pd
from datetime import datetime
import pytz

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from core.price_store import load_live_prices


def _get_server_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "YOUR_SERVER_IP"

IST = pytz.timezone("Asia/Kolkata")

st.set_page_config(
    page_title="Fyers RSI Scanner",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

CONFIG_PATH = os.path.join(ROOT, "config.json")
TOKEN_PATH  = os.path.join(ROOT, "data", "access_token.json")
RESCAN_FLAG = os.path.join(ROOT, "data", "rescan.flag")


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

def load_scanner_state(scanner_id):
    config     = load_config()
    state_file = config.get("scanners", {}).get(scanner_id, {}).get(
        "state_file", f"data/{scanner_id}_state.json")
    state_path = os.path.join(ROOT, state_file)
    if not os.path.exists(state_path):
        return {"records": {}, "signal_log": []}
    with open(state_path, "r") as f:
        return json.load(f)

def fmt_dt(s):
    if not s:
        return "-"
    return str(s)[:16].replace("T", " ")

def get_token_status():
    """Decode the Fyers JWT and report real expiry. Tokens live ~5-6 hrs;
    saved_date is meaningless for expiry checks."""
    import base64
    if not os.path.exists(TOKEN_PATH):
        return ("❌ NOT LOGGED IN — run login script before market open",
                "error")
    try:
        with open(TOKEN_PATH, "r") as f:
            data = json.load(f)
        jwt = data.get("access_token", "")
        if not jwt or jwt.count(".") != 2:
            return ("⚠️ NO JWT FOUND in token file — re-login", "error")
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp", 0)
        now_s = datetime.now(IST).timestamp()
        if not exp:
            return ("⚠️ JWT has no exp claim — re-login", "error")
        if now_s > exp:
            hours_ago = (now_s - exp) / 3600
            exp_iso = datetime.fromtimestamp(exp, tz=IST).strftime("%H:%M")
            return (f"❌ TOKEN EXPIRED — expired at {exp_iso} IST "
                    f"({hours_ago:.1f}h ago) — RE-LOGIN NOW",
                    "error")
        hours_left = (exp - now_s) / 3600
        exp_iso = datetime.fromtimestamp(exp, tz=IST).strftime("%H:%M")
        if hours_left < 1:
            return (f"⚠️ TOKEN EXPIRES at {exp_iso} IST "
                    f"({int(hours_left*60)} min left)",
                    "warning")
        return (f"✅ TOKEN VALID — expires {exp_iso} IST "
                f"({hours_left:.1f}h left)",
                "success")
    except Exception as e:
        return (f"⚠️ Token check error: {e}", "warning")

def colored_pnl(pnl):
    return f"🟢 +{pnl:.2f}%" if pnl >= 0 else f"🔴 {pnl:.2f}%"

# Module-level live_prices dict; updated inside _render_data on every refresh
live_prices = {}

def live_price(sym: str, fallback) -> float:
    """Return live tick price if available, else fall back to state's current_price."""
    p = live_prices.get(sym)
    return float(p) if p else (float(fallback) if fallback else 0.0)

def trigger_rescan(scanner_id):
    os.makedirs(os.path.dirname(RESCAN_FLAG), exist_ok=True)
    with open(RESCAN_FLAG, "w") as f:
        f.write(f"{scanner_id}:{datetime.now(IST).isoformat()}")

def get_feed_status():
    """Read data/heartbeat.json and return a colored status string."""
    hb_path = os.path.join(ROOT, "data", "heartbeat.json")
    if not os.path.exists(hb_path):
        return ("⚫ NO HEARTBEAT", "Scanner process not running "
                "(no heartbeat file).", "error")
    try:
        with open(hb_path, "r") as f:
            hb = json.load(f)
    except Exception as e:
        return ("⚫ HEARTBEAT UNREADABLE", str(e), "error")

    # Age of heartbeat
    try:
        hb_ts  = datetime.fromisoformat(hb["ts"])
        age_s  = (datetime.now(IST) - hb_ts).total_seconds()
    except Exception:
        age_s = None

    if age_s is None or age_s > 90:
        return ("⚫ SCANNER DOWN",
                f"Heartbeat last updated {int(age_s) if age_s else '?'}s ago "
                f"(>90s = process likely dead).", "error")

    status = hb.get("feed_status", "UNKNOWN")
    sec_tick = hb.get("seconds_since_tick")
    sub_pct = hb.get("subscription_coverage_pct", 0)
    in_mkt  = hb.get("in_market_hours", False)

    if status == "LIVE":
        msg = (f"GOOD — feed is working. Last tick {sec_tick}s ago. "
               f"Coverage {sub_pct}% of subscribed symbols.")
        return ("🟢 GOOD — FEED LIVE — WORKING", msg, "success")
    if status == "STARTING":
        uptime = hb.get("uptime_seconds", 0)
        remaining = max(0, 300 - uptime)
        msg = (f"STARTING — scanner booted {uptime}s ago, doing history "
               f"fetch + integrity pass + carry-forward. WebSocket "
               f"normally starts delivering ticks within the first ~5 min. "
               f"Will flip to BROKEN if still no ticks after ~{remaining}s. "
               f"No action needed yet — just wait.")
        return ("🟡 STARTING — PLEASE WAIT (~5 min)", msg, "warning")
    if status == "STALE":
        msg = (f"PROBLEM — WebSocket stalled. No tick received in "
               f"{sec_tick}s during market hours. "
               f"ACTION: Restart the scanner soon.")
        return ("🟡 PROBLEM — FEED STALE — RESTART SOON", msg, "warning")
    if status == "DEAD":
        msg = ("BROKEN — Market is open and scanner has been up >5 min "
               "but ZERO ticks received across ALL symbols. WebSocket "
               "failed to connect (token expired? network blocked?). "
               "ACTION: Re-login + restart scanner.")
        return ("🔴 BROKEN — FEED DEAD — RESTART NOW", msg, "error")
    if status == "CLOSED":
        return ("⚪ OK — MARKET CLOSED — NOTHING TO DO",
                f"Market is closed. Scanner alive ({int(age_s)}s heartbeat). "
                f"Feed will resume at next market open.", "info")
    return (f"❓ UNKNOWN STATUS — {status}",
            "Heartbeat does not contain feed metrics. Restart scanner "
            "to enable feed-health reporting.", "warning")


# ─── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("📈 RSI Scanner")
    st.caption(f"🕐 {datetime.now(IST).strftime('%d %b %Y  %H:%M:%S')}")
    st.markdown("---")

    # Feed status banner — always visible, color-coded
    _fs_label, _fs_msg, _fs_kind = get_feed_status()
    st.markdown(f"### {_fs_label}")
    if _fs_kind == "success":
        st.success(_fs_msg)
    elif _fs_kind == "warning":
        st.warning(_fs_msg)
    elif _fs_kind == "error":
        st.error(_fs_msg)
    else:
        st.info(_fs_msg)

    st.markdown("---")

    # ── Market Schedule (next trading day / upcoming holidays) ─────────
    try:
        from core.market_calendar import (
            is_trading_day, describe_non_trading_day, next_market_open,
            upcoming_events, IST as _IST,
        )
        from datetime import timedelta as _td
        _now    = datetime.now(_IST)
        _today  = _now.date()
        _tomm   = _today + _td(days=1)

        st.markdown("### 📅 Market Schedule")
        # Today
        if is_trading_day(_today):
            st.markdown(f"**Today** ({_today.strftime('%a %d %b')}): "
                        f"Trading day · 09:15 – 15:30")
        else:
            d = describe_non_trading_day(_today)
            est = "  ⚠️ estimated" if d.get("is_estimated") else ""
            st.markdown(f"**Today** ({_today.strftime('%a %d %b')}): "
                        f"**Closed** — {d['reason']}{est}")
        # Tomorrow
        if is_trading_day(_tomm):
            st.markdown(f"**Tomorrow** ({_tomm.strftime('%a %d %b')}): "
                        f"Trading day · opens 09:15")
        else:
            d = describe_non_trading_day(_tomm)
            est = "  ⚠️ estimated" if d.get("is_estimated") else ""
            reopen = next_market_open(_now)
            st.markdown(f"**Tomorrow** ({_tomm.strftime('%a %d %b')}): "
                        f"**Closed** — {d['reason']}{est}")
            st.markdown(f"  Market reopens **{reopen.strftime('%a %d %b at %H:%M')}**")
        # Next 7 days non-trading events
        evts = upcoming_events(_today, days_ahead=7)
        if evts:
            with st.expander(f"Next 7 days — {len(evts)} non-trading day(s)"):
                for e in evts:
                    est = "  ⚠️ estimated (verify before relying on it)" if e["is_estimated"] else ""
                    st.markdown(f"- **{e['date'].strftime('%a %d %b')}** — "
                                f"{e['reason']}{est}")
        else:
            st.caption("Next 7 days: all trading days, no holidays.")
    except Exception as _e:
        st.caption(f"(Calendar widget error: {_e})")

    st.markdown("---")
    # Token status box (color-coded). Shows REAL JWT expiry, not saved_date.
    _tok_msg, _tok_kind = get_token_status()
    if _tok_kind == "success":
        st.success(_tok_msg)
    elif _tok_kind == "warning":
        st.warning(_tok_msg)
    elif _tok_kind == "error":
        st.error(_tok_msg)
    else:
        st.info(_tok_msg)
    st.markdown("---")
    # key= preserves nav selection across auto-reruns — prevents Active Buys flash
    st.radio("", [
        "🟢 Active Buys",
        "🔴 Exit Signals",
        "👁️ Watched Stocks",
        "📋 Signal Log",
        "⚙️ Settings"
    ], label_visibility="collapsed", key="nav_select")
    st.markdown("---")
    if st.button("🔄 Refresh", use_container_width=True):
        st.rerun()
    st.checkbox("Auto-refresh (15s)", value=True, key="auto_refresh_cb")

# ─── Header ────────────────────────────────────────────────────────────────────
st.title("📈 Fyers RSI Scanner")

# ─── Scanner 3 tab renderer ────────────────────────────────────────────────────
def _render_s3_tab(scanner_id: str, scfg: dict, config: dict, nav: str):
    """Renders the complete Scanner 3 (Webhook Mirror) tab."""

    state      = load_scanner_state(scanner_id)
    records    = state.get("records", {})
    signal_log = state.get("signal_log", [])

    # Segregate positions by side
    buy_side  = {s: r for s, r in records.items()
                 if r.get("side") == "BUY"  and r.get("state") != "GENERAL"}
    sell_side = {s: r for s, r in records.items()
                 if r.get("side") == "SELL" and r.get("state") != "GENERAL"}
    watched   = {s: r for s, r in records.items()
                 if r.get("state") in ("WATCHED_BUY", "WATCHED_SELL")}
    active    = {s: r for s, r in records.items()
                 if r.get("state") in ("ACTIVE_BUY", "ACTIVE_SELL",
                                       "EXITING_BUY", "EXITING_SELL")}
    exits     = sorted(
                    [e for e in signal_log
                     if e.get("type") in ("EXIT_BUY", "EXIT_SELL")],
                    key=lambda e: e.get("time", "") or "", reverse=True)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🟢 Long Positions",  len(buy_side))
    m2.metric("🔴 Short Positions", len(sell_side))
    m3.metric("👁️ Watching",        len(watched))
    m4.metric("📋 Exits Today",     len(exits))
    st.markdown("---")

    cfg = scfg.get("settings", {})

    # ══════════════════════════════════════════════════════════════════════════
    # ACTIVE POSITIONS
    # ══════════════════════════════════════════════════════════════════════════
    if nav == "🟢 Active Buys":
        # BUY side
        st.subheader(f"🟢 Long Positions (BUY side) — {len(buy_side)} stocks")
        if not buy_side:
            st.info("No active long positions.")
        else:
            rows = []
            for sym, r in sorted(buy_side.items(),
                                  key=lambda x: x[1].get("entry_time") or "", reverse=True):
                ref    = r.get("ref_price") or 0
                entry  = r.get("entry_price") or 0
                cur    = live_price(sym, r.get("current_price") or entry)
                pnl    = round(((cur - entry) / entry * 100), 2) if entry else 0
                state_ = r.get("state","")
                status = ("EXITING" if "EXITING" in state_
                          else f"Avg {r.get('avg_count',0)}" if r.get("avg_count",0) > 0
                          else "ACTIVE")
                rows.append({
                    "Stock":      r.get("plain_name", sym),
                    "Status":     status,
                    "Ref ₹":      f"₹{ref:.2f}",
                    "Ref Time":   fmt_dt(r.get("ref_time")),
                    "Entry ₹":    f"₹{entry:.2f}",
                    "Entry Time": fmt_dt(r.get("entry_time")),
                    "Now ₹":      f"₹{cur:.2f}",
                    "Avgs":       r.get("avg_count", 0),
                    "P&L":        colored_pnl(pnl),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            st.markdown("### Full Detail (Long)")
            for sym, r in sorted(buy_side.items(),
                                  key=lambda x: x[1].get("entry_time") or "", reverse=True):
                ref    = r.get("ref_price") or 0
                entry  = r.get("entry_price") or 0
                cur    = live_price(sym, r.get("current_price") or entry)
                pnl    = round(((cur - entry) / entry * 100), 2) if entry else 0
                state_ = r.get("state","")
                status = "EXITING" if "EXITING" in state_ else f"Avg {r.get('avg_count',0)}" if r.get("avg_count",0) > 0 else "ACTIVE"
                st.markdown(f"### 🟢 **{r.get('plain_name',sym)}** &nbsp; `{status}` &nbsp; {colored_pnl(pnl)}")
                st.caption(r.get("company_name",""))
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Ref Price",   f"₹{ref:.2f}",   fmt_dt(r.get("ref_time")))
                c2.metric("Entry Price", f"₹{entry:.2f}", fmt_dt(r.get("entry_time")))
                c3.metric("Now",         f"₹{cur:.2f}")
                c4.metric("P&L",         f"{pnl:+.2f}%")
                if r.get("state") in ("EXITING_BUY",):
                    st.caption(f"Exit ref: ₹{r.get('exit_ref_price',0):.2f} "
                               f"@ {fmt_dt(r.get('exit_ref_time'))} — "
                               f"waiting {cfg.get('exit_offset_pct_buy',1.0):.1f}% above")
                if r.get("avg_entries"):
                    avg_rows = [{"Level": f"Avg {a.get('avg_num','')}", "Price": f"₹{a.get('price',0):.2f}",
                                 "Time": fmt_dt(a.get("signal_time")), "Move %": f"{a.get('change_pct',0):.2f}%"}
                                for a in r.get("avg_entries", [])]
                    st.dataframe(pd.DataFrame(avg_rows), use_container_width=False, hide_index=True)
                st.divider()

        st.markdown("---")

        # SELL side
        st.subheader(f"🔴 Short Positions (SELL side) — {len(sell_side)} stocks")
        if not sell_side:
            st.info("No active short positions.")
        else:
            rows = []
            for sym, r in sorted(sell_side.items(),
                                  key=lambda x: x[1].get("entry_time") or "", reverse=True):
                ref    = r.get("ref_price") or 0
                entry  = r.get("entry_price") or 0
                cur    = live_price(sym, r.get("current_price") or entry)
                pnl    = round(((entry - cur) / entry * 100), 2) if entry else 0
                state_ = r.get("state","")
                status = ("EXITING" if "EXITING" in state_
                          else f"Avg Short {r.get('avg_count',0)}" if r.get("avg_count",0) > 0
                          else "ACTIVE")
                rows.append({
                    "Stock":      r.get("plain_name", sym),
                    "Status":     status,
                    "Ref ₹":      f"₹{ref:.2f}",
                    "Ref Time":   fmt_dt(r.get("ref_time")),
                    "Entry ₹":    f"₹{entry:.2f}",
                    "Entry Time": fmt_dt(r.get("entry_time")),
                    "Now ₹":      f"₹{cur:.2f}",
                    "Avgs":       r.get("avg_count", 0),
                    "P&L":        colored_pnl(pnl),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            st.markdown("### Full Detail (Short)")
            for sym, r in sorted(sell_side.items(),
                                  key=lambda x: x[1].get("entry_time") or "", reverse=True):
                ref    = r.get("ref_price") or 0
                entry  = r.get("entry_price") or 0
                cur    = live_price(sym, r.get("current_price") or entry)
                pnl    = round(((entry - cur) / entry * 100), 2) if entry else 0
                state_ = r.get("state","")
                status = "EXITING" if "EXITING" in state_ else f"Avg Short {r.get('avg_count',0)}" if r.get("avg_count",0) > 0 else "ACTIVE"
                st.markdown(f"### 🔴 **{r.get('plain_name',sym)}** &nbsp; `{status}` &nbsp; {colored_pnl(pnl)}")
                st.caption(r.get("company_name",""))
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Ref Price",   f"₹{ref:.2f}",   fmt_dt(r.get("ref_time")))
                c2.metric("Entry Price", f"₹{entry:.2f}", fmt_dt(r.get("entry_time")))
                c3.metric("Now",         f"₹{cur:.2f}")
                c4.metric("P&L",         f"{pnl:+.2f}%")
                if r.get("state") in ("EXITING_SELL",):
                    st.caption(f"Exit ref: ₹{r.get('exit_ref_price',0):.2f} "
                               f"@ {fmt_dt(r.get('exit_ref_time'))} — "
                               f"waiting {cfg.get('exit_offset_pct_sell',1.0):.1f}% below")
                if r.get("avg_entries"):
                    avg_rows = [{"Level": f"Avg Short {a.get('avg_num','')}", "Price": f"₹{a.get('price',0):.2f}",
                                 "Time": fmt_dt(a.get("signal_time")), "Move %": f"{a.get('change_pct',0):.2f}%"}
                                for a in r.get("avg_entries", [])]
                    st.dataframe(pd.DataFrame(avg_rows), use_container_width=False, hide_index=True)
                st.divider()

    # ══════════════════════════════════════════════════════════════════════════
    # EXIT SIGNALS
    # ══════════════════════════════════════════════════════════════════════════
    elif nav == "🔴 Exit Signals":
        st.subheader(f"🔴 Closed Positions — {len(exits)} total")
        if not exits:
            st.info("No exit signals yet.")
        else:
            rows = []
            for e in exits[:200]:
                entry_p = e.get("entry_price") or 0
                exit_p  = e.get("price") or 0
                pnl     = e.get("pnl_pct", 0)
                side    = "LONG" if e.get("type") == "EXIT_BUY" else "SHORT"
                rows.append({
                    "Stock":      e.get("plain_name",""),
                    "Side":       side,
                    "Entry ₹":    f"₹{entry_p:.2f}",
                    "Exit ₹":     f"₹{exit_p:.2f}",
                    "Exit Time":  fmt_dt(e.get("time")),
                    "Avgs":       e.get("avg_count", 0),
                    "P&L":        colored_pnl(float(pnl) if pnl else 0),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════════
    # WATCHED
    # ══════════════════════════════════════════════════════════════════════════
    elif nav == "👁️ Watched Stocks":
        st.subheader(f"👁️ Waiting for Entry — {len(watched)} stocks")
        if not watched:
            st.info("No stocks waiting for entry trigger.")
        else:
            rows = []
            for sym, r in sorted(watched.items(),
                                  key=lambda x: x[1].get("ref_time") or "", reverse=True):
                ref  = r.get("ref_price") or 0
                cur  = live_price(sym, r.get("current_price") or ref)
                side = r.get("side","")
                if side == "BUY":
                    dist = round(((ref - cur) / ref * 100), 2) if ref else 0
                    need = max(0, round(float(cfg.get("drop_pct", 2.0)) - dist, 2))
                    trigger_note = f"Need {need:.2f}% more drop"
                else:
                    dist = round(((cur - ref) / ref * 100), 2) if ref else 0
                    need = max(0, round(float(cfg.get("rise_pct", 2.0)) - dist, 2))
                    trigger_note = f"Need {need:.2f}% more rise"
                rows.append({
                    "Side":     side,
                    "Stock":    r.get("plain_name", sym),
                    "Ref ₹":    f"₹{ref:.2f}",
                    "Ref Time": fmt_dt(r.get("ref_time")),
                    "Now ₹":    f"₹{cur:.2f}",
                    "Distance": f"{dist:.2f}%",
                    "Trigger":  trigger_note,
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════════
    # SIGNAL LOG
    # ══════════════════════════════════════════════════════════════════════════
    elif nav == "📋 Signal Log":
        st.subheader("📋 Signal Log — Scanner 3")
        if not signal_log:
            st.info("No signals yet.")
        else:
            rows = []
            for e in sorted(signal_log, key=lambda x: x.get("time","") or "",
                            reverse=True):
                t  = e.get("type","")
                px = e.get("price") or e.get("avg_price") or 0
                rows.append({
                    "Type":  e.get("label", t),
                    "Stock": e.get("plain_name",""),
                    "Price": f"₹{float(px):.2f}" if px else "-",
                    "Time":  fmt_dt(e.get("time")),
                    "Info":  (
                        f"Ref:₹{e.get('ref_price',0):.2f}"
                        if t in ("BUY","SELL") else
                        f"Entry:₹{e.get('entry_price',0):.2f} P&L:{e.get('pnl_pct',0):+.2f}%"
                        if "EXIT" in t else
                        f"Avg #{e.get('avg_number','')} prev:₹{e.get('avg_price',0):.2f}"
                    ),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════════
    # SETTINGS
    # ══════════════════════════════════════════════════════════════════════════
    elif nav == "⚙️ Settings":
        st.subheader("⚙️ Settings — Scanner 3 (Webhook Mirror)")

        server_ip = _get_server_ip()
        port      = scfg.get("incoming_webhooks", {}).get("server_port", 5001)

        # ── Incoming webhook URLs (st.code → has built-in copy button) ───
        st.markdown("#### 📥 Incoming Webhook URLs")
        st.info("Click the copy icon in the top-right of each box, then "
                "paste into your Chartink alert settings.")
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**🟢 BUY alerts URL**")
            st.code(f"http://{server_ip}:{port}/s3/buy", language=None)
            st.markdown("**🔴 EXIT-BUY URL** (optional — exit is auto-computed)")
            st.code(f"http://{server_ip}:{port}/s3/exit-buy", language=None)
        with col_b:
            st.markdown("**🔴 SELL alerts URL**")
            st.code(f"http://{server_ip}:{port}/s3/sell", language=None)
            st.markdown("**🟢 EXIT-SELL URL** (optional — exit is auto-computed)")
            st.code(f"http://{server_ip}:{port}/s3/exit-sell", language=None)
        st.caption(f"Server IP: **{server_ip}** | Port: **{port}** "
                   f"| Status check: http://{server_ip}:{port}/s3/status")

        st.markdown("---")

        # ── Outgoing webhook URLs (editable) ─────────────────────────────
        st.markdown("#### 📤 Outgoing Webhook URLs")
        st.caption("Where to send signals when entry/exit conditions are met.")
        ow = scfg.get("outgoing_webhooks", {})
        col_c, col_d = st.columns(2)
        with col_c:
            buy_url      = st.text_input("🟢 BUY signal URL",
                value=ow.get("buy_url",""),
                placeholder="https://your-app.com/webhook/buy",
                key="s3_out_buy")
            exit_buy_url = st.text_input("🔴 EXIT-BUY URL",
                value=ow.get("exit_buy_url",""),
                placeholder="https://your-app.com/webhook/exit-buy",
                key="s3_out_exit_buy")
        with col_d:
            sell_url      = st.text_input("🔴 SELL signal URL",
                value=ow.get("sell_url",""),
                placeholder="https://your-app.com/webhook/sell",
                key="s3_out_sell")
            exit_sell_url = st.text_input("🟢 EXIT-SELL URL",
                value=ow.get("exit_sell_url",""),
                placeholder="https://your-app.com/webhook/exit-sell",
                key="s3_out_exit_sell")

        st.markdown("---")

        # ── Strategy parameters ───────────────────────────────────────────
        st.markdown("#### ⚙️ Strategy Parameters")
        tf_opts = ["5", "15", "60"]   # candle-based strategy uses only 5/15/60
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**🟢 BUY side**")
            drop_pct = st.slider("Drop % for BUY trigger  (0 = instant)",
                0.0, 5.0, float(cfg.get("drop_pct", 2.0)),
                step=0.25, key="s3_drop")
            avg_pct_buy = st.slider("Avg % (BUY)  (0 = disable averaging)",
                0.0, 5.0, float(cfg.get("avg_pct_buy", 3.0)),
                step=0.25, key="s3_avg_buy")
            exit_offset_pct_buy = st.slider("Exit % (BUY) above avg of buys",
                0.5, 10.0, float(cfg.get("exit_offset_pct_buy", 4.0)),
                step=0.25, key="s3_exit_buy")
            _buy_tf_cur = str(cfg.get("trigger_timeframe_buy",
                                cfg.get("trigger_timeframe", "5")))
            if _buy_tf_cur not in tf_opts: _buy_tf_cur = "5"
            trigger_tf_buy = st.selectbox("Trigger Timeframe (BUY)",
                tf_opts, index=tf_opts.index(_buy_tf_cur),
                key="s3_tf_buy")
        with col2:
            st.markdown("**🔴 SELL side**")
            rise_pct = st.slider("Rise % for SELL trigger  (0 = instant)",
                0.0, 5.0, float(cfg.get("rise_pct", 2.0)),
                step=0.25, key="s3_rise")
            avg_pct_sell = st.slider("Avg % (SELL)  (0 = disable averaging)",
                0.0, 5.0, float(cfg.get("avg_pct_sell", 3.0)),
                step=0.25, key="s3_avg_sell")
            exit_offset_pct_sell = st.slider("Exit % (SELL) below avg of shorts",
                0.5, 10.0, float(cfg.get("exit_offset_pct_sell", 4.0)),
                step=0.25, key="s3_exit_sell")
            _sell_tf_cur = str(cfg.get("trigger_timeframe_sell",
                                cfg.get("trigger_timeframe", "5")))
            if _sell_tf_cur not in tf_opts: _sell_tf_cur = "5"
            trigger_tf_sell = st.selectbox("Trigger Timeframe (SELL)",
                tf_opts, index=tf_opts.index(_sell_tf_cur),
                key="s3_tf_sell")

        scanner_name = st.text_input("Scanner Name",
            value=scfg.get("name","Scanner 3 — Webhook Mirror"), key="s3_name")

        st.markdown("---")
        if st.button("💾 Save Settings", type="primary",
                     use_container_width=True, key="s3_save"):
            config["scanners"]["scanner_3"]["name"]     = scanner_name
            config["scanners"]["scanner_3"]["settings"] = {
                "drop_pct":               drop_pct,
                "rise_pct":               rise_pct,
                "avg_pct_buy":            avg_pct_buy,
                "avg_pct_sell":           avg_pct_sell,
                "exit_offset_pct_buy":    exit_offset_pct_buy,
                "exit_offset_pct_sell":   exit_offset_pct_sell,
                "trigger_timeframe":      trigger_tf_buy,   # legacy alias
                "trigger_timeframe_buy":  trigger_tf_buy,
                "trigger_timeframe_sell": trigger_tf_sell,
            }
            config["scanners"]["scanner_3"]["outgoing_webhooks"] = {
                "buy_url":       buy_url,
                "sell_url":      sell_url,
                "exit_buy_url":  exit_buy_url,
                "exit_sell_url": exit_sell_url,
            }
            save_config(config)
            st.success("✅ Saved! Restart scanner for trigger-timeframe changes to take effect.")

        st.markdown("---")
        _drop_desc = ("INSTANT (no wait)" if drop_pct == 0
                      else f"close ≤ ref × (1 - {drop_pct:.2f}%)")
        _rise_desc = ("INSTANT (no wait)" if rise_pct == 0
                      else f"close ≥ ref × (1 + {rise_pct:.2f}%)")
        _avg_buy_desc  = ("DISABLED" if avg_pct_buy  == 0
                          else f"every {avg_pct_buy:.2f}% drop from last entry")
        _avg_sell_desc = ("DISABLED" if avg_pct_sell == 0
                          else f"every {avg_pct_sell:.2f}% rise from last entry")
        st.code(
            f"BUY  : webhook → ref = last completed {trigger_tf_buy}m candle close\n"
            f"       trigger = {_drop_desc}\n"
            f"       avg     = {_avg_buy_desc}\n"
            f"       exit    = avg(buys) × (1 + {exit_offset_pct_buy:.2f}%)\n"
            f"\n"
            f"SELL : webhook → ref = last completed {trigger_tf_sell}m candle close\n"
            f"       trigger = {_rise_desc}\n"
            f"       avg     = {_avg_sell_desc}\n"
            f"       exit    = avg(shorts) × (1 - {exit_offset_pct_sell:.2f}%)\n"
            f"\n"
            f"Duplicate webhooks for a symbol are ignored while it's not in\n"
            f"GENERAL state. Same symbol may re-enter after EXIT fires.",
            language="text"
        )


# ─── Scanner 4 tab renderer (RSI Momentum) ─────────────────────────────────────
def _render_s4_tab(scanner_id: str, scfg: dict, config: dict, nav: str):
    """Renders the complete Scanner 4 (Multi-Timeframe RSI Momentum) tab."""

    state      = load_scanner_state(scanner_id)
    records    = state.get("records", {})
    signal_log = state.get("signal_log", [])

    buy_active  = {s: r for s, r in records.items()
                   if r.get("state", "").startswith("BUY") and r.get("state","").endswith("_ACTIVE")}
    sell_active = {s: r for s, r in records.items()
                   if r.get("state", "").startswith("SELL") and r.get("state","").endswith("_ACTIVE")}
    flagged     = {s: r for s, r in records.items()
                   if r.get("state") in ("FLAGGED_BUY", "FLAGGED_SELL")}
    cooling     = {s: r for s, r in records.items()
                   if r.get("state") in ("COOLING_BUY", "COOLING_SELL")}
    sl_cooling  = {s: r for s, r in records.items()
                   if r.get("state") in ("COOLING_BUY_STOPLOSS",
                                          "COOLING_SELL_STOPLOSS")}
    exits       = sorted(
                      [e for e in signal_log
                       if str(e.get("type","")).startswith("EXIT_")],
                      key=lambda e: e.get("time","") or "", reverse=True)

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("🟢 Buy Active",     len(buy_active))
    m2.metric("🔴 Sell Active",    len(sell_active))
    m3.metric("🚩 Flagged",        len(flagged))
    m4.metric("❄️ Cooling",        len(cooling))
    m5.metric("🛑 Stoploss-cooling", len(sl_cooling))
    m6.metric("📋 Exits",          len(exits))
    st.markdown("---")

    cfg = scfg.get("settings", {})

    def _target_for(level: int, side: str) -> float:
        if side == "BUY":
            return float(cfg.get("buy1_target_pct", 5.0)) if level == 1 \
                   else float(cfg.get("buy23_target_pct", 6.0))
        return float(cfg.get("sell1_target_pct", 5.0)) if level == 1 \
               else float(cfg.get("sell23_target_pct", 6.0))

    def _state_label(s: str) -> str:
        return {
            "BUY1_ACTIVE":  "Buy 1",  "BUY2_ACTIVE":  "Buy 2",
            "BUY3_ACTIVE":  "Buy 3",  "BUY4_ACTIVE":  "Buy 4",
            "SELL1_ACTIVE": "Sell 1", "SELL2_ACTIVE": "Sell 2",
            "SELL3_ACTIVE": "Sell 3", "SELL4_ACTIVE": "Sell 4",
            "FLAGGED_BUY":  "Flagged (Buy)",
            "FLAGGED_SELL": "Flagged (Sell)",
            "COOLING_BUY":  "Cooling (Buy)",
            "COOLING_SELL": "Cooling (Sell)",
        }.get(s, s)

    # ══════════════════════════════════════════════════════════════════════════
    # ACTIVE BUYS — shows BUY SIDE and SELL SIDE in separate sections
    # ══════════════════════════════════════════════════════════════════════════
    if nav == "🟢 Active Buys":
        # ── BUY SIDE ──────────────────────────────────────────────────────
        st.subheader(f"🟢 BUY SIDE — {len(buy_active)} active")
        if not buy_active:
            st.info("No active buy positions.")
        else:
            rows = []
            for sym, r in sorted(buy_active.items(),
                                 key=lambda x: x[1].get("entry_time") or "" or "",
                                 reverse=True):
                entry  = r.get("entry_price") or 0
                level  = r.get("entry_level") or 1
                cur    = live_price(sym, r.get("current_price") or entry)
                target = round(entry * (1.0 + _target_for(level, "BUY") / 100.0), 2) if entry else 0
                pnl    = round(((cur - entry) / entry * 100.0), 2) if entry else 0
                rows.append({
                    "Stock":        r.get("plain_name", sym),
                    "State":        _state_label(r.get("state","")),
                    "Entry Price":  f"₹{entry:.2f}",
                    "Entry Time":   fmt_dt(r.get("entry_time")),
                    "Current":      f"₹{cur:.2f}",
                    "Target":       f"₹{target:.2f}",
                    "P&L":          colored_pnl(pnl),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.markdown("---")

        # ── SELL SIDE ─────────────────────────────────────────────────────
        st.subheader(f"🔴 SELL SIDE — {len(sell_active)} active")
        if not sell_active:
            st.info("No active sell positions.")
        else:
            rows = []
            for sym, r in sorted(sell_active.items(),
                                 key=lambda x: x[1].get("entry_time") or "" or "",
                                 reverse=True):
                entry  = r.get("entry_price") or 0
                level  = r.get("entry_level") or 1
                cur    = live_price(sym, r.get("current_price") or entry)
                target = round(entry * (1.0 - _target_for(level, "SELL") / 100.0), 2) if entry else 0
                pnl    = round(((entry - cur) / entry * 100.0), 2) if entry else 0
                rows.append({
                    "Stock":        r.get("plain_name", sym),
                    "State":        _state_label(r.get("state","")),
                    "Entry Price":  f"₹{entry:.2f}",
                    "Entry Time":   fmt_dt(r.get("entry_time")),
                    "Current":      f"₹{cur:.2f}",
                    "Target":       f"₹{target:.2f}",
                    "P&L":          colored_pnl(pnl),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # ── FLAGGED & COOLING quick view ──────────────────────────────────
        if flagged or cooling or sl_cooling:
            st.markdown("---")
            c1, c2, c3 = st.columns(3)
            with c1:
                st.markdown(f"#### 🚩 Flagged ({len(flagged)})")
                if flagged:
                    rows = [{
                        "Stock":   r.get("plain_name", s),
                        "State":   _state_label(r.get("state","")),
                        "RSI":     r.get("flagged_rsi") or "-",
                        "Since":   fmt_dt(r.get("flagged_at")),
                    } for s, r in flagged.items()]
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)
                else:
                    st.caption("None")
            with c2:
                st.markdown(f"#### ❄️ Cooling ({len(cooling)})")
                if cooling:
                    rows = [{
                        "Stock":  r.get("plain_name", s),
                        "State":  _state_label(r.get("state","")),
                        "Last":   r.get("entry_price") or "-",
                    } for s, r in cooling.items()]
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)
                else:
                    st.caption("None")
            with c3:
                # Stocks that hit the LIVE daily-RSI stoploss — stricter cooling gate
                st.markdown(f"#### 🛑 Stoploss-Cooling ({len(sl_cooling)})")
                if sl_cooling:
                    rows = [{
                        "Stock":      r.get("plain_name", s),
                        "Side":       "BUY" if r.get("state") == "COOLING_BUY_STOPLOSS" else "SELL",
                        "Entry ₹":    r.get("entry_price") or "-",
                        "Exit Time":  fmt_dt(r.get("entry_time")),
                    } for s, r in sl_cooling.items()]
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)
                    st.caption("Returns to GENERAL only when RSI recovers past the stricter "
                               "stoploss-cooling threshold.")
                else:
                    st.caption("None")

    # ══════════════════════════════════════════════════════════════════════════
    # EXIT SIGNALS
    # ══════════════════════════════════════════════════════════════════════════
    elif nav == "🔴 Exit Signals":
        st.subheader(f"🔴 Exit Signals — {len(exits)} total")
        if not exits:
            st.info("No exit signals yet.")
        else:
            rows = []
            for e in exits[:200]:
                entry_p = e.get("entry_price") or 0
                exit_p  = e.get("price") or 0
                pnl     = e.get("pnl_pct", 0) or 0
                side    = "BUY" if "BUY" in e.get("type","") else "SELL"
                rows.append({
                    "Stock":      e.get("stock") or e.get("plain_name",""),
                    "Side":       side,
                    "Label":      e.get("label",""),
                    "Entry ₹":    f"₹{entry_p:.2f}" if entry_p else "-",
                    "Exit ₹":     f"₹{exit_p:.2f}",
                    "Exit Time":  fmt_dt(e.get("time")),
                    "Level":      e.get("entry_level","-"),
                    "P&L":        colored_pnl(float(pnl) if pnl else 0),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════════
    # WATCHED — Strategy 4 shows FLAGGED here
    # ══════════════════════════════════════════════════════════════════════════
    elif nav == "👁️ Watched Stocks":
        st.subheader(f"🚩 Flagged — {len(flagged)} stocks waiting for cooldown")
        if not flagged:
            st.info("No flagged stocks.")
        else:
            rows = []
            for sym, r in sorted(flagged.items(),
                                 key=lambda x: x[1].get("flagged_at") or "" or "",
                                 reverse=True):
                cur = live_price(sym, r.get("current_price") or 0)
                rows.append({
                    "Stock":   r.get("plain_name", sym),
                    "Side":    "BUY" if r.get("state") == "FLAGGED_BUY" else "SELL",
                    "Flag RSI": r.get("flagged_rsi") or "-",
                    "Since":   fmt_dt(r.get("flagged_at")),
                    "Now ₹":   f"₹{cur:.2f}",
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════════
    # SIGNAL LOG
    # ══════════════════════════════════════════════════════════════════════════
    elif nav == "📋 Signal Log":
        st.subheader("📋 Signal Log — Scanner 4")
        if not signal_log:
            st.info("No signals yet.")
        else:
            rows = []
            for e in sorted(signal_log, key=lambda x: x.get("time","") or "",
                            reverse=True):
                t  = str(e.get("type",""))
                px = e.get("price") or 0
                info_parts = []
                if e.get("target_price"):
                    info_parts.append(f"Target:₹{e['target_price']:.2f}")
                if e.get("daily_rsi") is not None:
                    info_parts.append(f"D-RSI:{e['daily_rsi']}")
                if e.get("pnl_pct") is not None and "EXIT" in t:
                    info_parts.append(f"P&L:{e['pnl_pct']:+.2f}%")
                rows.append({
                    "Type":  e.get("label", t),
                    "Stock": e.get("stock") or e.get("plain_name",""),
                    "Price": f"₹{float(px):.2f}" if px else "-",
                    "Time":  fmt_dt(e.get("time")),
                    "Info":  " | ".join(info_parts),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ══════════════════════════════════════════════════════════════════════════
    # SETTINGS
    # ══════════════════════════════════════════════════════════════════════════
    elif nav == "⚙️ Settings":
        st.subheader(f"⚙️ Settings — {scfg.get('name', scanner_id)}")

        # ── Stock-group selectors ───────────────────────────────────────
        st.markdown("### 📚 STOCK GROUPS")
        try:
            from core.stock_groups import get_stock_groups
            _sg     = get_stock_groups()
            _sg.reload()  # pick up any new CSVs the user dropped in
            _groups = _sg.list()
            _counts = {g: _sg.size(g) for g in _groups}
        except Exception as _e:
            _groups = ["nifty650"]
            _counts = {"nifty650": 0}
            st.warning(f"Stock-group helper unavailable: {_e}")
        st.caption(
            "Add new groups by dropping a CSV into `data/stock_groups/` with "
            "the same columns as `data/stocks.csv` "
            "(`plain_name,fyers_symbol,company_name`). "
            "Filename (without `.csv`) becomes the group name. "
            "**A scanner restart is required for group changes to take effect.**"
        )
        gcol1, gcol2 = st.columns(2)
        _bg_cur = scfg.get("buy_stock_group", "nifty650")
        _sg_cur = scfg.get("sell_stock_group", "nifty650")
        if _bg_cur not in _groups: _bg_cur = "nifty650"
        if _sg_cur not in _groups: _sg_cur = "nifty650"
        with gcol1:
            buy_stock_group = st.selectbox(
                "🟢 BUY universe",
                _groups,
                index=_groups.index(_bg_cur) if _bg_cur in _groups else 0,
                format_func=lambda g: f"{g}  ({_counts.get(g,0)} stocks)",
                key="s4_buy_group",
            )
        with gcol2:
            sell_stock_group = st.selectbox(
                "🔴 SELL universe",
                _groups,
                index=_groups.index(_sg_cur) if _sg_cur in _groups else 0,
                format_func=lambda g: f"{g}  ({_counts.get(g,0)} stocks)",
                key="s4_sell_group",
            )

        st.markdown("---")
        st.markdown("### ⏱ TIMEFRAME")
        sig_tf_opts = ["5", "10", "15"]
        _cur_tf     = str(cfg.get("signal_timeframe", "15"))
        if _cur_tf not in sig_tf_opts:
            _cur_tf = "15"
        signal_timeframe = st.selectbox(
            "Signal timeframe (all entry/exit checks run on this TF)",
            sig_tf_opts,
            index=sig_tf_opts.index(_cur_tf),
            key="s4_sigtf",
        )

        st.markdown("---")
        st.markdown("### 🟢 BUY THRESHOLDS")
        bcol1, bcol2 = st.columns(2)
        with bcol1:
            buy1_rsi_lower      = st.slider("Buy 1 RSI lower",       50.0, 80.0,
                float(cfg.get("buy1_rsi_lower", 68)), step=0.5, key="s4_b1lo")
            buy1_rsi_upper      = st.slider("Buy 1 RSI upper",       60.0, 90.0,
                float(cfg.get("buy1_rsi_upper", 75)), step=0.5, key="s4_b1hi")
            buy1_cooldown_rsi   = st.slider("Buy 1 cooldown RSI",    40.0, 75.0,
                float(cfg.get("buy1_cooldown_rsi", 60)), step=0.5, key="s4_b1cd")
            buy2_rsi_trigger    = st.slider("Buy 2 trigger RSI",     10.0, 50.0,
                float(cfg.get("buy2_rsi_trigger", 30)), step=0.5, key="s4_b2t")
            buy3_drop_pct       = st.slider("Buy 3/4 drop %",         0.0, 10.0,
                float(cfg.get("buy3_drop_pct", 3.0)), step=0.25, key="s4_b3d")
        with bcol2:
            daily_rsi_buy_filter  = st.slider("Daily RSI filter (>)",  40.0, 85.0,
                float(cfg.get("daily_rsi_buy_filter", 65)), step=0.5, key="s4_dbf")
            weekly_rsi_buy_filter = st.slider("Weekly RSI filter (>)", 40.0, 85.0,
                float(cfg.get("weekly_rsi_buy_filter", 65)), step=0.5, key="s4_wbf")
            buy1_target_pct       = st.slider("Buy 1 target %",         1.0, 20.0,
                float(cfg.get("buy1_target_pct", 5.0)), step=0.25, key="s4_b1tg")
            buy23_target_pct      = st.slider("Buy 2/3/4 target %",     1.0, 20.0,
                float(cfg.get("buy23_target_pct", 6.0)), step=0.25, key="s4_b23tg")
            cooling_rsi_buy       = st.slider("Cooling → General RSI after Exit to less than (<)",
                20.0, 60.0,
                float(cfg.get("cooling_rsi_buy", 40)), step=0.5, key="s4_cb")

        st.markdown("---")
        st.markdown("### 🔴 SELL THRESHOLDS")
        scol1, scol2 = st.columns(2)
        with scol1:
            sell1_rsi_upper     = st.slider("Sell 1 RSI upper",       20.0, 50.0,
                float(cfg.get("sell1_rsi_upper", 32)), step=0.5, key="s4_s1hi")
            sell1_rsi_lower     = st.slider("Sell 1 RSI lower",       10.0, 40.0,
                float(cfg.get("sell1_rsi_lower", 25)), step=0.5, key="s4_s1lo")
            sell1_cooldown_rsi  = st.slider("Sell 1 cooldown RSI",    25.0, 60.0,
                float(cfg.get("sell1_cooldown_rsi", 40)), step=0.5, key="s4_s1cd")
            sell2_rsi_trigger   = st.slider("Sell 2 trigger RSI",     50.0, 90.0,
                float(cfg.get("sell2_rsi_trigger", 68)), step=0.5, key="s4_s2t")
            sell3_rise_pct      = st.slider("Sell 3/4 rise %",         0.0, 10.0,
                float(cfg.get("sell3_rise_pct", 3.0)), step=0.25, key="s4_s3r")
        with scol2:
            daily_rsi_sell_filter  = st.slider("Daily RSI filter (<)",  15.0, 60.0,
                float(cfg.get("daily_rsi_sell_filter", 40)), step=0.5, key="s4_dsf")
            weekly_rsi_sell_filter = st.slider("Weekly RSI filter (<)", 15.0, 60.0,
                float(cfg.get("weekly_rsi_sell_filter", 40)), step=0.5, key="s4_wsf")
            sell1_target_pct       = st.slider("Sell 1 target %",         1.0, 20.0,
                float(cfg.get("sell1_target_pct", 5.0)), step=0.25, key="s4_s1tg")
            sell23_target_pct      = st.slider("Sell 2/3/4 target %",     1.0, 20.0,
                float(cfg.get("sell23_target_pct", 6.0)), step=0.25, key="s4_s23tg")
            cooling_rsi_sell       = st.slider("Cooling → General RSI after Exit to greater than (>)",
                40.0, 80.0,
                float(cfg.get("cooling_rsi_sell", 60)), step=0.5, key="s4_cs")

        st.markdown("---")
        st.markdown("### ⏰ EXIT (once-per-day check)")
        ecol1, ecol2, ecol3 = st.columns(3)
        with ecol1:
            exit_check_time = st.text_input("Exit check time (HH:MM)",
                value=str(cfg.get("exit_check_time", "15:25")), key="s4_ect")
        with ecol2:
            daily_rsi_exit = st.slider("BUY Daily RSI exit (<)",     20.0, 70.0,
                float(cfg.get("daily_rsi_exit", 50)), step=0.5, key="s4_dxb")
        with ecol3:
            daily_rsi_exit_sell = st.slider("SELL Daily RSI exit (>)", 30.0, 80.0,
                float(cfg.get("daily_rsi_exit_sell", 50)), step=0.5, key="s4_dxs")

        st.markdown("### 🛑 STOPLOSS — Daily RSI (LIVE — checked on every tick during market hours)")
        slcol1, slcol2 = st.columns(2)
        with slcol1:
            daily_rsi_stoploss_buy = st.slider(
                "BUY stoploss — Daily RSI crosses below (<)",
                20.0, 60.0,
                float(cfg.get("daily_rsi_stoploss_buy", 48)),
                step=0.5, key="s4_slb",
                help="LIVE check. When live Daily RSI (yesterday close + today's current price as the day's close) "
                     "crosses below this value DURING market hours, an immediate Exit Buy fires. "
                     "Backed up by a candle-close check as a safety net.")
            stoploss_cooling_rsi_buy = st.slider(
                "After STOPLOSS — back to General when RSI rises above (>)",
                40.0, 85.0,
                float(cfg.get("stoploss_cooling_rsi_buy", 68)),
                step=0.5, key="s4_slbcd",
                help="After a stoploss exit, the stock is locked in COOLING_BUY_STOPLOSS until "
                     "the signal-TF RSI closes above this threshold (default 68). Stricter than "
                     "the regular cooling gate.")
        with slcol2:
            daily_rsi_stoploss_sell = st.slider(
                "SELL stoploss — Daily RSI crosses above (>)",
                40.0, 80.0,
                float(cfg.get("daily_rsi_stoploss_sell", 55)),
                step=0.5, key="s4_sls",
                help="LIVE check during market hours. Mirror of the BUY stoploss.")
            stoploss_cooling_rsi_sell = st.slider(
                "After STOPLOSS — back to General when RSI drops below (<)",
                15.0, 60.0,
                float(cfg.get("stoploss_cooling_rsi_sell", 32)),
                step=0.5, key="s4_slscd",
                help="After a sell-side stoploss, locks in COOLING_SELL_STOPLOSS until RSI drops "
                     "below this threshold (default 32). Stricter mirror of regular sell cooling.")

        st.markdown("---")
        st.markdown("### 📤 WEBHOOKS")
        st.caption("4 separate URLs — entries and exits go to different endpoints "
                   "for each side.")
        wh = scfg.get("webhooks", {})
        wcol1, wcol2 = st.columns(2)
        with wcol1:
            buy_webhook_url      = st.text_input("🟢 BUY entries URL",
                value=wh.get("buy_webhook_url", ""),
                placeholder="https://your-app.com/webhook/buy",
                help="Receives BUY 1 / 2 / 3 / 4 entry signals.",
                key="s4_buy_url")
            buy_exit_webhook_url = st.text_input("🟢 BUY EXIT URL",
                value=wh.get("buy_exit_webhook_url", ""),
                placeholder="https://your-app.com/webhook/buy-exit",
                help="Receives all buy exits: target hit, daily-RSI exit (15:25), "
                     "and daily-RSI stoploss.",
                key="s4_buy_exit_url")
        with wcol2:
            sell_webhook_url      = st.text_input("🔴 SELL entries URL",
                value=wh.get("sell_webhook_url", ""),
                placeholder="https://your-app.com/webhook/sell",
                help="Receives SELL 1 / 2 / 3 / 4 entry signals.",
                key="s4_sell_url")
            sell_exit_webhook_url = st.text_input("🔴 SELL EXIT URL",
                value=wh.get("sell_exit_webhook_url", ""),
                placeholder="https://your-app.com/webhook/sell-exit",
                help="Receives all sell exits: target hit, daily-RSI exit (15:25), "
                     "and daily-RSI stoploss.",
                key="s4_sell_exit_url")

        scanner_name = st.text_input("Scanner Name",
            value=scfg.get("name", "Scanner 4 — RSI Momentum"), key="s4_name")

        st.markdown("---")
        st.markdown("### 🔁 RESET — clear in-memory positions and start fresh")
        st.caption("Writes a reset flag the scanner picks up within ~10 seconds. "
                   "Signal log is preserved; only active/flagged/cooling states are cleared.")
        rcol1, rcol2 = st.columns(2)

        def _write_reset_flag(scanner_id_arg: str, side: str):
            flag = os.path.join(ROOT, "data",
                                f"reset_{scanner_id_arg}_{side}.flag")
            os.makedirs(os.path.dirname(flag), exist_ok=True)
            with open(flag, "w") as f:
                f.write(f"{datetime.now(IST).isoformat()}")

        with rcol1:
            if st.button("🔁 Reset BUY side", type="secondary",
                         use_container_width=True, key="s4_reset_buy"):
                _write_reset_flag("scanner_4", "buy")
                st.success("✅ Reset flag dropped — buy-side positions will "
                           "clear within 10 s (or on next scanner startup).")
        with rcol2:
            if st.button("🔁 Reset SELL side", type="secondary",
                         use_container_width=True, key="s4_reset_sell"):
                _write_reset_flag("scanner_4", "sell")
                st.success("✅ Reset flag dropped — sell-side positions will "
                           "clear within 10 s (or on next scanner startup).")

        st.markdown("---")
        if st.button("💾 Save Settings", type="primary",
                     use_container_width=True, key="s4_save"):
            config["scanners"]["scanner_4"]["name"]             = scanner_name
            config["scanners"]["scanner_4"]["buy_stock_group"]  = buy_stock_group
            config["scanners"]["scanner_4"]["sell_stock_group"] = sell_stock_group
            config["scanners"]["scanner_4"]["settings"] = {
                "signal_timeframe":         signal_timeframe,
                "buy1_rsi_lower":           buy1_rsi_lower,
                "buy1_rsi_upper":           buy1_rsi_upper,
                "buy1_cooldown_rsi":        buy1_cooldown_rsi,
                "buy2_rsi_trigger":         buy2_rsi_trigger,
                "buy3_drop_pct":            buy3_drop_pct,
                "daily_rsi_buy_filter":     daily_rsi_buy_filter,
                "weekly_rsi_buy_filter":    weekly_rsi_buy_filter,
                "buy1_target_pct":          buy1_target_pct,
                "buy23_target_pct":         buy23_target_pct,
                "daily_rsi_exit":           daily_rsi_exit,
                "daily_rsi_stoploss_buy":   daily_rsi_stoploss_buy,
                "stoploss_cooling_rsi_buy": stoploss_cooling_rsi_buy,
                "cooling_rsi_buy":          cooling_rsi_buy,
                "sell1_rsi_upper":          sell1_rsi_upper,
                "sell1_rsi_lower":          sell1_rsi_lower,
                "sell1_cooldown_rsi":       sell1_cooldown_rsi,
                "sell2_rsi_trigger":        sell2_rsi_trigger,
                "sell3_rise_pct":           sell3_rise_pct,
                "daily_rsi_sell_filter":    daily_rsi_sell_filter,
                "weekly_rsi_sell_filter":   weekly_rsi_sell_filter,
                "sell1_target_pct":         sell1_target_pct,
                "sell23_target_pct":        sell23_target_pct,
                "daily_rsi_exit_sell":      daily_rsi_exit_sell,
                "daily_rsi_stoploss_sell":   daily_rsi_stoploss_sell,
                "stoploss_cooling_rsi_sell": stoploss_cooling_rsi_sell,
                "cooling_rsi_sell":          cooling_rsi_sell,
                "exit_check_time":          exit_check_time,
            }
            config["scanners"]["scanner_4"]["webhooks"] = {
                "buy_webhook_url":       buy_webhook_url,
                "buy_exit_webhook_url":  buy_exit_webhook_url,
                "sell_webhook_url":      sell_webhook_url,
                "sell_exit_webhook_url": sell_exit_webhook_url,
            }
            save_config(config)
            trigger_rescan("scanner_4")
            st.success("✅ Saved — scanner_4 will recompute state from history "
                       "under new settings within ~10 s.")

        # ── Saved settings summary (refreshed from disk on every render) ──
        st.markdown("---")
        st.markdown("### 📋 Saved Settings")
        _saved      = load_config().get("scanners", {}).get("scanner_4", {})
        _s          = _saved.get("settings", {})
        _saved_tf   = _s.get("signal_timeframe", "15")

        st.markdown(f"**Signal timeframe:** `{_saved_tf}m`")

        bcol_s, scol_s = st.columns(2)
        with bcol_s:
            st.markdown("**🟢 BUY side**")
            st.code(
                f"Buy 1 RSI lower             : {_s.get('buy1_rsi_lower', 68)}\n"
                f"Buy 1 RSI upper             : {_s.get('buy1_rsi_upper', 75)}\n"
                f"Buy 1 cooldown RSI          : {_s.get('buy1_cooldown_rsi', 60)}\n"
                f"Buy 2 trigger RSI           : {_s.get('buy2_rsi_trigger', 30)}\n"
                f"Buy 3/4 drop %              : {_s.get('buy3_drop_pct', 3.0)}\n"
                f"Daily RSI filter (>)        : {_s.get('daily_rsi_buy_filter', 65)}\n"
                f"Weekly RSI filter (>)       : {_s.get('weekly_rsi_buy_filter', 65)}\n"
                f"Buy 1 target %              : {_s.get('buy1_target_pct', 5.0)}\n"
                f"Buy 2/3/4 target %          : {_s.get('buy23_target_pct', 6.0)}\n"
                f"Daily RSI exit (15:25)      : {_s.get('daily_rsi_exit', 50)}\n"
                f"Daily RSI STOPLOSS (<)      : {_s.get('daily_rsi_stoploss_buy', 48)}\n"
                f"Cooling → General (<)       : {_s.get('cooling_rsi_buy', 40)}\n"
                f"Stoploss-cooling → Gen (>)  : {_s.get('stoploss_cooling_rsi_buy', 68)}",
                language="text"
            )
        with scol_s:
            st.markdown("**🔴 SELL side**")
            st.code(
                f"Sell 1 RSI upper            : {_s.get('sell1_rsi_upper', 32)}\n"
                f"Sell 1 RSI lower            : {_s.get('sell1_rsi_lower', 25)}\n"
                f"Sell 1 cooldown RSI         : {_s.get('sell1_cooldown_rsi', 40)}\n"
                f"Sell 2 trigger RSI          : {_s.get('sell2_rsi_trigger', 68)}\n"
                f"Sell 3/4 rise %             : {_s.get('sell3_rise_pct', 3.0)}\n"
                f"Daily RSI filter (<)        : {_s.get('daily_rsi_sell_filter', 40)}\n"
                f"Weekly RSI filter (<)       : {_s.get('weekly_rsi_sell_filter', 40)}\n"
                f"Sell 1 target %             : {_s.get('sell1_target_pct', 5.0)}\n"
                f"Sell 2/3/4 target %         : {_s.get('sell23_target_pct', 6.0)}\n"
                f"Daily RSI exit (15:25)      : {_s.get('daily_rsi_exit_sell', 50)}\n"
                f"Daily RSI STOPLOSS (>)      : {_s.get('daily_rsi_stoploss_sell', 55)}\n"
                f"Cooling → General (>)       : {_s.get('cooling_rsi_sell', 60)}\n"
                f"Stoploss-cooling → Gen (<)  : {_s.get('stoploss_cooling_rsi_sell', 32)}",
                language="text"
            )
        st.caption(f"Exit-check time: **{_s.get('exit_check_time', '15:25')}** IST")

        # ── Plain-language rules ──────────────────────────────────────────
        st.markdown("---")
        st.markdown("### 📖 Strategy Rules (in plain language)")
        st.markdown(
f"""
**Signal timeframe:** all checks below run on **{_saved_tf}-minute candle closes** (except the daily-RSI exit which fires once a day at {_s.get('exit_check_time', '15:25')}).

**🟢 BUY side**
1. **BUY 1 — direct (Path A):** {_saved_tf}m RSI closes inside ({_s.get('buy1_rsi_lower', 68)}, {_s.get('buy1_rsi_upper', 75)}) **and** Daily RSI > {_s.get('daily_rsi_buy_filter', 65)} **and** Weekly RSI > {_s.get('weekly_rsi_buy_filter', 65)} → **BUY 1** fires at that candle close.
2. **BUY 1 — flagged (Path B):** if {_saved_tf}m RSI closes **above {_s.get('buy1_rsi_upper', 75)}** (with the same D/W filters passing), the stock is **FLAGGED**. Any later {_saved_tf}m candle that closes with RSI **< {_s.get('buy1_cooldown_rsi', 60)}** (D/W still passing) fires **BUY 1** at that close.
3. **BUY 2:** after BUY 1, the first {_saved_tf}m close with RSI **< {_s.get('buy2_rsi_trigger', 30)}** (D-RSI still > {_s.get('daily_rsi_buy_filter', 65)}) fires BUY 2.
4. **BUY 3 & BUY 4:** after the previous Buy, each {_s.get('buy3_drop_pct', 3.0)}% drop below the last Buy's price (D-RSI > {_s.get('daily_rsi_buy_filter', 65)}) fires the next Buy. Max 4 Buys.
5. **Target / Exit:**
   - BUY 1 target = entry × (1 + {_s.get('buy1_target_pct', 5.0)}%)
   - BUY 2/3/4 target = entry × (1 + {_s.get('buy23_target_pct', 6.0)}%)
   - **🛑 LIVE STOPLOSS:** during market hours, if **live Daily RSI** *crosses* below **{_s.get('daily_rsi_stoploss_buy', 48)}** on any tick → immediate exit (no waiting for candle close).
   - **Daily-RSI exit (once at {_s.get('exit_check_time', '15:25')}):** if Daily RSI **< {_s.get('daily_rsi_exit', 50)}** → exit.
6. **Cooling — TWO modes:**
   - **Normal cooling (after target / 15:25 exit):** wait for {_saved_tf}m RSI **< {_s.get('cooling_rsi_buy', 40)}** → GENERAL.
   - **Stoploss cooling (after live-stoploss exit):** stricter — wait for {_saved_tf}m RSI **> {_s.get('stoploss_cooling_rsi_buy', 68)}** → GENERAL. Stock must rally before being re-eligible.

**🔴 SELL side** *(mirror of buy)*
1. **SELL 1 — direct:** {_saved_tf}m RSI closes inside ({_s.get('sell1_rsi_lower', 25)}, {_s.get('sell1_rsi_upper', 32)}) **and** Daily RSI < {_s.get('daily_rsi_sell_filter', 40)} **and** Weekly RSI < {_s.get('weekly_rsi_sell_filter', 40)} → **SELL 1**.
2. **SELL 1 — flagged:** {_saved_tf}m RSI **below {_s.get('sell1_rsi_lower', 25)}** → FLAGGED; later candle with RSI **> {_s.get('sell1_cooldown_rsi', 40)}** (D/W still passing) fires SELL 1.
3. **SELL 2:** first {_saved_tf}m close with RSI **> {_s.get('sell2_rsi_trigger', 68)}** (D-RSI < {_s.get('daily_rsi_sell_filter', 40)}) → SELL 2.
4. **SELL 3 & 4:** each {_s.get('sell3_rise_pct', 3.0)}% rise above the last Sell's price (D-RSI < {_s.get('daily_rsi_sell_filter', 40)}) → next Sell. Max 4 Sells.
5. **Target / Exit:**
   - SELL 1 target = entry × (1 − {_s.get('sell1_target_pct', 5.0)}%)
   - SELL 2/3/4 target = entry × (1 − {_s.get('sell23_target_pct', 6.0)}%)
   - **🛑 LIVE STOPLOSS:** during market hours, if **live Daily RSI** *crosses* above **{_s.get('daily_rsi_stoploss_sell', 55)}** on any tick → immediate exit.
   - **Daily-RSI exit (once at {_s.get('exit_check_time', '15:25')}):** if Daily RSI **> {_s.get('daily_rsi_exit_sell', 50)}** → exit.
6. **Cooling — TWO modes:**
   - **Normal cooling (after target / 15:25 exit):** wait for {_saved_tf}m RSI **> {_s.get('cooling_rsi_sell', 60)}** → GENERAL.
   - **Stoploss cooling (after live-stoploss exit):** stricter — wait for {_saved_tf}m RSI **< {_s.get('stoploss_cooling_rsi_sell', 32)}** → GENERAL.
"""
        )


# ─── Data rendering ────────────────────────────────────────────────────────────
# Uses st.fragment(run_every=15) when available (Streamlit ≥ 1.37) so only the
# data area refreshes every 15 s — sidebar and nav stay stable, no flash.

def _render_data():
    global live_prices
    live_prices = load_live_prices()

    config   = load_config()
    scanners = config.get("scanners", {})
    nav      = st.session_state.get("nav_select", "🟢 Active Buys")

    scanner_ids    = list(scanners.keys())
    scanner_labels = []
    for sid in scanner_ids:
        scfg  = scanners[sid]
        name  = scfg.get("name", sid)
        active= scfg.get("active", False)
        scanner_labels.append(name if active else f"{name} 🔒")

    scanner_tabs = st.tabs(scanner_labels)

    for tab, scanner_id in zip(scanner_tabs, scanner_ids):
        with tab:
            scfg   = scanners[scanner_id]
            active = scfg.get("active", False)
            sid    = scanner_id   # short alias for keys

            if not active:
                st.markdown("## 🔒 Coming Soon")
                st.info(f"**{scfg.get('name', scanner_id)}** will be enabled when the strategy is ready.")
                continue

            # Strategy 3 uses a completely different UI
            if scfg.get("strategy_type") == "webhook_mirror":
                _render_s3_tab(scanner_id, scfg, config, nav)
                continue

            # Strategy 4 (RSI Momentum) — own tab
            if scfg.get("strategy_type") == "momentum":
                _render_s4_tab(scanner_id, scfg, config, nav)
                continue

            state      = load_scanner_state(scanner_id)
            records    = state.get("records", {})
            signal_log = state.get("signal_log", [])

            active_buys   = {s: r for s, r in records.items() if r.get("state") == "ACTIVE_BUY"}
            watched       = {s: r for s, r in records.items() if r.get("state") == "WATCHED"}
            active_sells  = {s: r for s, r in records.items() if r.get("state") == "ACTIVE_SELL"}
            watched_sells = {s: r for s, r in records.items() if r.get("state") == "WATCHED_SELL"}
            sl_cooling12  = {s: r for s, r in records.items()
                             if r.get("state") in ("COOLING_BUY_STOPLOSS",
                                                    "COOLING_SELL_STOPLOSS")}
            exit_signals = sorted(
                [e for e in signal_log
                 if e.get("type") in ("EXIT", "EXIT_STOPLOSS",
                                       "EXIT_SELL", "EXIT_SELL_STOPLOSS")],
                key=lambda e: e.get("time", ""),
                reverse=True
            )

            m1,m2,m3,m4,m5,m6,m7 = st.columns(7)
            m1.metric("🟢 Active Buys",   len(active_buys))
            m2.metric("👁️ Watch Buy",     len(watched))
            m3.metric("🔴 Active Sells",  len(active_sells))
            m4.metric("👁️ Watch Sell",    len(watched_sells))
            m5.metric("🛑 SL-cooling",    len(sl_cooling12))
            m6.metric("🚪 Exited",        len(exit_signals))
            m7.metric("📋 Stocks",        len(records))
            st.markdown("---")

            # ══════════════════════════════════════════════════════════════════
            # ACTIVE BUYS
            # ══════════════════════════════════════════════════════════════════
            if nav == "🟢 Active Buys":
                st.subheader(f"🟢 Active Buys — {len(active_buys)} stocks")

                if not active_buys:
                    st.info("No active buy signals.")
                else:
                    sorted_buys = sorted(active_buys.items(),
                        key=lambda x: x[1].get("buy_signal_at") or "", reverse=True)

                    rows = []
                    for sym, r in sorted_buys:
                        ref    = r.get("reference_price") or 0
                        buy    = r.get("buy_price") or 0
                        cur    = live_price(sym, r.get("current_price") or buy)
                        pnl    = round(((cur-buy)/buy*100),2) if buy else 0
                        missed = " ⚡Missed" if r.get("gap_fill") else ""
                        rows.append({
                            "Stock":    r.get("plain_name",sym) + missed,
                            "Status":   f"Avg {r.get('avg_count',0)}" if r.get("avg_count",0)>0 else "BUY",
                            "Ref ₹":    f"₹{ref:.2f}",
                            "Ref Time": fmt_dt(r.get("reference_time")),
                            "Buy ₹":    f"₹{buy:.2f}",
                            "Buy Time": fmt_dt(r.get("buy_time") or r.get("buy_signal_at")),
                            "Now ₹":    f"₹{cur:.2f}",
                            "Avgs":     r.get("avg_count",0),
                            "P&L":      colored_pnl(pnl),
                        })
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)

                    st.markdown("---")
                    st.markdown("### Full Detail")

                    for sym, r in sorted_buys:
                        ref       = r.get("reference_price") or 0
                        ref_time  = fmt_dt(r.get("reference_time"))
                        buy       = r.get("buy_price") or 0
                        buy_time  = fmt_dt(r.get("buy_time") or r.get("buy_signal_at"))
                        cur       = live_price(sym, r.get("current_price") or buy)
                        avg_cnt   = r.get("avg_count",0)
                        last_avg  = r.get("last_avg_price") or buy
                        last_avg_t= fmt_dt(r.get("last_avg_time"))
                        pnl       = round(((cur-buy)/buy*100),2) if buy else 0
                        status    = f"Avg {avg_cnt}" if avg_cnt>0 else "BUY"

                        missed_badge = "  &nbsp; ⚡ `MISSED — scanner was offline`" if r.get("gap_fill") else ""
                        st.markdown(f"### 🟢 **{r.get('plain_name',sym)}** &nbsp; `{status}` &nbsp; {colored_pnl(pnl)}" + missed_badge)
                        st.caption(r.get("company_name",""))

                        c1,c2,c3,c4,c5 = st.columns(5)
                        c1.metric("Ref Price", f"₹{ref:.2f}",  ref_time)
                        c2.metric("Buy Price", f"₹{buy:.2f}",  buy_time)
                        c3.metric("Now",       f"₹{cur:.2f}")
                        c4.metric("P&L",       f"{pnl:+.2f}%")
                        c5.metric("RSI Watch", str(r.get("rsi_at_watch","-")))

                        avg_entries = r.get("avg_entries",[])
                        if avg_entries:
                            avg_rows = []
                            for avg in avg_entries:
                                avg_rows.append({
                                    "Level":    f"Avg {avg.get('avg_num','')}",
                                    "Price":    f"₹{avg.get('price',0):.2f}",
                                    "Time":     fmt_dt(avg.get("signal_time")),
                                    "Drop %":   f"{avg.get('drop_pct',0):.2f}%",
                                })
                            st.dataframe(pd.DataFrame(avg_rows),
                                         use_container_width=False, hide_index=True)
                            st.caption(f"Last Avg: ₹{last_avg:.2f} @ {last_avg_t}  |  Total: {avg_cnt}")

                        st.divider()

                # ── Active SELLS (shorts) ─────────────────────────────────
                if active_sells:
                    st.markdown("---")
                    st.subheader(f"🔴 Active Sells (Shorts) — {len(active_sells)} stock(s)")
                    rows = []
                    for sym, r in sorted(active_sells.items(),
                                          key=lambda x: x[1].get("buy_signal_at") or "" or "",
                                          reverse=True):
                        ref    = r.get("reference_price") or 0
                        short  = r.get("buy_price") or 0     # field reused for sell entry
                        cur    = live_price(sym, r.get("current_price") or short)
                        # P&L for short = (entry - current) / entry
                        pnl    = round(((short-cur)/short*100),2) if short else 0
                        status = (f"Avg Short {r.get('avg_count',0)}"
                                  if r.get("avg_count",0)>0 else "SHORT")
                        rows.append({
                            "Stock":     r.get("plain_name",sym),
                            "Status":    status,
                            "Ref ₹":     f"₹{ref:.2f}",
                            "Ref Time":  fmt_dt(r.get("reference_time")),
                            "Short ₹":   f"₹{short:.2f}",
                            "Short Time":fmt_dt(r.get("buy_time") or r.get("buy_signal_at")),
                            "Now ₹":     f"₹{cur:.2f}",
                            "Avgs":      r.get("avg_count",0),
                            "P&L":       colored_pnl(pnl),
                        })
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)

                # ── Stoploss-cooling panel (stocks that exited via live D-RSI
                # stoploss; locked until scan-TF RSI rallies past threshold) ──
                if sl_cooling12:
                    st.markdown("---")
                    st.markdown(f"### 🛑 Stoploss-Cooling — {len(sl_cooling12)} stock(s)")
                    st.caption("Stocks that hit the live daily-RSI stoploss. "
                               "Locked until scan-TF RSI recovers past the "
                               "stoploss-cooling threshold.")
                    rows = []
                    for sym, r in sorted(sl_cooling12.items(),
                                          key=lambda x: x[1].get("exit_signal_at") or "" or "",
                                          reverse=True):
                        entry_p = r.get("buy_price") or 0
                        exit_p  = r.get("exit_price") or 0
                        d_rsi   = r.get("rsi_at_exit") or "-"
                        is_sell = r.get("state") == "COOLING_SELL_STOPLOSS"
                        if is_sell and entry_p:
                            pnl = round(((entry_p-exit_p)/entry_p*100),2)
                        elif entry_p:
                            pnl = round(((exit_p-entry_p)/entry_p*100),2)
                        else:
                            pnl = 0
                        rows.append({
                            "Stock":      r.get("plain_name", sym),
                            "Side":       "SELL" if is_sell else "BUY",
                            "Entry ₹":    f"₹{entry_p:.2f}",
                            "Exit ₹":     f"₹{exit_p:.2f}",
                            "Exit Time":  fmt_dt(r.get("exit_time")),
                            "D-RSI @ SL": d_rsi,
                            "P&L":        colored_pnl(pnl),
                        })
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)

            # ══════════════════════════════════════════════════════════════════
            # EXIT SIGNALS
            # ══════════════════════════════════════════════════════════════════
            elif nav == "🔴 Exit Signals":
                st.subheader(f"🔴 Exit Signals — {len(exit_signals)} total")

                if not exit_signals:
                    st.info("No exit signals yet.")
                else:
                    rows = []
                    for e in exit_signals[:200]:
                        buy_p  = e.get("buy_price") or 0
                        exit_p = e.get("price") or 0
                        pnl    = round(((exit_p-buy_p)/buy_p*100),2) if buy_p else 0
                        missed = " ⚠️ MISSED" if e.get("gap_fill") else ""
                        rows.append({
                            "Stock":     e.get("plain_name","") + missed,
                            "Buy ₹":     f"₹{buy_p:.2f}",
                            "Buy Time":  fmt_dt(e.get("buy_time")),
                            "Exit ₹":    f"₹{exit_p:.2f}",
                            "Exit Time": fmt_dt(e.get("time")),
                            "Avgs":      e.get("avg_count",0),
                            "RSI Exit":  e.get("rsi_exit","-"),
                            "P&L":       colored_pnl(pnl),
                        })
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)

                    st.markdown("---")
                    st.markdown("### Full Detail")
                    for e in exit_signals[:100]:
                        buy_p    = e.get("buy_price") or 0
                        buy_time = fmt_dt(e.get("buy_time"))
                        exit_p   = e.get("price") or 0
                        exit_time= fmt_dt(e.get("time"))
                        pnl      = round(((exit_p-buy_p)/buy_p*100),2) if buy_p else 0
                        missed   = e.get("gap_fill", False)

                        name_str = e.get("plain_name","")
                        st.markdown(f"### 🔴 **{name_str}** &nbsp; `EXIT` &nbsp; {colored_pnl(pnl)}"
                                    + ("  &nbsp; ⚠️ `MISSED — scanner was off`" if missed else ""))
                        st.caption(e.get("company",""))
                        c1,c2,c3,c4 = st.columns(4)
                        c1.metric("Buy Price",  f"₹{buy_p:.2f}",  buy_time)
                        c2.metric("Exit Price", f"₹{exit_p:.2f}", exit_time)
                        c3.metric("P&L",        f"{pnl:+.2f}%")
                        c4.metric("RSI Exit",   str(e.get("rsi_exit","-")))
                        st.caption(f"Avgs taken: {e.get('avg_count',0)}")
                        st.divider()

            # ══════════════════════════════════════════════════════════════════
            # WATCHED STOCKS
            # ══════════════════════════════════════════════════════════════════
            elif nav == "👁️ Watched Stocks":
                cfg          = scfg.get("settings", {})
                drop_req     = float(cfg.get("drop_percent", 2.0))
                rise_req     = float(cfg.get("rise_percent", 2.0))

                # BUY watches
                st.subheader(f"👁️ Watched BUY — {len(watched)} stocks")
                if not watched:
                    st.info("No stocks in buy watch list.")
                else:
                    rows = []
                    for sym, r in sorted(watched.items(),
                        key=lambda x: x[1].get("watched_at") or "" or "", reverse=True):
                        ref         = r.get("reference_price") or 0
                        cur         = live_price(sym, r.get("current_price") or ref)
                        drop_so_far = round(((ref-cur)/ref*100),2) if ref else 0
                        still_need  = max(0, round(drop_req-drop_so_far,2))
                        rows.append({
                            "Since":       fmt_dt(r.get("watched_at")),
                            "Stock":       r.get("plain_name",sym),
                            "Ref ₹":       f"₹{ref:.2f}",
                            "Now ₹":       f"₹{cur:.2f}",
                            "Drop So Far": f"{drop_so_far:.2f}%",
                            "Need More":   f"{still_need:.2f}%",
                            "RSI Watch":   r.get("rsi_at_watch","-"),
                        })
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)

                # SELL watches
                st.markdown("---")
                st.subheader(f"👁️ Watched SELL — {len(watched_sells)} stocks")
                if not watched_sells:
                    st.info("No stocks in sell watch list.")
                else:
                    rows = []
                    for sym, r in sorted(watched_sells.items(),
                        key=lambda x: x[1].get("watched_at") or "" or "", reverse=True):
                        ref         = r.get("reference_price") or 0
                        cur         = live_price(sym, r.get("current_price") or ref)
                        rise_so_far = round(((cur-ref)/ref*100),2) if ref else 0
                        still_need  = max(0, round(rise_req-rise_so_far,2))
                        rows.append({
                            "Since":       fmt_dt(r.get("watched_at")),
                            "Stock":       r.get("plain_name",sym),
                            "Ref ₹":       f"₹{ref:.2f}",
                            "Now ₹":       f"₹{cur:.2f}",
                            "Rise So Far": f"{rise_so_far:.2f}%",
                            "Need More":   f"{still_need:.2f}%",
                            "RSI Watch":   r.get("rsi_at_watch","-"),
                        })
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)

            # ══════════════════════════════════════════════════════════════════
            # SIGNAL LOG
            # ══════════════════════════════════════════════════════════════════
            elif nav == "📋 Signal Log":
                st.subheader("📋 Signal Log")

                if not signal_log:
                    st.info("No signals yet.")
                else:
                    rows = []
                    for e in sorted(signal_log,
                                    key=lambda x: x.get("time","") or "",
                                    reverse=True):
                        t = e.get("type","")
                        p = e.get("price") or e.get("avg_price") or 0
                        missed = " ⚠️MISSED" if e.get("gap_fill") else ""
                        rows.append({
                            "Type":    e.get("label",t) + missed,
                            "Stock":   e.get("plain_name",""),
                            "Price":   f"₹{float(p):.2f}" if p else "-",
                            "Time":    fmt_dt(e.get("time")),
                            "Details": (
                                f"Ref:₹{e.get('ref_price',0):.2f} Drop:{e.get('drop_pct',0):.2f}%"
                                if t in ("BUY","AVG")
                                else f"Buy:₹{e.get('buy_price',0):.2f} RSI:{e.get('rsi_exit','-')}"
                            ),
                        })
                    st.dataframe(pd.DataFrame(rows),
                                 use_container_width=True, hide_index=True)

            # ══════════════════════════════════════════════════════════════════
            # SETTINGS
            # ══════════════════════════════════════════════════════════════════
            elif nav == "⚙️ Settings":
                st.subheader(f"⚙️ Settings — {scfg.get('name', scanner_id)}")

                # Stock-group selectors — buy + sell now both apply.
                try:
                    from core.stock_groups import get_stock_groups
                    _sg12 = get_stock_groups()
                    _sg12.reload()
                    _grps12 = _sg12.list()
                except Exception:
                    _grps12 = ["nifty650"]
                _bg = scfg.get("buy_stock_group",  "nifty650")
                _sgr = scfg.get("sell_stock_group", "nifty650")
                if _bg  not in _grps12: _bg  = "nifty650"
                if _sgr not in _grps12: _sgr = "nifty650"
                ggc1, ggc2 = st.columns(2)
                with ggc1:
                    buy_stock_group = st.selectbox(
                        "📚 BUY universe", _grps12,
                        index=_grps12.index(_bg),
                        help="Restricts buy-side entry scans to this group.",
                        key=f"bgroup_{sid}",
                    )
                with ggc2:
                    sell_stock_group = st.selectbox(
                        "📚 SELL universe", _grps12,
                        index=_grps12.index(_sgr),
                        help="Restricts sell-side entry scans to this group. "
                             "Restart scanner to apply.",
                        key=f"sgroup_{sid}",
                    )

                cfg = scfg.get("settings", {})
                col1, col2 = st.columns(2)

                with col1:
                    st.markdown("**RSI Parameters**")
                    rsi_period = st.number_input("RSI Period",    5, 50,
                        int(cfg.get("rsi_period",14)), step=1, key=f"rp_{sid}")
                    rsi_entry  = st.slider("RSI Entry (Watch)",   5, 40,
                        int(cfg.get("rsi_entry_threshold",20)), key=f"re_{sid}")
                    rsi_reset  = st.slider("RSI Reset",          50, 90,
                        int(cfg.get("rsi_reset_threshold",70)), key=f"rr_{sid}")
                    rsi_exit   = st.slider("RSI Exit",           50, 90,
                        int(cfg.get("rsi_exit_threshold",68)),  key=f"rx_{sid}")
                    drop_pct   = st.slider("Drop % (BUY)",      0.0, 10.0,
                        float(cfg.get("drop_percent",2.0)), step=0.25, key=f"dp_{sid}")
                    avg_pct    = st.slider("Avg Drop %",         0.0, 10.0,
                        float(cfg.get("avg_drop_percent",3.0)), step=0.25, key=f"ap_{sid}")

                with col2:
                    st.markdown("**Timeframes**")
                    tf_opts    = ["1","2","5","10","15","30","60","D","W"]
                    scan_tf    = st.selectbox("Scan TF",         tf_opts,
                        index=tf_opts.index(str(cfg.get("scan_timeframe","5"))),    key=f"st_{sid}")
                    trigger_tf = st.selectbox("Trigger TF",     tf_opts,
                        index=tf_opts.index(str(cfg.get("trigger_timeframe","1"))), key=f"tt_{sid}")
                    exit_tf    = st.selectbox("Exit TF",        tf_opts,
                        index=tf_opts.index(str(cfg.get("exit_timeframe","10"))),   key=f"et_{sid}")

                    st.markdown("**Higher TF Filters**")
                    c3, c4 = st.columns(2)
                    with c3:
                        daily_on = st.checkbox("Daily RSI Filter",
                            value=bool(cfg.get("daily_rsi_filter_enabled",True)), key=f"df_{sid}")
                        daily_th = st.slider("Daily RSI >", 40, 80,
                            int(cfg.get("daily_rsi_threshold",60)),
                            disabled=not daily_on, key=f"dt_{sid}")
                    with c4:
                        weekly_on = st.checkbox("Weekly RSI Filter",
                            value=bool(cfg.get("weekly_rsi_filter_enabled",True)), key=f"wf_{sid}")
                        weekly_th = st.slider("Weekly RSI >", 40, 80,
                            int(cfg.get("weekly_rsi_threshold",60)),
                            disabled=not weekly_on, key=f"wt_{sid}")

                st.markdown("---")
                st.markdown("### 🔴 SELL THRESHOLDS (Phase 2 — mirror of buy)")
                sc1, sc2 = st.columns(2)
                with sc1:
                    rsi_entry_sell = st.slider("RSI Entry SELL (Watch — >)",
                        50, 95, int(cfg.get("rsi_entry_sell_threshold", 80)),
                        key=f"res_{sid}",
                        help="When scan-TF RSI closes ABOVE this value the stock "
                             "is watched for a SELL trigger.")
                    rsi_reset_sell = st.slider("RSI Reset SELL (<)",
                        10, 50, int(cfg.get("rsi_reset_sell_threshold", 30)),
                        key=f"rrs_{sid}",
                        help="If RSI falls below this after watching, the sell "
                             "watch is cancelled.")
                    rsi_exit_sell  = st.slider("RSI Exit SELL (<)",
                        10, 50, int(cfg.get("rsi_exit_sell_threshold", 32)),
                        key=f"rxs_{sid}",
                        help="Exit-TF candle closing below this value exits the "
                             "short position.")
                with sc2:
                    rise_pct = st.slider("Rise % (SELL trigger)",
                        0.0, 10.0,
                        float(cfg.get("rise_percent", 2.0)),
                        step=0.25, key=f"rp2_{sid}",
                        help="Trigger-TF candle closing this % above reference "
                             "price fires the SELL.")
                    avg_rise_pct = st.slider("Avg Rise %",
                        0.0, 10.0,
                        float(cfg.get("avg_rise_percent", 3.0)),
                        step=0.25, key=f"arp_{sid}",
                        help="Each trigger-TF candle closing this % above the "
                             "last avg fires an Avg Short.")
                    st.caption("Sell-side higher-TF filters: Daily RSI < "
                               f"**{cfg.get('daily_rsi_threshold_sell', 40)}** "
                               "and Weekly RSI < "
                               f"**{cfg.get('weekly_rsi_threshold_sell', 40)}** "
                               "(uses the same Filter ON/OFF toggles as buy).")

                st.markdown("---")
                st.markdown("**🛑 STOPLOSS — Daily RSI (live, every tick during market hours)**")
                slc1, slc2 = st.columns(2)
                with slc1:
                    daily_rsi_stoploss_buy = st.slider(
                        "BUY stoploss — Daily RSI crosses below (<)", 20, 60,
                        int(cfg.get("daily_rsi_stoploss_buy", 48)),
                        key=f"slb_{sid}",
                        help="LIVE check. Fires an immediate exit when the live "
                             "Daily RSI (yesterday close + today's current price) "
                             "crosses below this value during market hours.")
                    stoploss_cooling_rsi_buy = st.slider(
                        "After BUY stoploss → General when scan RSI rises above (>)",
                        40, 85, int(cfg.get("stoploss_cooling_rsi_buy", 68)),
                        key=f"slcb_{sid}",
                        help="After a stoploss exit, the stock is locked in "
                             "COOLING_BUY_STOPLOSS until scan-TF RSI closes above "
                             "this value.")
                with slc2:
                    daily_rsi_stoploss_sell = st.slider(
                        "SELL stoploss — Daily RSI crosses above (>)", 40, 80,
                        int(cfg.get("daily_rsi_stoploss_sell", 55)),
                        key=f"sls_{sid}",
                        help="LIVE check. Fires an immediate exit when the live "
                             "Daily RSI crosses above this value (mirror of buy).")
                    stoploss_cooling_rsi_sell = st.slider(
                        "After SELL stoploss → General when scan RSI drops below (<)",
                        15, 60, int(cfg.get("stoploss_cooling_rsi_sell", 32)),
                        key=f"slcs_{sid}",
                        help="After a sell-side stoploss, locks the stock in "
                             "COOLING_SELL_STOPLOSS until scan-TF RSI closes below "
                             "this value.")

                st.markdown("---")
                st.markdown("**Webhook URLs**")
                wh_cfg   = scfg.get("webhooks", {})
                whc1, whc2 = st.columns(2)
                with whc1:
                    buy_url       = st.text_input("🟢 BUY + AVG Webhook URL",
                        value=wh_cfg.get("buy_webhook_url",""),
                        placeholder="https://your-app.com/webhook/buy",
                        help="Receives BUY entries and Avg additions.",
                        key=f"bu_{sid}")
                    exit_url      = st.text_input("🟢 BUY EXIT Webhook URL",
                        value=wh_cfg.get("exit_webhook_url",""),
                        placeholder="https://your-app.com/webhook/buy-exit",
                        help="Receives buy exits (normal RSI + live stoploss).",
                        key=f"eu_{sid}")
                with whc2:
                    sell_url      = st.text_input("🔴 SELL + AVG Webhook URL",
                        value=wh_cfg.get("sell_webhook_url",""),
                        placeholder="https://your-app.com/webhook/sell",
                        help="Receives SELL entries and Avg Short additions.",
                        key=f"su_{sid}")
                    sell_exit_url = st.text_input("🔴 SELL EXIT Webhook URL",
                        value=wh_cfg.get("sell_exit_webhook_url",""),
                        placeholder="https://your-app.com/webhook/sell-exit",
                        help="Receives sell exits (normal RSI + live stoploss).",
                        key=f"seu_{sid}")
                wh_on    = st.checkbox("Enable Webhooks",
                    value=wh_cfg.get("enabled",True), key=f"we_{sid}")

                scanner_name = st.text_input("Scanner Name",
                    value=scfg.get("name", scanner_id), key=f"sn_{sid}")

                st.markdown("---")

                new_settings = {
                    "rsi_period":                rsi_period,
                    "rsi_entry_threshold":       rsi_entry,
                    "rsi_reset_threshold":       rsi_reset,
                    "rsi_exit_threshold":        rsi_exit,
                    "drop_percent":              drop_pct,
                    "avg_drop_percent":          avg_pct,
                    "scan_timeframe":            scan_tf,
                    "trigger_timeframe":         trigger_tf,
                    "exit_timeframe":            exit_tf,
                    "daily_rsi_stoploss_buy":    daily_rsi_stoploss_buy,
                    "stoploss_cooling_rsi_buy":  stoploss_cooling_rsi_buy,
                    "rsi_entry_sell_threshold":  rsi_entry_sell,
                    "rsi_reset_sell_threshold":  rsi_reset_sell,
                    "rsi_exit_sell_threshold":   rsi_exit_sell,
                    "rise_percent":              rise_pct,
                    "avg_rise_percent":          avg_rise_pct,
                    "daily_rsi_stoploss_sell":   daily_rsi_stoploss_sell,
                    "stoploss_cooling_rsi_sell": stoploss_cooling_rsi_sell,
                    "daily_rsi_threshold_sell":  cfg.get("daily_rsi_threshold_sell", 40),
                    "weekly_rsi_threshold_sell": cfg.get("weekly_rsi_threshold_sell", 40),
                    "daily_rsi_filter_enabled":  daily_on,
                    "daily_rsi_threshold":      daily_th,
                    "weekly_rsi_filter_enabled":weekly_on,
                    "weekly_rsi_threshold":     weekly_th,
                }

                # Reset button (clears in-memory positions and signal state)
                st.markdown("**🔁 Reset state** — clears WATCHED / ACTIVE_BUY / "
                            "EXITED records back to GENERAL (signal log kept).")
                if st.button("🔁 Reset", type="secondary",
                             use_container_width=True, key=f"reset_{sid}"):
                    _rflag = os.path.join(ROOT, "data",
                                          f"reset_{sid}_all.flag")
                    os.makedirs(os.path.dirname(_rflag), exist_ok=True)
                    with open(_rflag, "w") as _f:
                        _f.write(datetime.now(IST).isoformat())
                    st.success("✅ Reset flag dropped — scanner will clear "
                               "positions within ~10 s.")

                st.markdown("---")

                cs, cr = st.columns(2)
                with cs:
                    if st.button("💾 Save", type="primary",
                                 use_container_width=True, key=f"save_{sid}"):
                        config["scanners"][scanner_id]["settings"]         = new_settings
                        config["scanners"][scanner_id]["name"]             = scanner_name
                        config["scanners"][scanner_id]["buy_stock_group"]  = buy_stock_group
                        config["scanners"][scanner_id]["sell_stock_group"] = sell_stock_group
                        config["scanners"][scanner_id]["webhooks"] = {
                            "buy_webhook_url":       buy_url,
                            "exit_webhook_url":      exit_url,
                            "sell_webhook_url":      sell_url,
                            "sell_exit_webhook_url": sell_exit_url,
                            "enabled":               wh_on
                        }
                        save_config(config)
                        st.success("✅ Saved!")
                with cr:
                    if st.button("🔁 Rescan", type="secondary",
                                 use_container_width=True, key=f"rescan_{sid}"):
                        config["scanners"][scanner_id]["settings"]         = new_settings
                        config["scanners"][scanner_id]["buy_stock_group"]  = buy_stock_group
                        config["scanners"][scanner_id]["sell_stock_group"] = sell_stock_group
                        config["scanners"][scanner_id]["webhooks"] = {
                            "buy_webhook_url":       buy_url,
                            "exit_webhook_url":      exit_url,
                            "sell_webhook_url":      sell_url,
                            "sell_exit_webhook_url": sell_exit_url,
                            "enabled":               wh_on
                        }
                        save_config(config)
                        trigger_rescan(scanner_id)
                        st.success("🔁 Rescan triggered!")
                        time.sleep(2)
                        st.rerun()

                # ── Saved settings (read from disk so they reflect what's persisted) ──
                st.markdown("---")
                st.markdown("### 📋 Saved Settings")
                _saved12 = load_config().get("scanners", {}).get(scanner_id, {})
                _s12     = _saved12.get("settings", {})
                bs_col, ss_col = st.columns(2)
                with bs_col:
                    st.markdown("**🟢 BUY side**")
                    st.code(
                        f"Scan TF                 : {_s12.get('scan_timeframe','5')}m\n"
                        f"Trigger TF              : {_s12.get('trigger_timeframe','1')}m\n"
                        f"Exit TF                 : {_s12.get('exit_timeframe','10')}m\n"
                        f"RSI period              : {_s12.get('rsi_period',14)}\n"
                        f"Entry RSI (<)           : {_s12.get('rsi_entry_threshold',20)}\n"
                        f"Reset RSI (>)           : {_s12.get('rsi_reset_threshold',70)}\n"
                        f"Exit RSI (>)            : {_s12.get('rsi_exit_threshold',68)}\n"
                        f"Drop % (BUY)            : {_s12.get('drop_percent',2.0)}\n"
                        f"Avg Drop %              : {_s12.get('avg_drop_percent',3.0)}\n"
                        f"Daily RSI filter        : {_s12.get('daily_rsi_threshold',60)}  "
                        f"({'ON' if _s12.get('daily_rsi_filter_enabled',True) else 'OFF'})\n"
                        f"Weekly RSI filter       : {_s12.get('weekly_rsi_threshold',60)}  "
                        f"({'ON' if _s12.get('weekly_rsi_filter_enabled',True) else 'OFF'})\n"
                        f"Stoploss D-RSI (<)      : {_s12.get('daily_rsi_stoploss_buy',48)}\n"
                        f"Stoploss-cooling (>)    : {_s12.get('stoploss_cooling_rsi_buy',68)}",
                        language="text"
                    )
                with ss_col:
                    st.markdown("**🔴 SELL side**")
                    st.code(
                        f"Scan TF                 : {_s12.get('scan_timeframe','5')}m\n"
                        f"Trigger TF              : {_s12.get('trigger_timeframe','1')}m\n"
                        f"Exit TF                 : {_s12.get('exit_timeframe','10')}m\n"
                        f"RSI period              : {_s12.get('rsi_period',14)}\n"
                        f"Entry RSI SELL (>)      : {_s12.get('rsi_entry_sell_threshold',80)}\n"
                        f"Reset RSI SELL (<)      : {_s12.get('rsi_reset_sell_threshold',30)}\n"
                        f"Exit RSI SELL (<)       : {_s12.get('rsi_exit_sell_threshold',32)}\n"
                        f"Rise % (SELL)           : {_s12.get('rise_percent',2.0)}\n"
                        f"Avg Rise %              : {_s12.get('avg_rise_percent',3.0)}\n"
                        f"Daily RSI filter SELL (<): {_s12.get('daily_rsi_threshold_sell',40)}  "
                        f"({'ON' if _s12.get('daily_rsi_filter_enabled',True) else 'OFF'})\n"
                        f"Weekly RSI filter SELL (<): {_s12.get('weekly_rsi_threshold_sell',40)}  "
                        f"({'ON' if _s12.get('weekly_rsi_filter_enabled',True) else 'OFF'})\n"
                        f"Stoploss D-RSI (>)      : {_s12.get('daily_rsi_stoploss_sell',55)}\n"
                        f"Stoploss-cooling (<)    : {_s12.get('stoploss_cooling_rsi_sell',32)}",
                        language="text"
                    )

                st.caption(
                    f"BUY universe: **{_saved12.get('buy_stock_group','nifty650')}**  |  "
                    f"SELL universe: **{_saved12.get('sell_stock_group','nifty650')}**"
                )

                # ── Plain-language rules ──────────────────────────────────────────
                st.markdown("---")
                st.markdown("### 📖 Strategy Rules (in plain language)")
                st.markdown(
f"""
**🟢 BUY side**
1. **WATCH:** when {_s12.get('scan_timeframe','5')}-min RSI closes **below {_s12.get('rsi_entry_threshold',20)}**, the stock is marked WATCHED and its reference price is locked. {'Daily RSI must also be > ' + str(_s12.get('daily_rsi_threshold',60)) + ' (filter ON). ' if _s12.get('daily_rsi_filter_enabled',True) else ''}{'Weekly RSI must also be > ' + str(_s12.get('weekly_rsi_threshold',60)) + ' (filter ON). ' if _s12.get('weekly_rsi_filter_enabled',True) else ''}
2. **BUY:** the first {_s12.get('trigger_timeframe','1')}-min candle that closes **{_s12.get('drop_percent',2.0)}% below the reference price** fires BUY 1.
3. **AVG (averaging down):** every subsequent {_s12.get('trigger_timeframe','1')}-min candle that closes **{_s12.get('avg_drop_percent',3.0)}% below the last avg price** adds another averaging entry.
4. **RESET:** if {_s12.get('scan_timeframe','5')}-min RSI rallies back above **{_s12.get('rsi_reset_threshold',70)}** OR a higher-TF filter fails, the WATCH is cancelled and the stock returns to GENERAL.
5. **EXIT (normal):** the first {_s12.get('exit_timeframe','10')}-min candle that closes above RSI **{_s12.get('rsi_exit_threshold',68)}** fires EXIT. Stock → EXITED, resets at next market open.
6. **🛑 STOPLOSS (live, every tick):** during market hours, if the live Daily RSI **crosses below {_s12.get('daily_rsi_stoploss_buy',48)}** on any tick, an immediate exit fires. Stock → **COOLING_BUY_STOPLOSS** and is locked until {_s12.get('scan_timeframe','5')}-min RSI closes **above {_s12.get('stoploss_cooling_rsi_buy',68)}** — stricter than the normal reset because the stoploss indicates a structurally weakening trend.

**🔴 SELL side** (mirror of buy)
1. **WATCH:** when {_s12.get('scan_timeframe','5')}-min RSI closes **above {_s12.get('rsi_entry_sell_threshold',80)}**, the stock is marked WATCHED_SELL and its reference price is locked. {'Daily RSI must also be < ' + str(_s12.get('daily_rsi_threshold_sell',40)) + ' (filter ON). ' if _s12.get('daily_rsi_filter_enabled',True) else ''}{'Weekly RSI must also be < ' + str(_s12.get('weekly_rsi_threshold_sell',40)) + ' (filter ON). ' if _s12.get('weekly_rsi_filter_enabled',True) else ''}
2. **SELL:** the first {_s12.get('trigger_timeframe','1')}-min candle that closes **{_s12.get('rise_percent',2.0)}% above the reference price** fires SELL 1 (open short).
3. **AVG SHORT:** every subsequent {_s12.get('trigger_timeframe','1')}-min candle that closes **{_s12.get('avg_rise_percent',3.0)}% above the last avg price** adds another Avg Short entry.
4. **RESET:** if {_s12.get('scan_timeframe','5')}-min RSI drops back below **{_s12.get('rsi_reset_sell_threshold',30)}** OR a higher-TF filter flips, the WATCH is cancelled.
5. **EXIT (normal):** the first {_s12.get('exit_timeframe','10')}-min candle that closes below RSI **{_s12.get('rsi_exit_sell_threshold',32)}** fires EXIT_SELL → EXITED_SELL → resets at next market open.
6. **🛑 STOPLOSS (live, every tick):** during market hours, if the live Daily RSI **crosses above {_s12.get('daily_rsi_stoploss_sell',55)}** on any tick, an immediate exit fires. Stock → **COOLING_SELL_STOPLOSS** and is locked until {_s12.get('scan_timeframe','5')}-min RSI closes **below {_s12.get('stoploss_cooling_rsi_sell',32)}**.
"""
                )


# ─── Wrap in fragment if Streamlit ≥ 1.37 (flash-free auto-refresh) ────────────
_HAS_FRAGMENT = hasattr(st, "fragment")

if _HAS_FRAGMENT:
    @st.fragment(run_every=15)
    def render_data():
        _render_data()
else:
    render_data = _render_data

render_data()

# ─── Auto-refresh fallback for older Streamlit (no fragment support) ───────────
if not _HAS_FRAGMENT and st.session_state.get("auto_refresh_cb", True):
    time.sleep(15)
    st.rerun()

