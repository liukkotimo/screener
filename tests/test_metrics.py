"""calc_metrics end to end on an in-memory database: units, FX, market cap scaling, NULL handling."""
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
    i = add_item(conn, 'SHEL.L', 'GBp', market_cap=2e9, market_cap_date='2026-03-30')  # GBP, at 1000p
    add_prices(conn, i, dates, closes)
    conn.execute("INSERT INTO dividend VALUES (?, '2025-09-01', 30.0)", (i,))           # pence
    conn.executemany('INSERT INTO fx_rate_daily VALUES (?, ?, ?)',
                     [('GBP', d.strftime('%Y-%m-%d'), 1.2) for d in dates])

    assert calc_metrics(conn)['stored'] == 1
    m = metrics(conn, 'SHEL.L')
    assert m['asof_date'] == '2026-03-31' and m['last_price_date'] == '2026-03-31'
    assert m['price_eur'] == pytest.approx(12.0 * 1.2)                 # 1200p = 12 GBP
    assert m['market_cap'] == pytest.approx(2.4e9)                     # scaled by 1200/1000
    assert m['market_cap_eur'] == pytest.approx(2.4e9 * 1.2)
    assert m['perf_1m'] == pytest.approx(0.2)
    assert m['dividend_yield'] == pytest.approx(30 / 1200)
    assert m['avg_value_traded_eur_3m'] == pytest.approx(10.0 * 1000 * 1.2, rel=0.01)
    assert m['perf_3y'] is None and m['cvar_95'] is None              # history too short: NULL, not 0
    assert m['drawdown_52w'] == 0.0


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


def test_fundamentals_in_financial_currency(conn):
    # Quoted in pence, reports in USD (like SHEL.L): valuation compares both in EUR.
    dates = pd.bdate_range('2025-01-01', '2026-03-31')
    i = add_item(conn, 'SHEL.L', 'GBp', market_cap=2e9, market_cap_date='2026-03-31')  # GBP
    conn.execute("UPDATE item SET financial_currency = 'USD', forward_eps = 2.0, "
                 "info_updated_at = '2026-03-31T18:00:00+00:00' WHERE item_id = ?", (i,))
    add_prices(conn, i, dates, [1000.0] * len(dates))                                   # 10 GBP
    conn.executemany('INSERT INTO fx_rate_daily VALUES (?, ?, ?)',
                     [(c, d.strftime('%Y-%m-%d'), r) for d in dates for c, r in (('GBP', 1.2), ('USD', 0.9))])
    add_annual(conn, i, 'USD', '2024-12-31', revenue=1.0e9, ebit=1.0e8)
    add_annual(conn, i, 'USD', '2025-12-31', revenue=1.2e9, ebit=2.4e8, ebitda=3e8, free_cash_flow=1.5e8,
               total_debt=8e8, cash_and_st_investments=2e8, interest_expense=0.0)

    calc_metrics(conn)
    m = metrics(conn, 'SHEL.L')
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

    # Backdated: FY2025 is not public yet on 2026-02-27, forward EPS snapshot is too far away.
    calc_metrics(conn, asof='2026-02-27')
    old = dict(conn.execute("SELECT * FROM item_metrics WHERE asof_date = '2026-02-27'").fetchone())
    assert old['fiscal_year_end'] == '2024-12-31' and old['ebit_margin'] == pytest.approx(0.1)
    assert old['pe_forward'] is None


def test_no_fundamentals_leaves_new_columns_null(conn):
    dates = pd.bdate_range('2025-01-01', '2026-03-31')
    i = add_item(conn, 'E.HE', 'EUR', market_cap=1e9, market_cap_date='2026-03-31')
    add_prices(conn, i, dates, [10.0] * len(dates))
    calc_metrics(conn)
    m = metrics(conn, 'E.HE')
    assert m['market_cap_eur'] == pytest.approx(1e9) and m['perf_1y'] == pytest.approx(0.0)
    assert all(m[c] is None for c in ('roic', 'ev_ebit', 'fcf_yield', 'pe_forward', 'fiscal_year_end'))
