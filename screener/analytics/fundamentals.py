"""
Fundamental metrics from annual financial statements, and trailing twelve months (TTM) from quarterly ones.

Functions take a DataFrame of annual figures indexed by fiscal year end (ascending; columns as in the
fundamental_annual table, all in one currency) and return a float, or None when it cannot be computed.
Pick the years known at an as-of date with usable_years() first. No database or EODHD code here.

The single-year functions (roic, ebit_margin, ...) also take a TTM row: ttm() builds one from the latest four
quarters (fundamental_quarterly columns), picked with usable_quarters().
"""
import math
import statistics

import pandas as pd

REPORT_LAG_DAYS = 90       # a fiscal year is assumed public this many days after it ends
MAX_STATEMENT_AGE_DAYS = 548  # ~18 months: an older latest fiscal year is too stale to use
TAX_RATE_MAX = 0.4
NO_INTEREST_COVERAGE = 999.0  # interest_coverage when interest expense is reported as 0 and EBIT is positive
YEAR_STEP_DAYS = (270, 460)  # consecutive fiscal year ends are 9 .. 15 months apart (allows a year-end shift)
ENDPOINT_TOLERANCE_DAYS = 92  # a growth rate's first year must end `years` before the latest, +- ~3 months
SPLIT_CHECK_MIN_RATIO = 1.5  # splits at least this large (or 1/1.5) are checked against the share counts

QUARTER_REPORT_LAG_DAYS = 60  # a quarter is assumed public this many days after it ends
MAX_QUARTER_AGE_DAYS = 240    # ~8 months: an older latest quarter is too stale to use
QUARTER_STEP_DAYS = (80, 100)  # consecutive quarter ends are about 3 months apart
# summed over the four quarters of a TTM row; the other columns (balance sheet) are taken from the last quarter
FLOW_COLUMNS = ('revenue', 'ebit', 'ebitda', 'pretax_income', 'tax_provision', 'interest_expense', 'net_income',
                'operating_cash_flow', 'capital_expenditure', 'free_cash_flow', 'dividends_paid')


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
    """Mean of roic() over the latest `years` consecutive fiscal years; None unless all of them are computable."""
    values = yearly_values(annual, years, roic)
    return None if values is None else sum(values) / years


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
    """Annual revenue growth rate over `years` fiscal years, see growth_rate()."""
    return growth_rate(annual, years, lambda y: num(y.get('revenue')))


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


# --- multi-year helpers -------------------------------------------------------------------------------------

def last_years(annual: pd.DataFrame, n: int) -> pd.DataFrame | None:
    """The latest n fiscal years if they are consecutive (no missing year); None otherwise."""
    if len(annual) < n:
        return None
    a = annual.iloc[-n:]
    gaps = pd.Series(a.index).diff().dt.days.iloc[1:]
    return a if gaps.between(*YEAR_STEP_DAYS).all() else None


def yearly_values(annual: pd.DataFrame, n: int, func) -> list[float] | None:
    """func(year) for each of the latest n consecutive fiscal years; None if a year is missing or gives None."""
    a = last_years(annual, n)
    if a is None:
        return None
    values = [func(row) for _, row in a.iterrows()]
    return None if any(v is None for v in values) else values


def mean(values: list[float] | None) -> float | None:
    return None if values is None else statistics.fmean(values)


def pstdev(values: list[float] | None) -> float | None:
    """Population standard deviation."""
    return None if values is None else statistics.pstdev(values)


def year_before(annual: pd.DataFrame, years: int) -> pd.Series | None:
    """The fiscal year that ended `years` before the latest one (+- ENDPOINT_TOLERANCE_DAYS); None if absent."""
    if len(annual) < 2:
        return None
    target = annual.index[-1] - pd.DateOffset(years=years)
    distance = abs(annual.index[:-1] - target)
    i = distance.argmin()
    return annual.iloc[i] if distance[i].days <= ENDPOINT_TOLERANCE_DAYS else None


def growth_rate(annual: pd.DataFrame, years: int, func) -> float | None:
    """
    Annual growth rate (CAGR) of func(year) from the fiscal year `years` before the latest to the latest.
    Years in between may be missing. None if either endpoint is missing or not positive (no CAGR from a loss).
    """
    first = year_before(annual, years)
    if first is None:
        return None
    a, b = func(first), func(annual.iloc[-1])
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return (b / a) ** (1 / years) - 1


def ebit_margin_trend(annual: pd.DataFrame, years: int) -> float | None:
    """(ebit_margin now - ebit_margin `years` fiscal years earlier) / years: change per year."""
    first = year_before(annual, years)
    if first is None:
        return None
    before, now = ebit_margin(first), ebit_margin(annual.iloc[-1])
    return None if before is None or now is None else (now - before) / years


def fcf_positive_years(annual: pd.DataFrame, n: int) -> int | None:
    """How many of the latest n consecutive fiscal years had free cash flow > 0; None if any FCF is missing."""
    values = yearly_values(annual, n, lambda y: num(y.get('free_cash_flow')))
    return None if values is None else sum(v > 0 for v in values)


def eps(year: pd.Series) -> float | None:
    """net_income / year-end shares_outstanding (any sign); None if shares are missing or <= 0."""
    return _ratio(year.get('net_income'), year.get('shares_outstanding'))


def shares_on_price_basis(annual: pd.DataFrame, splits: pd.Series, fetched) -> pd.Series:
    """
    Year-end shares_outstanding on the split basis of the stored prices (all known splits applied).

    EODHD adjusts the reported share counts for the splits known when the fundamentals were fetched
    (`fetched`); splits after that date are applied here. EODHD's history is not always consistent (Sampo: some
    years adjusted, some not, one year already on the post-split basis before the split date). If the share
    count of a year changes from the year before by roughly the ratio of any later split, up or down (closer
    to the ratio than to no change), the counts are mixed: that year and all earlier ones are returned as NaN
    (the later ones are on the basis of the fetch), so share-based metrics needing them become NULL.
    splits: ratio (new shares per old share) indexed by split date.
    """
    shares = pd.to_numeric(annual.get('shares_outstanding', pd.Series(index=annual.index, dtype=float)),
                           errors='coerce').astype(float)
    shares = shares.where(shares > 0)
    splits = pd.Series(splits.to_numpy(dtype=float), index=pd.DatetimeIndex(splits.index))
    if fetched is not None:
        later = splits[splits.index.normalize() > pd.Timestamp(fetched).tz_localize(None).normalize()]
        shares = shares * float(later.prod())
    known = shares.dropna()
    for (d0, s0), (d1, s1) in zip(known.items(), list(known.items())[1:]):
        change = abs(math.log(s1 / s0))
        later = splits[splits.index > d0]  # EODHD adjusts earlier years retroactively, statements may be late
        for ratio in (*later, float(later.prod())):
            if abs(math.log(ratio)) >= math.log(SPLIT_CHECK_MIN_RATIO) and abs(change - abs(math.log(ratio))) < change:
                shares[shares.index <= d0] = float('nan')
    return shares


# --- trailing twelve months from quarterly statements ----------------------------------------------------------

def usable_quarters(quarterly: pd.DataFrame, asof) -> pd.DataFrame | None:
    """
    Quarters that were public at as-of: quarter end at least QUARTER_REPORT_LAG_DAYS before as-of.
    None if there is none, or the latest is more than MAX_QUARTER_AGE_DAYS older than as-of.
    """
    asof = pd.Timestamp(asof)
    q = quarterly[quarterly.index <= asof - pd.Timedelta(days=QUARTER_REPORT_LAG_DAYS)].sort_index()
    if q.empty or (asof - q.index[-1]).days > MAX_QUARTER_AGE_DAYS:
        return None
    return q


def consecutive_quarters(quarters: pd.DataFrame) -> bool:
    """True if no quarter is missing between the first and the last (ends QUARTER_STEP_DAYS apart)."""
    gaps = pd.Series(quarters.index).diff().dt.days.iloc[1:]
    return bool(gaps.between(*QUARTER_STEP_DAYS).all())


def ttm(quarters: pd.DataFrame, offset: int = 0) -> pd.Series | None:
    """
    Trailing twelve months: one year-like row from the four consecutive quarters ending `offset` quarters
    before the latest (offset=4: the TTM a year earlier). FLOW_COLUMNS are summed (NaN if any quarter lacks the
    figure, never a partial sum); the others come from the last of the four quarters. The row is named after
    that quarter's end. None if there are not four consecutive quarters.
    """
    end = len(quarters) - offset
    q = quarters.iloc[max(end - 4, 0):max(end, 0)]
    if len(q) < 4 or not consecutive_quarters(q):
        return None
    row = q.iloc[-1].copy()
    for c in FLOW_COLUMNS:
        if c in q:
            row[c] = pd.to_numeric(q[c], errors='coerce').sum(min_count=4)
    return row


def ttm_growth(quarters: pd.DataFrame, func) -> float | None:
    """
    Growth of func(TTM row) over the TTM a year earlier: now / before - 1 (needs 8 consecutive quarters).
    None if either is missing or not positive (no growth rate from a loss).
    """
    if len(quarters) < 8 or not consecutive_quarters(quarters.iloc[-8:]):
        return None
    now, before = ttm(quarters), ttm(quarters, 4)
    a, b = func(before), func(now)
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return b / a - 1
