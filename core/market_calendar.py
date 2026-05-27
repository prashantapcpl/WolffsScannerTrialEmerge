"""
market_calendar.py
NSE (Indian equity) market calendar + timing utilities.

Single source of truth for:
  - is_trading_day(date)              — Mon-Fri minus NSE holidays
  - is_market_hours(datetime)         — 9:15 <= t < 15:30 IST on trading day
  - expected_intraday_rows(date, tf)  — how many candles a complete day should have
  - is_half_day(date)                 — muhurat / early-close session
  - next_trading_day(date)            — calendar walker
  - prev_trading_day(date)            — calendar walker

NSE holidays must be kept current. The list below covers 2025-2026; add new
years as they're announced. Source: NSE annual holiday calendar.
"""

from datetime import datetime, date, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

# Market session times (IST)
MARKET_OPEN_HOUR   = 9
MARKET_OPEN_MIN    = 15     # 09:15
MARKET_CLOSE_HOUR  = 15
MARKET_CLOSE_MIN   = 30     # 15:30
# Closing auction window 15:30-15:40 is OUT of our candle range -- we treat
# candles opening 15:30+ as post-session/settlement and exclude them.

# Trading-minutes per full session = 9:15 -> 15:30 = 6h15m = 375 minutes
SESSION_MINUTES = 375

# Expected number of intraday candles per FULL trading day, by timeframe
# For intervals that don't divide 375 evenly, partial last bar is included.
EXPECTED_ROWS = {
    "1":   375,
    "2":   188,    # 375/2 = 187.5 -> 188 with partial
    "3":   125,
    "5":   75,
    "10":  38,     # 375/10 = 37.5 -> 38 with partial
    "15":  25,
    "30":  13,     # 375/30 = 12.5 -> 13 with partial
    "60":  7,      # 375/60 = 6.25 -> 7 with partial
    "D":   1,
    "W":   1,      # weekly: one row per week
}

# Half-day expected rows (muhurat trading or special early-close days).
# Sessions are 90 minutes (e.g., 18:15-19:45 for Diwali muhurat). Different
# from regular trading day.
HALF_DAY_MINUTES = 90
HALF_DAY_ROWS    = {tf: max(1, HALF_DAY_MINUTES // int(tf) + 1)
                    for tf in ("1", "2", "3", "5", "10", "15", "30", "60")}
HALF_DAY_ROWS.update({"D": 1, "W": 1})

# --- NSE holiday calendar -----------------------------------------------------
# Add new dates as they're announced. Format: YYYY-MM-DD.
# Source: NSE annual holiday list (https://www.nseindia.com/resources/exchange-communication-holidays)
# Includes: Republic Day, Holi, Good Friday, Eid, Independence Day, Diwali, etc.

NSE_HOLIDAYS_2025 = {
    "2025-02-26",   # Mahashivratri
    "2025-03-14",   # Holi
    "2025-03-31",   # Eid-Ul-Fitr (Ramzan Id)
    "2025-04-10",   # Mahavir Jayanti
    "2025-04-14",   # Dr. B. R. Ambedkar Jayanti
    "2025-04-18",   # Good Friday
    "2025-05-01",   # Maharashtra Day
    "2025-08-15",   # Independence Day
    "2025-08-27",   # Ganesh Chaturthi
    "2025-10-02",   # Mahatma Gandhi Jayanti / Dussehra
    "2025-10-21",   # Diwali Laxmi Pujan (muhurat - half day)
    "2025-10-22",   # Diwali Balipratipada
    "2025-11-05",   # Prakash Gurpurb (Guru Nanak Jayanti)
    "2025-12-25",   # Christmas
}

NSE_HOLIDAYS_2026 = {
    # CONFIRMED holidays only -- "estimated" entries removed because guessing
    # wrong dates makes the scanner think the market is closed on real
    # trading days (silently swallows live ticks for that day).
    # Add specific dates ONLY when confirmed against the NSE annual list:
    #   https://www.nseindia.com/resources/exchange-communication-holidays
    "2026-01-26",   # Republic Day
    "2026-04-03",   # Good Friday
    "2026-04-14",   # Dr. B. R. Ambedkar Jayanti
    "2026-05-01",   # Maharashtra Day
    "2026-05-28",   # Buddha Purnima (user-confirmed 27-May session)
    "2026-08-15",   # Independence Day
    "2026-10-02",   # Mahatma Gandhi Jayanti
    "2026-12-25",   # Christmas
    # Lunar / Islamic / Hindu festival holidays (Holi, Eid, Diwali, Buddha
    # Purnima, Ganesh Chaturthi, Dussehra, Guru Nanak Jayanti) vary year
    # to year -- do NOT hardcode estimates. Add them once NSE publishes
    # the official 2026 list.
}

NSE_HOLIDAYS = NSE_HOLIDAYS_2025 | NSE_HOLIDAYS_2026

# Estimated (NOT confirmed by NSE) holidays. Dashboard flags these so the
# user can verify before they take effect. Add new estimated dates here;
# move to NSE_HOLIDAYS_YYYY once NSE publishes the official calendar.
NSE_HOLIDAYS_ESTIMATED = set()    # currently empty -- only confirmed dates used

# Reasons for known holidays (used in dashboard "upcoming events" display)
NSE_HOLIDAY_REASONS = {
    "2025-02-26": "Mahashivratri",
    "2025-03-14": "Holi",
    "2025-03-31": "Eid-Ul-Fitr",
    "2025-04-10": "Mahavir Jayanti",
    "2025-04-14": "Dr. B. R. Ambedkar Jayanti",
    "2025-04-18": "Good Friday",
    "2025-05-01": "Maharashtra Day",
    "2025-08-15": "Independence Day",
    "2025-08-27": "Ganesh Chaturthi",
    "2025-10-02": "Mahatma Gandhi Jayanti / Dussehra",
    "2025-10-21": "Diwali Laxmi Pujan",
    "2025-10-22": "Diwali Balipratipada",
    "2025-11-05": "Guru Nanak Jayanti",
    "2025-12-25": "Christmas",
    "2026-01-26": "Republic Day",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. B. R. Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Buddha Purnima",
    "2026-08-15": "Independence Day",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-12-25": "Christmas",
}

# Half-day (muhurat) sessions
NSE_HALF_DAYS = {
    "2025-10-21",   # Diwali muhurat 2025
}


def _to_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    return d


def is_holiday(d) -> bool:
    """Return True if `d` is a declared NSE holiday."""
    return _to_date(d).strftime("%Y-%m-%d") in NSE_HOLIDAYS


def is_half_day(d) -> bool:
    return _to_date(d).strftime("%Y-%m-%d") in NSE_HALF_DAYS


def is_trading_day(d) -> bool:
    """True iff Mon-Fri AND not an NSE holiday."""
    d = _to_date(d)
    if d.weekday() >= 5:   # Sat/Sun
        return False
    return not is_holiday(d)


def is_market_hours(dt: datetime) -> bool:
    """True iff dt is on a trading day during 09:15 <= t < 15:30 IST.
    Special case: half-days use a different 90-min window (see is_half_day).
    """
    if dt.tzinfo is None:
        dt = IST.localize(dt)
    elif dt.tzinfo != IST:
        dt = dt.astimezone(IST)
    if not is_trading_day(dt):
        return False
    h, m = dt.hour, dt.minute
    return (h == MARKET_OPEN_HOUR and m >= MARKET_OPEN_MIN) or \
           (MARKET_OPEN_HOUR < h < MARKET_CLOSE_HOUR) or \
           (h == MARKET_CLOSE_HOUR and m < MARKET_CLOSE_MIN)


def expected_intraday_rows(d, tf: str) -> int:
    """How many candles a complete trading day should produce for tf."""
    if not is_trading_day(d):
        return 0
    if is_half_day(d):
        return HALF_DAY_ROWS.get(tf, 0)
    return EXPECTED_ROWS.get(tf, 0)


def next_trading_day(d) -> date:
    d = _to_date(d) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d) -> date:
    d = _to_date(d) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_between(start, end) -> list:
    """Inclusive list of trading days from start to end."""
    s, e = _to_date(start), _to_date(end)
    out = []
    cur = s
    while cur <= e:
        if is_trading_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def next_market_open(from_dt=None) -> datetime:
    """Return the IST datetime of the next 09:15 market-open on a trading day."""
    if from_dt is None:
        from_dt = datetime.now(IST)
    if from_dt.tzinfo is None:
        from_dt = IST.localize(from_dt)
    d = from_dt.date()
    # Same-day pre-open?
    if is_trading_day(d) and (from_dt.hour < 9 or
                              (from_dt.hour == 9 and from_dt.minute < 15)):
        return IST.localize(datetime(d.year, d.month, d.day, 9, 15))
    # Otherwise walk forward
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d = d + timedelta(days=1)
    return IST.localize(datetime(d.year, d.month, d.day, 9, 15))


def describe_non_trading_day(d) -> dict:
    """Return {reason, is_estimated} for a non-trading date, or None if it's
    actually a trading day."""
    d = _to_date(d)
    if is_trading_day(d):
        return None
    wd = d.weekday()
    if wd == 5:
        return {"reason": "Weekend (Saturday)", "is_estimated": False}
    if wd == 6:
        return {"reason": "Weekend (Sunday)",   "is_estimated": False}
    key = d.strftime("%Y-%m-%d")
    reason = NSE_HOLIDAY_REASONS.get(key, "NSE Holiday")
    est    = key in NSE_HOLIDAYS_ESTIMATED
    return {"reason": reason, "is_estimated": est}


def upcoming_events(from_date=None, days_ahead: int = 7) -> list:
    """List of non-trading days in the next `days_ahead` calendar days.
    Each entry: {date, reason, is_estimated}."""
    if from_date is None:
        from_date = datetime.now(IST).date()
    from_date = _to_date(from_date)
    out = []
    for i in range(1, days_ahead + 1):
        d = from_date + timedelta(days=i)
        desc = describe_non_trading_day(d)
        if desc:
            out.append({"date": d, **desc})
    return out
