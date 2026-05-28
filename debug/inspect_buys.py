import json, os
ROOT = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(ROOT, "data", "scanner_1_state.json")) as f:
    state = json.load(f)

targets = ["NSE:DIVISLAB-EQ", "NSE:WELSPUNLIV-EQ",
           "NSE:HINDALCO-EQ", "NSE:SOLARINDS-EQ"]
for sym in targets:
    r = state["records"].get(sym)
    if not r:
        print(f"{sym}: not in records")
        continue
    s = r["state"]
    print(f"\n{sym}: state={s}")
    keys = ["watched_at", "reference_price", "reference_time",
            "rsi_at_watch", "buy_time", "buy_price",
            "drop_pct_at_buy", "gap_fill", "side"]
    for k in keys:
        print(f"   {k:<18} = {r.get(k)}")
