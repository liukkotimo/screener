"""
Return and drawdown metrics on a single price series.

All functions take a pandas Series of prices indexed by date (ascending; normally adj_close, which includes
dividends) and an as-of date, and return a float, or None when the history does not cover the window.
No database or EODHD code here, so the module can be reused elsewhere.
"""
import pandas as pd

# A window endpoint may fall on a weekend/holiday: the last price on or before it is used,
# as long as that price is at most this many calendar days older.
TOLERANCE_DAYS = 7


def window(prices: pd.Series, asof, months: int, tolerance_days: int = TOLERANCE_DAYS) -> pd.Series | None:
    """
    Prices from the window start anchor up to as-of.

    The anchor is the last price on or before (asof - months); the end is the last price on or before asof.
    None if either is missing or older than tolerance_days (history too short, or the item stopped trading).
    """
    asof = pd.Timestamp(asof)
    s = prices.dropna()
    s = s[s.index <= asof]
    if s.empty or (asof - s.index[-1]).days > tolerance_days:
        return None
    start = asof - pd.DateOffset(months=months)
    before = s[s.index <= start]
    if before.empty or (start - before.index[-1]).days > tolerance_days:
        return None
    w = s[s.index >= before.index[-1]]
    return w if (w > 0).all() else None


def value_at(prices: pd.Series, asof, tolerance_days: int = TOLERANCE_DAYS) -> float | None:
    """Last value on or before asof, if at most tolerance_days old."""
    asof = pd.Timestamp(asof)
    s = prices.dropna()
    s = s[s.index <= asof]
    if s.empty or (asof - s.index[-1]).days > tolerance_days:
        return None
    return float(s.iloc[-1])


def performance(prices: pd.Series, asof, months: int) -> float | None:
    """Plain total return over the window (not annualized): last / first - 1."""
    w = window(prices, asof, months)
    return None if w is None else float(w.iloc[-1] / w.iloc[0] - 1)


def drawdown_from_high(prices: pd.Series, asof, months: int) -> float | None:
    """Last price vs. the highest price in the window: last / max - 1 (0 at a new high, otherwise negative)."""
    w = window(prices, asof, months)
    return None if w is None else float(w.iloc[-1] / w.max() - 1)


def days_since_low(prices: pd.Series, asof, months: int) -> int | None:
    """Calendar days from the lowest price in the window to the last price date (0 = at the low now)."""
    w = window(prices, asof, months)
    return None if w is None else int((w.index[-1] - w.idxmin()).days)


def max_drawdown(prices: pd.Series, asof, months: int) -> float | None:
    """Worst peak-to-trough decline within the window (0 or negative)."""
    w = window(prices, asof, months)
    return None if w is None else float((w / w.cummax() - 1).min())


def cagr(prices: pd.Series, asof, months: int) -> float | None:
    """Annual growth rate over the window: (last / first) ** (12 / months) - 1."""
    w = window(prices, asof, months)
    return None if w is None else float((w.iloc[-1] / w.iloc[0]) ** (12 / months) - 1)


def recovery_from_low(prices: pd.Series, asof, months: int) -> float | None:
    """Last price vs. the lowest price in the window: last / min - 1 (0 at the low, otherwise positive)."""
    w = window(prices, asof, months)
    return None if w is None else float(w.iloc[-1] / w.min() - 1)


def drawdown_from_peak(prices: pd.Series, asof, min_months: int = 12,
                       tolerance_days: int = TOLERANCE_DAYS) -> float | None:
    """
    Last price vs. the highest price of the whole history up to as-of: last / max - 1 (<= 0).
    None if the last price is stale or the history covers less than min_months.
    """
    asof = pd.Timestamp(asof)
    s = prices.dropna()
    s = s[s.index <= asof]
    if s.empty or (asof - s.index[-1]).days > tolerance_days or (s <= 0).any():
        return None
    if s.index[0] > asof - pd.DateOffset(months=min_months):
        return None
    return float(s.iloc[-1] / s.max() - 1)
