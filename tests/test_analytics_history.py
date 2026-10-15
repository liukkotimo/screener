"""Long-history price metrics, relative strength, multi-year fundamentals and valuation history."""
import math

import numpy as np
import pandas as pd
import pytest

from screener.analytics import fundamentals as fa
from screener.analytics import valuation as va
from screener.analytics.relative import relative_strength
from screener.analytics.returns import (cagr, days_since_low, drawdown_from_high, drawdown_from_peak, max_drawdown,
                                        recovery_from_low, window)
from screener.analytics.risk import downside_volatility, ulcer_index, volatility
from screener.analytics.trend import ma_distance, ma_slope, moving_average


def path(points: dict, start: str, end: str, freq='D') -> pd.Series:
    """Daily prices through the given {date: price} points, linear in between, flat before the first."""
    idx = pd.date_range(start, end, freq=freq)
    x = [pd.Timestamp(d).toordinal() for d in points]
    return pd.Series(np.interp([d.toordinal() for d in idx], x, list(points.values())), index=idx)


# --- price history ------------------------------------------------------------------------------------------

def test_new_high_keeps_the_earlier_max_drawdown():
    # 100 -> 50 -> 120: at a new 5-year high, but the -50 % fall stays in max_drawdown
    s = path({'2021-01-01': 100, '2022-01-01': 50, '2025-12-31': 120}, '2020-01-01', '2025-12-31')
    assert drawdown_from_high(s, '2025-12-31', 60) == 0.0
    assert max_drawdown(s, '2025-12-31', 60) == pytest.approx(-0.5)
    assert max_drawdown(s, '2025-12-31', 120) is None              # 10 years: history too short


def test_recovery_and_days_since_low():
    s = path({'2024-01-01': 100, '2024-06-01': 80, '2025-12-31': 120}, '2022-01-01', '2025-12-31')
    assert recovery_from_low(s, '2025-12-31', 36) == pytest.approx(0.5)
    assert days_since_low(s, '2025-12-31', 36) == (pd.Timestamp('2025-12-31') - pd.Timestamp('2024-06-01')).days


def test_cagr_needs_the_whole_window():
    s = pd.Series(100.0, index=pd.date_range('2015-01-01', '2025-01-01'))
    s.iloc[-1] = 200.0
    assert cagr(s, '2025-01-01', 120) == pytest.approx(2 ** 0.1 - 1)
    assert cagr(s, '2025-01-01', 60) == pytest.approx(2 ** 0.2 - 1)
    assert cagr(s[s.index >= '2023-01-01'], '2025-01-01', 120) is None   # 2 years of history


def test_drawdown_from_peak_uses_all_history_but_needs_a_year():
    s = path({'2021-01-01': 100, '2021-06-01': 200, '2025-12-31': 150}, '2020-01-01', '2025-12-31')
    assert drawdown_from_peak(s, '2025-12-31') == pytest.approx(-0.25)
    assert drawdown_from_peak(s[s.index >= '2025-08-01'], '2025-12-31') is None
    assert drawdown_from_peak(s, '2026-02-01') is None                       # last price stale


def test_volatility_and_downside_volatility_on_alternating_returns():
    idx = pd.date_range('2022-01-01', '2025-12-31')
    s = pd.Series(100.0 * np.cumprod([1.0] + [1.01 if i % 2 else 0.99 for i in range(len(idx) - 1)]), index=idx)
    r = window(s, '2025-12-31', 36).pct_change().dropna()
    n_down = int((r < 0).sum())
    assert downside_volatility(s, '2025-12-31', 36) == pytest.approx(math.sqrt(n_down / len(r) * 1e-4) * math.sqrt(252))
    assert volatility(s, '2025-12-31', 36, min_obs=500) == pytest.approx(0.01 * math.sqrt(252), rel=1e-3)
    assert downside_volatility(s[s.index >= '2025-01-01'], '2025-12-31', 36) is None


def test_ulcer_index_known_series():
    s = pd.Series(100.0, index=pd.date_range('2024-01-01', '2024-02-01'))       # 32 days
    s['2024-01-30'], s['2024-01-31'] = 80.0, 90.0                                # drawdowns -0.2, -0.1, then 0
    assert ulcer_index(s, '2024-02-01', 1, min_obs=1) == pytest.approx(math.sqrt((0.04 + 0.01) / 32))
    assert ulcer_index(s, '2024-02-01', 1) is None                               # < 500 returns
    flat = pd.Series(100.0, index=pd.date_range('2022-01-01', '2025-12-31'))
    assert ulcer_index(flat, '2025-12-31', 36) == 0.0


def test_moving_average_distance_and_slope():
    s = pd.Series(np.arange(1.0, 401.0), index=pd.bdate_range('2024-01-01', periods=400))
    asof = s.index[-1]
    assert moving_average(s, asof) == pytest.approx(np.arange(201, 401).mean())
    assert ma_distance(s, asof) == pytest.approx(400 / 300.5 - 1)
    before = s[s.index <= asof - pd.DateOffset(months=3)].iloc[-200:].mean()
    assert ma_slope(s, asof, 3) == pytest.approx(300.5 / before - 1)
    assert ma_distance(s.iloc[:150], s.index[149]) is None                      # fewer than 200 prices
    assert ma_slope(s.iloc[:230], s.index[229], 3) is None                      # no full MA 3 months ago
    gappy = pd.concat([s.iloc[:100], s.iloc[300:]])                              # 200 prices over > 1 year
    assert moving_average(gappy, asof) is None


# --- relative strength --------------------------------------------------------------------------------------

def two_points(start_value, end_value, start='2024-01-01', end='2024-07-01', freq='D'):
    s = pd.Series(float(start_value), index=pd.date_range(start, end, freq=freq))
    s.iloc[-1] = float(end_value)
    return s


def test_relative_strength_is_the_return_difference():
    assert relative_strength(two_points(100, 115), two_points(200, 216), '2024-07-01', 6) == pytest.approx(0.07)
    assert relative_strength(two_points(100, 90), two_points(200, 160), '2024-07-01', 6) == pytest.approx(0.10)


def test_relative_strength_reads_the_benchmark_on_the_item_dates():
    stock = two_points(100, 110, start='2023-12-01', end='2024-06-27')   # last trade Thursday; as-of Sunday
    bench = pd.Series(100.0, index=pd.bdate_range('2023-12-01', '2024-06-28'))
    bench['2024-06-28'] = 120.0                                       # after the item's last date: not counted
    assert relative_strength(stock, bench, '2024-06-30', 6) == pytest.approx(0.10)
    assert relative_strength(stock, bench[bench.index >= '2024-02-01'], '2024-06-30', 6) is None  # no start value


def test_relative_strength_in_the_item_currency():
    stock = two_points(100, 115)                                       # SEK
    bench = two_points(100, 108)                                       # EUR
    eur_per_sek = two_points(0.10, 0.11)                               # SEK strengthened by 10 %
    bench_in_sek = (108 / 0.11) / (100 / 0.10) - 1
    assert relative_strength(stock, bench, '2024-07-01', 6, fx=eur_per_sek) == pytest.approx(0.15 - bench_in_sek)
    eur_per_usd = two_points(0.9, 0.9)                                 # USD benchmark, flat USD/EUR
    assert relative_strength(stock, bench, '2024-07-01', 6, eur_per_sek, eur_per_usd) == pytest.approx(0.15 - bench_in_sek)
    assert relative_strength(stock, bench, '2024-07-01', 6, fx=eur_per_sek.iloc[-3:]) is None   # FX history missing


# --- multi-year fundamentals --------------------------------------------------------------------------------

YEAR = {'revenue': 1000.0, 'ebit': 100.0, 'ebitda': 150.0, 'pretax_income': 90.0, 'tax_provision': 18.0,
        'interest_expense': 10.0, 'net_income': 72.0, 'free_cash_flow': 60.0, 'total_debt': 300.0,
        'cash_and_st_investments': 100.0, 'stockholders_equity': 600.0, 'shares_outstanding': 100.0}


def years(first: int, last: int, **per_year) -> pd.DataFrame:
    """Fiscal years ending Dec 31; per_year: column -> {year: value} overrides."""
    rows = {}
    for y in range(first, last + 1):
        rows[f'{y}-12-31'] = {**YEAR, **{c: v[y] for c, v in per_year.items() if y in v}}
    df = pd.DataFrame.from_dict(rows, orient='index', dtype=float)
    df.index = pd.to_datetime(df.index)
    return df


def test_all_years_present():
    a = years(2016, 2025, ebit={2016 + i: 100.0 + 10 * i for i in range(10)})
    rois = [fa.roic(r) for _, r in a.iterrows()]
    assert fa.roic_avg(a, 10) == pytest.approx(sum(rois) / 10)
    assert min(fa.yearly_values(a, 5, fa.roic)) == pytest.approx(min(rois[-5:]))
    assert fa.pstdev(fa.yearly_values(a, 5, fa.roic)) == pytest.approx(float(np.std(rois[-5:])))   # population
    assert fa.mean(fa.yearly_values(a, 5, fa.ebit_margin)) == pytest.approx(np.mean([0.150, 0.160, 0.170, 0.180, 0.190]))
    assert fa.ebit_margin_trend(a, 3) == pytest.approx((0.19 - 0.16) / 3)
    assert fa.revenue_cagr(a.assign(revenue=[1000.0 * 1.1 ** i for i in range(10)]), 5) == pytest.approx(0.1)
    assert fa.roic_avg(a.iloc[1:], 10) is None                                       # only 9 years


def test_one_missing_year():
    a = years(2015, 2025).drop(pd.Timestamp('2020-12-31'))                          # 10 rows, 2020 missing
    assert fa.roic_avg(a, 10) is None and fa.fcf_positive_years(a, 10) is None
    assert fa.yearly_values(a, 5, fa.roic) is not None                              # 2021..2025 consecutive
    assert fa.roic_avg(a, 5) is not None and fa.roic_avg(a, 6) is None
    assert fa.revenue_cagr(a, 5) is None                                            # endpoint 2020 is missing
    b = years(2015, 2025, revenue={2025: 2000.0}).drop(pd.Timestamp('2022-12-31'))   # a gap in between is fine
    assert fa.revenue_cagr(b, 5) == pytest.approx(2 ** 0.2 - 1)


def test_missing_figure_is_not_zero():
    a = years(2016, 2025, free_cash_flow={2019: np.nan})
    assert fa.fcf_positive_years(a, 10) is None
    assert fa.fcf_positive_years(a, 5) == 5


def test_negative_earnings_have_no_eps_cagr():
    a = years(2020, 2025, net_income={2020: 50.0, 2025: 100.0})
    assert fa.growth_rate(a, 5, fa.eps) == pytest.approx(2 ** 0.2 - 1)
    assert fa.growth_rate(years(2020, 2025, net_income={2020: -50.0}), 5, fa.eps) is None
    assert fa.growth_rate(years(2020, 2025, net_income={2025: -50.0}), 5, fa.eps) is None
    assert fa.growth_rate(years(2020, 2025, net_income={2020: 0.0}), 5, fa.eps) is None


def test_negative_fcf():
    a = years(2016, 2025, free_cash_flow={2017: -10.0, 2021: -50.0, 2024: 0.0})
    assert fa.fcf_positive_years(a, 10) == 7                                        # 0 is not positive
    assert fa.mean(fa.yearly_values(a, 5, fa.fcf_margin)) == pytest.approx((-0.05 + 0.06 * 3 + 0.0) / 5)


def test_zero_interest_expense():
    a = years(2021, 2025, interest_expense={2023: 0.0})
    assert min(fa.yearly_values(a, 5, fa.interest_coverage)) == pytest.approx(10.0)  # 999 for 2023
    a = years(2021, 2025, interest_expense={2023: 0.0}, ebit={2023: -5.0})
    assert fa.yearly_values(a, 5, fa.interest_coverage) is None                    # not computable that year


def test_net_debt_ebitda_max_needs_positive_ebitda_every_year():
    a = years(2021, 2025, total_debt={2022: 600.0})
    assert max(fa.yearly_values(a, 5, fa.net_debt_ebitda)) == pytest.approx(500 / 150)
    assert fa.yearly_values(years(2021, 2025, ebitda={2022: -1.0}), 5, fa.net_debt_ebitda) is None


def test_shares_on_price_basis_handles_splits():
    no_splits = pd.Series(dtype=float)
    split = pd.Series([4.0], index=pd.DatetimeIndex(['2023-06-01']))
    adjusted = years(2021, 2025, shares_outstanding={2021: 400.0, 2022: 400.0, 2023: 404.0, 2024: 400.0, 2025: 396.0})
    shares = fa.shares_on_price_basis(adjusted, split, '2026-01-15T10:00:00+00:00')  # EODHD already adjusted
    assert list(shares) == [400.0, 400.0, 404.0, 400.0, 396.0]
    assert fa.growth_rate(adjusted.assign(shares_outstanding=shares), 4,
                          lambda y: fa.num(y.get('shares_outstanding'))) == pytest.approx((396 / 400) ** 0.25 - 1)
    # split after the fundamentals were fetched: stored counts are on the old basis -> scaled here
    shares = fa.shares_on_price_basis(adjusted, split, '2023-01-15')
    assert list(shares) == [1600.0, 1600.0, 1616.0, 1600.0, 1584.0]
    # counts NOT adjusted (jump by the split ratio): the years before the jump are unusable (NaN)
    raw = years(2021, 2025, shares_outstanding={2021: 100.0, 2022: 100.0, 2023: 401.0, 2024: 400.0, 2025: 399.0})
    assert list(fa.shares_on_price_basis(raw, split, '2026-01-15').isna()) == [True, True, False, False, False]
    # statements already on the post-split basis before the split date (EODHD's Sampo): also caught
    early = years(2021, 2025, shares_outstanding={2021: 100.0, 2022: 100.0, 2023: 100.0, 2024: 500.0, 2025: 500.0})
    late_split = pd.Series([5.0], index=pd.DatetimeIndex(['2025-02-11']))
    assert list(fa.shares_on_price_basis(early, late_split, '2026-01-15').isna()) == [True] * 3 + [False] * 2
    assert fa.shares_on_price_basis(raw, no_splits, None).notna().all()             # a real 4x issue stays


# --- valuation history --------------------------------------------------------------------------------------

def test_percentile_and_median_on_a_known_distribution():
    h = pd.Series([*range(1, 11), 5.0], dtype=float)                                 # history 1..10, current 5
    assert va.percentile(h, min_samples=10) == 0.5
    assert va.median(h, min_samples=10) == 5.5
    assert va.percentile(pd.Series([*range(1, 11), 0.5]), min_samples=10) == 0.0
    assert va.percentile(pd.Series([*range(1, 11), 11.0]), min_samples=10) == 1.0


def test_percentile_with_missing_samples():
    h = pd.Series([1.0, np.nan, 2.0, 3.0, np.nan, 2.0])
    assert va.percentile(h, min_samples=3) == pytest.approx(2 / 3)                   # NaN samples dropped
    assert va.percentile(h, min_samples=4) is None                                   # too few valid samples
    assert va.percentile(pd.Series([1.0, 2.0, 3.0, np.nan]), min_samples=1) is None  # no current value


def test_meaningless_ratios_are_excluded():
    close = pd.Series(10.0, index=pd.date_range('2025-01-01', '2026-06-30'))
    one = lambda d: 1.0  # noqa: E731
    a = years(2024, 2025, ebit={2025: -50.0}, net_income={2025: -10.0}, free_cash_flow={2025: -100.0})
    r = va.ratios_at(pd.Timestamp('2026-06-30'), close, a, one, one)
    assert r['ev_ebit'] is None and r['pe'] is None                                 # loss
    assert r['fcf_yield'] == pytest.approx(-100 / 1000)                            # negative yield is kept
    assert r['ev_sales'] == pytest.approx((1000 + 200) / 1000)
    net_cash = years(2024, 2025, cash_and_st_investments={2025: 5000.0})            # EV < 0
    r = va.ratios_at(pd.Timestamp('2026-06-30'), close, net_cash, one, one)
    assert r['ev_ebit'] is None and r['ev_sales'] is None and r['pe'] == pytest.approx(1000 / 72)


def test_valuation_history_uses_only_data_known_at_each_date():
    close = pd.Series(10.0, index=pd.date_range('2021-01-01', '2026-06-30'))
    a = years(2020, 2025, ebit={y: 100.0 * (y - 2019) for y in range(2020, 2026)})
    a.loc[pd.Timestamp('2026-03-31')] = {**YEAR, 'ebit': 10_000.0}                  # public from 2026-06-29 only
    h = va.valuation_history(close, a, '2026-06-30', 60)
    ev = 10 * 100 + 200
    for d, value in h['ev_ebit'].items():
        known = a[a.index <= d - pd.Timedelta(days=fa.REPORT_LAG_DAYS)]
        assert value == pytest.approx(ev / known['ebit'].iloc[-1])                 # never a later year
    assert h['ev_ebit'].loc['2026-03-30'] == pytest.approx(ev / 500)                # FY2025 not public yet
    assert h['ev_ebit'].loc['2026-06-30'] == pytest.approx(ev / 10_000)             # as-of: FY 2026-03 public
