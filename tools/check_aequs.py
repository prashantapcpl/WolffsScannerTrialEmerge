"""Check AEQUS across universe + all scanner state files."""
import os, json, csv
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

print('=== Universe check ===')
hits = []
with open(os.path.join(ROOT, 'data', 'stocks.csv')) as f:
    for row in csv.reader(f):
        if any('AEQUS' in (c or '').upper() for c in row):
            hits.append(row)
for h in hits[:5]:
    print(' ', h)
if not hits:
    print('  AEQUS not found in stocks.csv')

print()
print('=== Scanner state files ===')
sym = 'NSE:AEQUS-EQ'
for sid in ['scanner_1','scanner_2','scanner_3','scanner_4']:
    path = os.path.join(ROOT, 'data', f'{sid}_state.json')
    if not os.path.exists(path):
        print(f'{sid}: no state file'); continue
    with open(path) as f:
        st = json.load(f)
    rec = st.get('records', {}).get(sym)
    if not rec:
        print(f'{sid}: no record for {sym}')
        continue
    print(f'{sid}:')
    show = ['state','side','watched_at','reference_price','rsi_at_watch',
            'buy_time','buy_price','drop_pct_at_buy','avg_count',
            'current_price','exit_time','exit_price','gap_fill',
            'ref_price','ref_time','trigger_tf','entries']
    for k in show:
        v = rec.get(k)
        if v not in (None, [], 0, '', 0.0):
            print(f'   {k:<18} = {v}')
    sig = [s for s in st.get('signal_log', [])
           if s.get('symbol') == sym
           or s.get('plain_name') == 'AEQUS'
           or s.get('stock') == 'AEQUS']
    if sig:
        print(f'   signal_log entries: {len(sig)} (last 5):')
        for s in sig[-5:]:
            t = s.get('time')
            typ = s.get('type','?')
            px = s.get('price') or s.get('buy_price')
            print(f'     {t}  {typ:<10}  price={px}')

print()
print('=== Price history CSV ===')
csv_path = os.path.join(ROOT, 'data', 'price_history', 'NSE_AEQUS-EQ_5m.csv')
if os.path.exists(csv_path):
    sz = os.path.getsize(csv_path)
    with open(csv_path) as f:
        rows = f.readlines()
    print(f'  5m CSV exists: {sz} bytes, {len(rows)} lines')
    print(f'  last row: {rows[-1].strip()}')
else:
    print('  5m CSV NOT found')
