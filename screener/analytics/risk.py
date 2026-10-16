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


def downside_volatility(prices: pd.Series, asof, months: int = 36, min_obs: int = 500) -> float | None:
    """
    Annualized downside deviation with a 0 % threshold: sqrt(mean(min(r, 0) ** 2)) * sqrt(252).
    The mean is over all daily returns; positive days count as 0.
    """
    r = daily_returns(prices, asof, months)
    if r is None or len(r) < min_obs:
        return None
    return float(math.sqrt((r.clip(upper=0) ** 2).mean()) * math.sqrt(TRADING_DAYS))


def ulcer_index(prices: pd.Series, asof, months: int = 36, min_obs: int = 500) -> float | None:
    """
    Ulcer Index: sqrt(mean(drawdown ** 2)), drawdown measured daily from the running high since the window
    start. Deep and long drawdowns both raise it; 0.15 = 15 %.
    """
    w = window(prices, asof, months)
    if w is None or len(w) - 1 < min_obs:
        return None
    dd = w / w.cummax() - 1
    return float(math.sqrt((dd ** 2).mean()))
