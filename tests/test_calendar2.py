from core.market_calendar import (
    is_trading_day, describe_non_trading_day, next_market_open,
    upcoming_events, IST,
)
from datetime import datetime, date, timedelta

now = datetime.now(IST)
print(f'Today {now.date()}: trading={is_trading_day(now.date())}')
print(f'Tomorrow {now.date()+timedelta(days=1)}: '
      f'trading={is_trading_day(now.date()+timedelta(days=1))}')

fri = date(2026, 5, 29)
sat = date(2026, 5, 30)
sun = date(2026, 5, 31)
mon = date(2026, 6, 1)
print()
print(f'Saturday 30-May: {describe_non_trading_day(sat)}')
print(f'Sunday   31-May: {describe_non_trading_day(sun)}')
print(f'Monday   01-Jun: trading={is_trading_day(mon)}')
print()
print(f'next_market_open from Friday 16:00 = '
      f'{next_market_open(IST.localize(datetime(2026,5,29,16,0)))}')
print()
print('Upcoming non-trading days in next 14 days from today:')
for e in upcoming_events(now.date(), 14):
    name = e['date'].strftime('%a %d %b')
    print(f'  {name}: {e["reason"]} (est={e["is_estimated"]})')
