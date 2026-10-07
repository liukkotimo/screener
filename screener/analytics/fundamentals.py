"""
Fundamental metrics from annual financial statements.

Functions take a DataFrame of annual figures indexed by fiscal year end (ascending; columns as in the
fundamental_annual table, all in one currency) and return a float, or None when it cannot be computed.
Pick the years known at an as-of date with usable_years() first. No database or Yahoo code here.
"""
import math

import pandas as pd

REPORT_LAG_DAYS = 90       # a fiscal year is assumed public this many days after it ends
MAX_STATEMENT_AGE_DAYS = 548  # ~18 months: an older latest fiscal year is too stale to use
TAX_RATE_MAX = 0.4
NO_INTEREST_COVERAGE = 999.0  # interest_coverage when interest expense is reported as 0 and EBIT is positive


def usable_years(annual: pd.DataFrame, asof) -> pd.DataFrame | None:
    """
    Fiscal years that were public at as-of: year end at least REPORT_LAG_DAYS before as-of.
    None if there is none, or the latest is more than MAX_STATEMENT_AGE_DAYS older than as-of.
    """
    asof = pd.Timestamp(asof)
    a = annual[annual.index <= asof - pd.Timedelta(days=REPORT_LAG_DAYS)].sort_index()
    if a.empty or (asof - a.index[-1]).days > MAX_STATEMENT_AGE_DAYS:
        return None
    return a


def num(v) -> float | None:
    """float, or None for None/NaN."""
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def _ratio(numerator, denominator, positive_denominator: bool = True) -> float | None:
    n, d = num(numerator), num(denominator)
    if n is None or d is None or d == 0 or (positive_denominator and d < 0):
        return None
    return n / d


def net_debt(year: pd.Series) -> float | None:
    """total_debt - cash_and_st_investments (negative = net cash)."""
    debt, cash = num(year.get('total_debt')), num(year.get('cash_and_st_investments'))
    return None if debt is None or cash is None else debt - cash


def roic(year: pd.Series) -> float | None:
    """
    EBIT * (1 - tax rate) / (stockholders_equity + net debt), for one fiscal year.

    Tax rate = tax_provision / pretax_income limited to [0, TAX_RATE_MAX]; 0 when pretax income <= 0.
    None if a figure is missing or invested capital <= 0.
    """
    ebit, equity, nd = num(year.get('ebit')), num(year.get('stockholders_equity')), net_debt(year)
    pretax, tax = num(year.get('pretax_income')), num(year.get('tax_provision'))
    if ebit is None or equity is None or nd is None or pretax is None or tax is None:
        return None
    tax_rate = min(max(tax / pretax, 0.0), TAX_RATE_MAX) if pretax > 0 else 0.0
    return _ratio(ebit * (1 - tax_rate), equity + nd)


def roic_avg(annual: pd.DataFrame, years: int) -> float | None:
    """Mean of roic() over the latest `years` fiscal years; None unless all of them are computable."""
    if len(annual) < years:
        return None
    values = [roic(row) for _, row in annual.iloc[-years:].iterrows()]
    return None if any(v is None for v in values) else sum(values) / years


def ebit_margin(year: pd.Series) -> float | None:
    """ebit / revenue; None if revenue <= 0."""
    return _ratio(year.get('ebit'), year.get('revenue'))


def ebit_margin_change(annual: pd.DataFrame) -> float | None:
    """ebit_margin of the latest fiscal year minus the year before (0.03 = +3 percentage points)."""
    if len(annual) < 2:
        return None
    now, before = ebit_margin(annual.iloc[-1]), ebit_margin(annual.iloc[-2])
    return None if now is None or before is None else now - before


def revenue_cagr(annual: pd.DataFrame, years: int) -> float | None:
    """
    Annual revenue growth rate from `years` fiscal years back to the latest.
    None if either revenue is missing or <= 0, or the two year ends are not `years` apart (+-3 months).
    """
    if len(annual) < years + 1:
        return None
    first, last = annual.iloc[-years - 1], annual.iloc[-1]
    months = (last.name.year - first.name.year) * 12 + last.name.month - first.name.month
    if abs(months - 12 * years) > 3:
        return None
    growth = _ratio(last.get('revenue'), first.get('revenue'))
    return None if growth is None or growth <= 0 else growth ** (1 / years) - 1


def fcf_margin(year: pd.Series) -> float | None:
    """free_cash_flow / revenue; None if revenue <= 0."""
    return _ratio(year.get('free_cash_flow'), year.get('revenue'))


def net_debt_ebitda(year: pd.Series) -> float | None:
    """net debt / EBITDA (negative = net cash); None if EBITDA <= 0."""
    return _ratio(net_debt(year), year.get('ebitda'))


def interest_coverage(year: pd.Series) -> float | None:
    """
    EBIT / interest expense. None if interest expense is not reported.
    Interest expense reported as 0 with positive EBIT gives NO_INTEREST_COVERAGE (999), not None,
    so debt-free companies are not dropped as missing data.
    """
    ebit, interest = num(year.get('ebit')), num(year.get('interest_expense'))
    if ebit is None or interest is None:
        return None
    if interest == 0:
        return NO_INTEREST_COVERAGE if ebit > 0 else None
    return ebit / interest


def fcf_payout_ratio(year: pd.Series) -> float | None:
    """dividends_paid / free_cash_flow; None if FCF <= 0 or dividends paid are not reported."""
    return _ratio(year.get('dividends_paid'), year.get('free_cash_flow'))
