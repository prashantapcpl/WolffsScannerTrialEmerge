"""Audit Scanner 1 records — show state distribution and inspect buys."""
import os, json
from collections import Counter

ROOT = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(ROOT, "config.json")) as f:
    cfg = json.load(f)

S1 = cfg["scanners"]["scanner_1"]["settings"]
print("=" * 78)
print("CURRENT Scanner 1 saved settings:")
print(f"  rsi_entry_threshold : {S1['rsi_entry_threshold']}")
print(f"  rsi_reset_threshold : {S1['rsi_reset_threshold']}")
print(f"  drop_percent        : {S1['drop_percent']}")
print(f"  daily_rsi_threshold : {S1['daily_rsi_threshold']}")
print(f"  weekly_rsi_threshold: {S1['weekly_rsi_threshold']}")
print("=" * 78)

rsi_entry = float(S1["rsi_entry_threshold"])
drop_pct  = float(S1["drop_percent"])

with open(os.path.join(ROOT, "data", "scanner_1_state.json")) as f:
    state = json.load(f)

records = state.get("records", {})

# State distribution
states = Counter()
for sym, r in records.items():
    states[r.get("state", "UNKNOWN")] += 1

print("\nState distribution across all records:")
for s, n in states.most_common():
    print(f"  {s:<30} {n}")

# Show all records that have a non-null buy_price (signals they were bought
# at some point)
print(f"\n--- Records with buy_price set ---")
bought = [(s, r) for s, r in records.items()
          if r.get("buy_price") is not None]
print(f"Found {len(bought)} records with buy_price set\n")

print(f"{'symbol':<22} {'state':<22} {'buy_time':<19} "
      f"{'rsi_watch':>10} {'drop%':>7} {'rsi-ok':>8} {'drop-ok':>8}")
print("-" * 110)

inconsistent = 0
today_count  = 0
for sym, r in sorted(bought, key=lambda x: x[1].get("buy_time") or "",
                      reverse=True):
    rsi_w = r.get("rsi_at_watch")
    drp   = r.get("drop_pct_at_buy")
    bt    = (r.get("buy_time") or "")[:19]
    if bt.startswith("2026-05-25"):
        today_count += 1

    rsi_ok  = "  -"
    drop_ok = "  -"
    if rsi_w is not None:
        rsi_ok = "PASS" if float(rsi_w) < rsi_entry else "FAIL"
    if drp is not None:
        drop_ok = "PASS" if float(drp) >= drop_pct else "FAIL"

    if rsi_ok == "FAIL" or drop_ok == "FAIL":
        inconsistent += 1

    rsi_s = f"{float(rsi_w):.2f}" if rsi_w is not None else "    -"
    drp_s = f"{float(drp):.2f}"   if drp   is not None else "  -"

    print(f"{sym:<22} {r.get('state','?'):<22} {bt:<19} {rsi_s:>10} "
          f"{drp_s:>7} {rsi_ok:>8} {drop_ok:>8}")

print("-" * 110)
print(f"\nTotal records with buy_price set : {len(bought)}")
print(f"  ... bought TODAY (2026-05-25)  : {today_count}")
print(f"\nINCONSISTENT with current settings: {inconsistent}/{len(bought)}")
print("  (rsi_at_watch >= rsi_entry, OR drop_pct_at_buy < drop_percent)")
