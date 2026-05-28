import py_compile
files = ['main.py','core/market_calendar.py','core/data_integrity.py']
for f in files:
    py_compile.compile(f, doraise=True); print(f'{f}: OK')

from core.market_calendar import is_trading_day, expected_intraday_rows, is_holiday
from datetime import date

print()
print('Calendar self-tests:')
print(f'  2026-05-25 (Mon) trading?     {is_trading_day(date(2026,5,25))}')
print(f'  2026-05-26 (Tue) trading?     {is_trading_day(date(2026,5,26))}')
print(f'  2026-05-23 (Sat) trading?     {is_trading_day(date(2026,5,23))}')
print(f'  2026-05-24 (Sun) trading?     {is_trading_day(date(2026,5,24))}')
print(f'  2026-01-26 (Republic Day)?    trading={is_trading_day(date(2026,1,26))} holiday={is_holiday(date(2026,1,26))}')
print(f'  expected 5m rows full day:    {expected_intraday_rows(date(2026,5,25),"5")}')
print(f'  expected 5m rows on Sun:      {expected_intraday_rows(date(2026,5,24),"5")}')
print(f'  expected 15m rows full day:   {expected_intraday_rows(date(2026,5,25),"15")}')
print(f'  expected 60m rows full day:   {expected_intraday_rows(date(2026,5,25),"60")}')
