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
