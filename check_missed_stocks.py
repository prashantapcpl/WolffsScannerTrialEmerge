"""Check why MTARTECH and ENRIN were missed by S1."""
import os, json
ROOT = os.path.dirname(os.path.abspath(__file__))

# 1. Is each in the nifty650 stock group?
with open(os.path.join(ROOT, "data", "stocks.csv")) as f:
    universe = set()
    for line in f:
        parts = line.strip().split(",")
        if len(parts) >= 2:
            universe.add(parts[1].strip())

# 2. Is each in scanner_1_state.json?
with open(os.path.join(ROOT, "data", "scanner_1_state.json")) as f:
    state = json.load(f)
records = state.get("records", {})

# 3. Is the signal_log empty for these?
signal_log = state.get("signal_log", [])

for sym in ["NSE:MTARTECH-EQ", "NSE:ENRIN-EQ", "NSE:YATHARTH-EQ"]:
    print(f"\n{sym}:")
    print(f"  in stocks.csv universe : {sym in universe}")
    rec = records.get(sym)
    if rec:
        print(f"  state                  : {rec['state']}")
        print(f"  side                   : {rec.get('side')}")
        print(f"  watched_at             : {rec.get('watched_at')}")
        print(f"  rsi_at_watch           : {rec.get('rsi_at_watch')}")
        print(f"  buy_time               : {rec.get('buy_time')}")
        print(f"  buy_price              : {rec.get('buy_price')}")
        print(f"  exit_time              : {rec.get('exit_time')}")
    else:
        print(f"  NOT in scanner_1_state records")
    matches = [s for s in signal_log if s.get("symbol") == sym
                                    or s.get("plain_name") == sym.replace("NSE:","").replace("-EQ","")]
    print(f"  signal_log entries     : {len(matches)}")
    for s in matches[-5:]:
        print(f"     {s.get('time')} {s.get('type')} price={s.get('price') or s.get('buy_price')}")
