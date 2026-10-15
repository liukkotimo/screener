"""
Valuation history: EV/EBIT, P/E, EV/Sales and FCF yield sampled monthly, each from the data of its own date.

At every sample date the price, the share count and the statement figures are those of that date: the latest
fiscal year public then (fundamentals.usable_years: 90 days after year end, at most ~18 months old), its
year-end share count, and the FX rates of that day. Today's fundamentals are never combined with old prices.

This is an approximation of point-in-time data, not the real thing: EODHD has no filing dates (the 90-day lag
is assumed) and gives the statements as currently reported, so later restatements are mixed in. Share counts
are year-end figures, so buybacks or issues during the year show up only with the next fiscal year.

No database or EODHD code here.
"""
import pandas as pd

from screener.analytics import fundamentals as fa
from screener.analytics.returns import value_at

MIN_SAMPLES = 36  # of the 60 monthly samples in 5 years, at least this many must be valid


def sample_dates(asof, months: int) -> list[pd.Timestamp]:
    """as-of minus 1 .. months calendar months (the history, oldest first); as-of itself is the current value."""
    asof = pd.Timestamp(asof)
    return [asof - pd.DateOffset(months=k) for k in range(months, 0, -1)]


def ratios_at(date, close: pd.Series, annual: pd.DataFrame, eur_per_quote, eur_per_fin) -> dict:
    """
    Valuation ratios at one date. close: split-adjusted close in the major quote currency (the same split
    basis as annual['shares_outstanding']); eur_per_quote / eur_per_fin: functions date -> EUR per unit
    (or None). Ratios that are meaningless are None: EV/EBIT and EV/Sales need EBIT / revenue > 0 and EV > 0,
    P/E needs net income > 0. FCF yield may be negative (cash burn is information, not noise).
    """
    keys = ('ev_ebit', 'pe', 'ev_sales', 'fcf_yield')
    years = fa.usable_years(annual, date)
    price, fx_q, fx_f = value_at(close, date), eur_per_quote(date), eur_per_fin(date)
    if years is None or price is None or fx_q is None or fx_f is None:
        return dict.fromkeys(keys)
    y = years.iloc[-1]
    shares, nd = fa.num(y.get('shares_outstanding')), fa.net_debt(y)
    if shares is None or shares <= 0:
        return dict.fromkeys(keys)
    mcap = price * fx_q * shares

    def fin(col):
        v = fa.num(y.get(col))
        return None if v is None else v * fx_f

    ev = None if nd is None else mcap + nd * fx_f
    ebit, revenue, net_income, fcf = fin('ebit'), fin('revenue'), fin('net_income'), fin('free_cash_flow')
    return {
        'ev_ebit': ev / ebit if ev is not None and ev > 0 and ebit is not None and ebit > 0 else None,
        'pe': mcap / net_income if net_income is not None and net_income > 0 else None,
        'ev_sales': ev / revenue if ev is not None and ev > 0 and revenue is not None and revenue > 0 else None,
        'fcf_yield': fcf / mcap if fcf is not None else None,
    }


def valuation_history(close: pd.Series, annual: pd.DataFrame, asof, months: int = 60,
                      fx_quote: pd.Series | None = None, fx_fin: pd.Series | None = None) -> pd.DataFrame:
    """
    Ratios (ratios_at) at as-of and at the monthly sample dates before it: a DataFrame indexed by date,
    ascending, the last row being as-of. fx_quote / fx_fin: EUR per unit of the quote and statement
    currency (None = EUR).
    """
    def rate(series):
        return (lambda d: 1.0) if series is None else (lambda d: value_at(series, d))

    dates = [*sample_dates(asof, months), pd.Timestamp(asof)]
    rows = [ratios_at(d, close, annual, rate(fx_quote), rate(fx_fin)) for d in dates]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(dates))


def median(history: pd.Series, min_samples: int = MIN_SAMPLES) -> float | None:
    """Median of the history (all rows but the last, the current value); None if too few valid samples."""
    h = history.iloc[:-1].dropna()
    return None if len(h) < min_samples else float(h.median())


def percentile(history: pd.Series, min_samples: int = MIN_SAMPLES) -> float | None:
    """
    Empirical percentile of the current value (last row) in the history before it:
    count(history <= current) / count(history), 0 .. 1. None if the current value or enough history is missing.
    """
    h, current = history.iloc[:-1].dropna(), history.iloc[-1]
    if pd.isna(current) or len(h) < min_samples:
        return None
    return float((h <= current).sum() / len(h))
