"""calc_metrics end to end on an in-memory database: units, FX, market cap scaling, NULL handling."""
import numpy as np
import pandas as pd
import pytest

from screener import db
from screener.metrics import calc_metrics


@pytest.fixture
def conn():
    c = db.connect(':memory:')
    yield c
    c.close()


def add_item(conn, ticker, currency, market_cap=None, market_cap_date=None):
    conn.execute("INSERT INTO item (ticker, currency, market_cap, market_cap_date, added_at) VALUES (?, ?, ?, ?, 'x')",
                 (ticker, currency, market_cap, market_cap_date))
    return conn.execute('SELECT item_id FROM item WHERE ticker = ?', (ticker,)).fetchone()[0]


def add_prices(conn, item_id, dates, closes, volume=1000.0):
    conn.executemany('INSERT INTO price_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                     [(item_id, d.strftime('%Y-%m-%d'), c, c, c, c, c, volume) for d, c in zip(dates, closes)])


def metrics(conn, ticker):
    return dict(conn.execute('SELECT m.* FROM item_metrics m JOIN item USING (item_id) WHERE ticker = ?',
                             (ticker,)).fetchone())


def test_gbp_pence_item(conn):
    dates = pd.bdate_range('2025-01-01', '2026-03-31')
    closes = [1000.0] * (len(dates) - 1) + [1200.0]          # pence; +20 % on the last day
    i = add_item(conn, 'SHEL.LSE', 'GBX', market_cap=2e9, market_cap_date='2026-03-30')  # GBP, at 1000p
    add_prices(conn, i, dates, closes)
    conn.execute("INSERT INTO dividend VALUES (?, '2025-09-01', 30.0)", (i,))           # pence
    conn.executemany('INSERT INTO fx_rate_daily VALUES (?, ?, ?)',
                     [('GBP', d.strftime('%Y-%m-%d'), 1.2) for d in dates])

    assert calc_metrics(conn)['stored'] == 1
    m = metrics(conn, 'SHEL.LSE')
    assert m['asof_date'] == '2026-03-31' and m['last_price_date'] == '2026-03-31'
    assert m['price_eur'] == pytest.approx(12.0 * 1.2)                 # 1200p = 12 GBP
    assert m['market_cap'] == pytest.approx(2.4e9)                     # scaled by 1200/1000
    assert m['market_cap_eur'] == pytest.approx(2.4e9 * 1.2)
    assert m['perf_1m'] == pytest.approx(0.2)
    assert m['dividend_yield'] == pytest.approx(30 / 1200)
    assert m['avg_value_traded_eur_3m'] == pytest.approx(10.0 * 1000 * 1.2, rel=0.01)
    assert m['perf_3y'] is None and m['cvar_95'] is None              # history too short: NULL, not 0
    assert m['drawdown_52w'] == 0.0


def test_backdated_market_cap_scales_back_from_a_newer_snapshot(conn):
    dates = pd.bdate_range('2026-01-01', '2026-03-31')
    closes = [10.0] * (len(dates) - 5) + [8.0] * 5                   # falls 20 % in the last week
    i = add_item(conn, 'N.HE', 'EUR', market_cap=8e9, market_cap_date='2026-03-31')  # snapshot at 8.0
    add_prices(conn, i, dates, closes)
    calc_metrics(conn, asof='2026-03-20')                            # before the snapshot, price 10.0
    assert metrics(conn, 'N.HE')['market_cap'] == pytest.approx(1e10)


def test_missing_fx_and_no_prices(conn):
    dates = pd.bdate_range('2026-01-01', '2026-03-31')
    i = add_item(conn, 'X.ST', 'SEK', market_cap=5e9, market_cap_date='2026-03-31')
    add_prices(conn, i, dates, [10.0] * len(dates))
    add_item(conn, 'EMPTY', 'EUR')
    stats = calc_metrics(conn)
    assert stats == {'asof': '2026-03-31', 'items': 2, 'stored': 1, 'no_prices': 1}
    m = metrics(conn, 'X.ST')
    assert m['market_cap'] == pytest.approx(5e9)
    assert m['market_cap_eur'] is None and m['price_eur'] is None     # no SEK rate stored


def add_annual(conn, item_id, currency, year_end, **values):
    cols = ['item_id', 'fiscal_year_end', 'currency', *values]
    conn.execute(f'INSERT INTO fundamental_annual ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                 [item_id, year_end, currency, *values.values()])


def add_quarter(conn, item_id, currency, quarter_end, **values):
    cols = ['item_id', 'quarter_end', 'currency', *values]
    conn.execute(f'INSERT INTO fundamental_quarterly ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                 [item_id, quarter_end, currency, *values.values()])


def test_fundamentals_in_financial_currency(conn):
    # Quoted in pence, reports in USD (like SHEL.LSE): valuation compares both in EUR.
    dates = pd.bdate_range('2025-01-01', '2026-03-31')
    i = add_item(conn, 'SHEL.LSE', 'GBX', market_cap=2e9, market_cap_date='2026-03-31')  # GBP
    conn.execute("UPDATE item SET financial_currency = 'USD', forward_eps = 2.0, "
                 "forward_eps_date = '2026-03-31' WHERE item_id = ?", (i,))
    add_prices(conn, i, dates, [1000.0] * len(dates))                                   # 10 GBP
    conn.executemany('INSERT INTO fx_rate_daily VALUES (?, ?, ?)',
                     [(c, d.strftime('%Y-%m-%d'), r) for d in dates for c, r in (('GBP', 1.2), ('USD', 0.9))])
    add_annual(conn, i, 'USD', '2024-12-31', revenue=1.0e9, ebit=1.0e8)
    add_annual(conn, i, 'USD', '2025-12-31', revenue=1.2e9, ebit=2.4e8, ebitda=3e8, free_cash_flow=1.5e8,
               total_debt=8e8, cash_and_st_investments=2e8, interest_expense=0.0)

    calc_metrics(conn)
    m = metrics(conn, 'SHEL.LSE')
    mcap_eur = 2e9 * 1.2
    assert m['fiscal_year_end'] == '2025-12-31'
    assert m['ebit_margin'] == pytest.approx(0.2)
    assert m['ebit_margin_change_1y'] == pytest.approx(0.2 - 0.1)
    assert m['net_debt_ebitda'] == pytest.approx(2.0)
    assert m['interest_coverage'] == 999.0
    assert m['ev_ebit'] == pytest.approx((mcap_eur + 6e8 * 0.9) / (2.4e8 * 0.9))
    assert m['fcf_yield'] == pytest.approx(1.5e8 * 0.9 / mcap_eur)
    assert m['pe_forward'] == pytest.approx(10 * 1.2 / (2.0 * 0.9))
    assert m['roic'] is None and m['roic_avg_4y'] is None and m['revenue_cagr_3y'] is None  # not enough data
    assert m['fcf_payout_ratio'] is None                                                   # dividends not reported

    # Backdated: FY2025 is not public yet on 2026-02-27; the forward EPS snapshot (2026-03-31) is 32 days
    # away, within the 100-day limit. On 2025-12-19 (102 days away) it is not used.
    calc_metrics(conn, asof='2026-02-27')
    old = dict(conn.execute("SELECT * FROM item_metrics WHERE asof_date = '2026-02-27'").fetchone())
    assert old['fiscal_year_end'] == '2024-12-31' and old['ebit_margin'] == pytest.approx(0.1)
    assert old['pe_forward'] == pytest.approx(10 * 1.2 / (2.0 * 0.9))
    calc_metrics(conn, asof='2025-12-19')
    assert conn.execute("SELECT pe_forward FROM item_metrics WHERE asof_date = '2025-12-19'").fetchone()[0] is None


def test_no_fundamentals_leaves_new_columns_null(conn):
    dates = pd.bdate_range('2025-01-01', '2026-03-31')
    i = add_item(conn, 'E.HE', 'EUR', market_cap=1e9, market_cap_date='2026-03-31')
    add_prices(conn, i, dates, [10.0] * len(dates))
    calc_metrics(conn)
    m = metrics(conn, 'E.HE')
    assert m['market_cap_eur'] == pytest.approx(1e9) and m['perf_1y'] == pytest.approx(0.0)
    assert all(m[c] is None for c in ('roic', 'ev_ebit', 'fcf_yield', 'pe_forward', 'fiscal_year_end'))




def test_relative_strength_against_configured_benchmarks(conn, no_benchmarks):
    no_benchmarks.write_text('benchmarks:\n  IDX.INDX: {currency: SEK}\n  W.XETRA: {currency: EUR}\n'
                             'market:\n  ST: IDX.INDX\nworld: W.XETRA\n')
    dates = pd.bdate_range('2025-09-01', '2026-03-31')
    stock = [100.0] * len(dates)
    stock[-1] = 115.0
    i = add_item(conn, 'X.ST', 'SEK')
    conn.execute("UPDATE item SET exchange = 'ST' WHERE item_id = ?", (i,))
    add_prices(conn, i, dates, stock)
    b = add_item(conn, 'IDX.INDX', 'SEK')
    conn.execute('UPDATE item SET active = 0 WHERE item_id = ?', (b,))
    add_prices(conn, b, dates, [200.0] * (len(dates) - 1) + [216.0])

    stats = calc_metrics(conn)
    assert metrics(conn, 'X.ST')['relative_strength_market_6m'] == pytest.approx(0.15 - 0.08)
    assert metrics(conn, 'X.ST')['relative_strength_world_6m'] is None   # W.XETRA has no prices: NULL, reported
    assert set(stats['benchmarks_missing']) == {'W.XETRA'}
    assert conn.execute("SELECT COUNT(*) FROM item_metrics m JOIN item USING (item_id) "
                        "WHERE ticker = 'IDX.INDX'").fetchone()[0] == 0   # benchmarks are not items to screen

    # World benchmark in EUR: compared in SEK. SEK weakens 10 % against EUR over the window.
    w = add_item(conn, 'W.XETRA', 'EUR')
    conn.execute('UPDATE item SET active = 0 WHERE item_id = ?', (w,))
    add_prices(conn, w, dates, [50.0] * len(dates))                   # flat in EUR = +11.1 % in SEK
    conn.executemany('INSERT INTO fx_rate_daily VALUES (?, ?, ?)',
                     [('SEK', d.strftime('%Y-%m-%d'), 0.09 if d == dates[-1] else 0.10) for d in dates])
    calc_metrics(conn)
    assert metrics(conn, 'X.ST')['relative_strength_world_6m'] == pytest.approx(0.15 - (0.10 / 0.09 - 1))


def test_long_history_and_valuation_history(conn):
    # 6 years of prices rising 10 -> 22 (EUR), constant statements: EV/EBIT = price, so as-of is the 5-year high.
    dates = pd.bdate_range('2020-03-31', '2026-03-31')
    closes = list(np.linspace(10.0, 22.0, len(dates)))
    i = add_item(conn, 'V.HE', 'EUR', market_cap=22e6, market_cap_date='2026-03-31')
    conn.execute("UPDATE item SET financial_currency = 'EUR', fundamentals_updated_at = '2026-03-01T00:00:00+00:00' "
                 'WHERE item_id = ?', (i,))
    add_prices(conn, i, dates, closes)
    for y in range(2015, 2026):
        add_annual(conn, i, 'EUR', f'{y}-12-31', revenue=4e6, ebit=1e6, ebitda=1.2e6, pretax_income=1e6,
                   tax_provision=2e5, interest_expense=0.0, net_income=8e5, free_cash_flow=5e5, total_debt=0.0,
                   cash_and_st_investments=0.0, stockholders_equity=5e6, shares_outstanding=1e6)

    calc_metrics(conn)
    m = metrics(conn, 'V.HE')
    assert m['perf_5y'] == pytest.approx(closes[-1] / closes[dates.searchsorted(pd.Timestamp('2021-03-31'))] - 1)
    assert m['cagr_10y'] is None and m['max_drawdown_10y'] is None              # only 6 years of prices
    assert m['drawdown_available_history'] == 0.0
    assert m['roic_avg_10y'] == pytest.approx(0.8 / 5) and m['fcf_positive_years_10y'] == 10
    assert m['eps_cagr_5y'] == pytest.approx(0.0) and m['shares_outstanding_cagr_5y'] == pytest.approx(0.0)
    assert m['ev_ebit_percentile_5y'] == 1.0 and m['pe_percentile_5y'] == 1.0   # dearest in its own history
    assert m['fcf_yield_percentile_5y'] == 0.0                                  # lowest yield in its history
    assert 10.0 < m['ev_ebit_median_5y'] < 22.0
    assert m['ev_sales_percentile_5y'] == 1.0

    # A split after the fundamentals were fetched: stored shares are on the old basis and scaled here.
    conn.execute("INSERT INTO split VALUES (?, '2026-03-15', 2.0)", (i,))
    conn.execute('UPDATE price_daily SET close = close / 2, adj_close = adj_close / 2 WHERE item_id = ? '
                 "AND date < '2026-03-15'", (i,))
    calc_metrics(conn)
    assert metrics(conn, 'V.HE')['shares_outstanding_cagr_5y'] == pytest.approx(0.0)
    assert metrics(conn, 'V.HE')['ev_ebit_median_5y'] == pytest.approx(m['ev_ebit_median_5y'])


def test_valuation_history_without_financial_currency_rates(conn):
    # Reports in USD but no USD rates are stored: statement-based ratios stay NULL instead of crashing.
    dates = pd.bdate_range('2020-03-31', '2026-03-31')
    i = add_item(conn, 'U.HE', 'EUR', market_cap=22e6, market_cap_date='2026-03-31')
    conn.execute("UPDATE item SET financial_currency = 'USD', fundamentals_updated_at = '2026-03-01T00:00:00+00:00' "
                 'WHERE item_id = ?', (i,))
    add_prices(conn, i, dates, [10.0] * len(dates))
    for y in range(2015, 2026):
        add_annual(conn, i, 'USD', f'{y}-12-31', revenue=4e6, ebit=1e6, net_income=8e5, free_cash_flow=5e5,
                   total_debt=0.0, cash_and_st_investments=0.0, shares_outstanding=1e6)

    assert calc_metrics(conn)['stored'] == 1
    m = metrics(conn, 'U.HE')
    assert m['ev_ebit_median_5y'] is None and m['pe_percentile_5y'] is None


def test_ttm_metrics_from_quarterly_statements(conn):
    dates = pd.bdate_range('2025-01-01', '2026-06-30')
    i = add_item(conn, 'Q.HE', 'EUR', market_cap=2e9, market_cap_date='2026-06-30')
    conn.execute("UPDATE item SET financial_currency = 'USD' WHERE item_id = ?", (i,))
    add_prices(conn, i, dates, [10.0] * len(dates))
    conn.executemany('INSERT INTO fx_rate_daily VALUES (?, ?, ?)',
                     [('USD', d.strftime('%Y-%m-%d'), 0.9) for d in dates])
    ends = ['2024-06-30', '2024-09-30', '2024-12-31', '2025-03-31', '2025-06-30', '2025-09-30', '2025-12-31',
            '2026-03-31']
    for n, end in enumerate(ends):
        new = n >= 4
        add_quarter(conn, i, 'USD', end, revenue=2.5e8 if new else 2e8, ebit=5e7, ebitda=6e7, pretax_income=5e7,
                    tax_provision=1e7, interest_expense=5e6, net_income=4e7 if new else 2e7, free_cash_flow=3e7,
                    total_debt=4e8, cash_and_st_investments=1e8, stockholders_equity=1.1e9, shares_outstanding=1e8)
    add_annual(conn, i, 'USD', '2025-12-31', revenue=1e9, ebit=1e8)

    calc_metrics(conn)
    m = metrics(conn, 'Q.HE')
    assert m['ttm_quarter_end'] == '2026-03-31'
    assert m['ebit_margin_ttm'] == pytest.approx(2e8 / 1e9)
    assert m['ebit_margin'] == pytest.approx(0.1)                       # annual metric unchanged
    assert m['fcf_margin_ttm'] == pytest.approx(0.12)
    assert m['roic_ttm'] == pytest.approx(2e8 * 0.8 / 1.4e9)
    assert m['net_debt_ebitda_ttm'] == pytest.approx(3e8 / 2.4e8)
    assert m['interest_coverage_ttm'] == pytest.approx(10.0)
    assert m['ev_ebit_ttm'] == pytest.approx((2e9 + 3e8 * 0.9) / (2e8 * 0.9))
    assert m['pe_ttm'] == pytest.approx(2e9 / (1.6e8 * 0.9))
    assert m['fcf_yield_ttm'] == pytest.approx(1.2e8 * 0.9 / 2e9)
    assert m['revenue_growth_ttm'] == pytest.approx(0.25)
    assert m['eps_growth_ttm'] == pytest.approx(1.0)

    # On 2026-05-15 Q1/2026 is not public yet: the TTM ends 2025-12-31 and growth lacks an 8th quarter.
    calc_metrics(conn, asof='2026-05-15')
    old = dict(conn.execute("SELECT * FROM item_metrics WHERE asof_date = '2026-05-15'").fetchone())
    assert old['ttm_quarter_end'] == '2025-12-31' and old['revenue_growth_ttm'] is None


def test_ttm_metrics_null_without_quarterly_statements(conn):
    dates = pd.bdate_range('2025-01-01', '2026-06-30')
    i = add_item(conn, 'A.HE', 'EUR', market_cap=1e9, market_cap_date='2026-06-30')
    conn.execute("UPDATE item SET financial_currency = 'EUR' WHERE item_id = ?", (i,))
    add_prices(conn, i, dates, [10.0] * len(dates))
    add_annual(conn, i, 'EUR', '2025-12-31', revenue=1e9, ebit=1e8)
    calc_metrics(conn)
    m = metrics(conn, 'A.HE')
    assert m['ebit_margin'] == pytest.approx(0.1)
    assert all(m[c] is None for c in ('ttm_quarter_end', 'roic_ttm', 'ebit_margin_ttm', 'pe_ttm',
                                      'revenue_growth_ttm', 'eps_growth_ttm'))
