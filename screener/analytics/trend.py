"""
Moving-average trend metrics on a single price series (normally adj_close), see returns.py for the rules.
"""
import pandas as pd

from screener.analytics.returns import TOLERANCE_DAYS

MA_MAX_SPAN_DAYS = 366  # the n prices of a moving average must lie within this many calendar days (no big gaps)


def moving_average(prices: pd.Series, asof, n: int = 200, tolerance_days: int = TOLERANCE_DAYS) -> float | None:
    """
    Mean of the last n prices on or before as-of. None if there are fewer than n, the last one is more than
    tolerance_days old, or the n prices span more than MA_MAX_SPAN_DAYS (gaps in the history).
    """
    asof = pd.Timestamp(asof)
    s = prices.dropna()
    s = s[s.index <= asof].iloc[-n:]
    if len(s) < n or (asof - s.index[-1]).days > tolerance_days or (s.index[-1] - s.index[0]).days > MA_MAX_SPAN_DAYS:
        return None
    return float(s.mean())


def ma_distance(prices: pd.Series, asof, n: int = 200) -> float | None:
    """Last price vs. its n-day moving average: last / MA - 1."""
    ma = moving_average(prices, asof, n)
    if ma is None or ma <= 0:
        return None
    s = prices.dropna()
    return float(s[s.index <= pd.Timestamp(asof)].iloc[-1] / ma - 1)


def ma_slope(prices: pd.Series, asof, months: int, n: int = 200) -> float | None:
    """Relative change of the n-day moving average over the last `months`: MA(as-of) / MA(as-of - months) - 1."""
    now = moving_average(prices, asof, n)
    before = moving_average(prices, pd.Timestamp(asof) - pd.DateOffset(months=months), n)
    return None if now is None or before is None or before <= 0 else float(now / before - 1)
