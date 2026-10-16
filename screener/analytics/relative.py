"""
Relative strength: an item's return minus a benchmark's return over the same dates, in one currency.

Which benchmark an item is compared with is decided elsewhere (screener/benchmarks.py); this module only
gets the two price series. No database or EODHD code here.
"""
import pandas as pd

from screener.analytics.returns import value_at, window


def relative_strength(prices: pd.Series, benchmark: pd.Series, asof, months: int,
                      fx: pd.Series | None = None, fx_benchmark: pd.Series | None = None) -> float | None:
    """
    Item return minus benchmark return over the item's window (returns.window): 0.15 vs 0.08 gives 0.07.

    The benchmark is read on the item's first and last window dates (last value on or before each, at most
    7 days older), so both returns cover the same period even when holidays differ. fx and fx_benchmark are
    EUR per unit of the item's and the benchmark's currency (None = EUR); the benchmark return is converted
    into the item's currency. Pass None for both when the currencies are the same. None if any value is
    missing.
    """
    w = window(prices, asof, months)
    if w is None:
        return None
    start, end = w.index[0], w.index[-1]
    b0, b1 = value_at(benchmark, start), value_at(benchmark, end)
    if b0 is None or b1 is None or b0 <= 0:
        return None
    # benchmark price in the item's currency = price * fx_benchmark / fx
    f0, f1 = _fx_change(fx_benchmark, start, end), _fx_change(fx, start, end)
    if f0 is None or f1 is None:
        return None
    benchmark_return = b1 / b0 * f0 / f1 - 1
    return float((w.iloc[-1] / w.iloc[0] - 1) - benchmark_return)


def _fx_change(rates: pd.Series | None, start, end) -> float | None:
    """rate(end) / rate(start); 1.0 for None (EUR)."""
    if rates is None:
        return 1.0
    r0, r1 = value_at(rates, start), value_at(rates, end)
    return None if r0 is None or r1 is None or r0 <= 0 else r1 / r0
