"""Metric functions on hand-made price series with known answers."""
import math

import numpy as np
import pandas as pd
import pytest

from screener.analytics.dividends import trailing_yield
from screener.analytics.returns import (days_since_low, drawdown_from_high, max_drawdown, performance,
                                        value_at, window)
from screener.analytics.risk import cvar, volatility


def series(values, start='2024-01-01', freq='D'):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq=freq), dtype=float)


# --- window rules -------------------------------------------------------------------------------------------

def test_window_uses_last_price_on_or_before_start():
    s = series(range(1, 400), freq='B')        # business days only
    asof = pd.Timestamp('2024-07-01')          # Monday
    w = window(s, asof, 1)                      # start 2024-06-01 is a Saturday -> Friday 2024-05-31
    assert w.index[0] == pd.Timestamp('2024-05-31') and w.index[-1] == asof


def test_window_none_when_history_too_short_or_stale():
    s = series(range(1, 101))                   # 2024-01-01 .. 2024-04-09
    assert window(s, '2024-04-09', 12) is None  # needs a year
    assert window(s, '2024-06-01', 1) is None   # last price 53 days before as-of: stale
    assert window(s, '2024-04-15', 1) is not None  # 6 days stale is within tolerance


def test_window_none_for_non_positive_prices():
    assert window(series([1.0, 0.0, 2.0] * 20), '2024-02-29', 1) is None


def test_value_at():
    s = series([1.0, 2.0, 3.0])
    assert value_at(s, '2024-01-02') == 2.0
    assert value_at(s, '2024-01-20') is None and value_at(s, '2023-12-31') is None


# --- returns -------------------------------------------------------------------------------------------------

def test_performance_known_values():
    s = series([100.0] * 31 + [110.0])          # 2024-01-01 .. 2024-02-01
    assert performance(s, '2024-02-01', 1) == pytest.approx(0.10)
    assert performance(s, '2024-02-01', 3) is None   # insufficient history -> None, never 0


def test_drawdowns_and_days_since_low():
    # up to 100, down to 80, recover to 90
    values = list(np.linspace(85, 100, 200)) + list(np.linspace(100, 80, 100)) + list(np.linspace(80, 90, 100))
    s = series(values)
    asof = s.index[-1]
    assert drawdown_from_high(s, asof, 12) == pytest.approx(90 / 100 - 1)
    assert max_drawdown(s, asof, 12) == pytest.approx(80 / 100 - 1)
    assert days_since_low(s, asof, 12) == 100   # low first reached on day 299, last day 399
    assert drawdown_from_high(series([5.0] * 400), '2025-01-15', 12) == 0.0


# --- risk ----------------------------------------------------------------------------------------------------

def prices_from_returns(returns, start=100.0, freq='B'):
    return series(start * np.cumprod([1.0, *(1 + np.asarray(returns))]), freq=freq)


def test_volatility_matches_definition():
    rng = np.random.default_rng(1)
    r = rng.normal(0, 0.01, 300)
    s = prices_from_returns(r)
    expected = np.std(r[-len(s.loc[s.index[-1] - pd.DateOffset(months=12):]) + 1:], ddof=1) * math.sqrt(252)
    assert volatility(s, s.index[-1]) == pytest.approx(expected, rel=0.02)
    assert volatility(prices_from_returns(r[:150]), prices_from_returns(r[:150]).index[-1]) is None


def test_cvar_average_of_worst_tail():
    # 3.5 years of business days: 5 % are -5 % days, the rest +0.1 %
    n = 910
    r = np.full(n, 0.001)
    r[::20] = -0.05
    s = prices_from_returns(r)
    assert cvar(s, s.index[-1]) == pytest.approx(0.05)
    short = prices_from_returns(r[:400])
    assert cvar(short, short.index[-1]) is None  # fewer than 500 returns


# --- dividends -----------------------------------------------------------------------------------------------

def test_trailing_yield():
    close = series([50.0] * 400)                 # 2024-01-01 .. 2025-02-03
    asof = pd.Timestamp('2025-02-03')
    divs = pd.Series([1.0, 0.5, 0.5], index=pd.to_datetime(['2024-01-15', '2024-06-01', '2025-01-10']))
    assert trailing_yield(divs, close, asof) == pytest.approx(1.0 / 50)   # 2024-01-15 is outside
    assert trailing_yield(pd.Series(dtype=float, index=pd.DatetimeIndex([])), close, asof) == 0.0
    assert trailing_yield(divs, close.iloc[-100:], asof) is None           # history shorter than 12 m
