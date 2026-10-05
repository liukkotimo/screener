"""Dividend metrics."""
import pandas as pd

from screener.analytics.returns import window


def trailing_yield(dividends: pd.Series, close: pd.Series, asof, months: int = 12) -> float | None:
    """
    Dividends with ex-date in (asof - months, asof] divided by the close at as-of.

    Both series must be in the same unit. Use the plain (split-adjusted) close, NOT adj_close, which is
    lowered retroactively by dividends and would inflate the yield. None if the price history does not cover
    the whole window (a partial window would understate the yield); 0.0 if it does but nothing was paid.
    """
    w = window(close, asof, months)
    if w is None:
        return None
    asof = pd.Timestamp(asof)
    start = asof - pd.DateOffset(months=months)
    paid = dividends[(dividends.index > start) & (dividends.index <= asof)].sum()
    return float(paid / w.iloc[-1])
