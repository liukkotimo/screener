"""
Risk metrics from daily returns of a single price series (see returns.window for the window rules).
"""
import math

import pandas as pd

from screener.analytics.returns import window

TRADING_DAYS = 252


def daily_returns(prices: pd.Series, asof, months: int) -> pd.Series | None:
    w = window(prices, asof, months)
    return None if w is None else w.pct_change().dropna()


def volatility(prices: pd.Series, asof, months: int = 12, min_obs: int = 200) -> float | None:
    """Annualized volatility: sample standard deviation of daily returns * sqrt(252)."""
    r = daily_returns(prices, asof, months)
    if r is None or len(r) < min_obs:
        return None
    return float(r.std(ddof=1) * math.sqrt(TRADING_DAYS))


def cvar(prices: pd.Series, asof, months: int = 36, level: float = 0.95, min_obs: int = 500) -> float | None:
    """
    Historical Conditional Value at Risk (expected shortfall) of daily returns, as a positive loss:
    the average of the worst floor((1 - level) * n) daily returns, negated.
    0.04 means "on the worst 5 % of days the average loss was 4 %".
    """
    r = daily_returns(prices, asof, months)
    if r is None or len(r) < min_obs:
        return None
    n_tail = max(1, int((1 - level) * len(r)))
    return float(-r.sort_values().iloc[:n_tail].mean())
