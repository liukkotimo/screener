"""Data update logic against a fake Yahoo client (no network)."""
from datetime import date

import pandas as pd
import pytest

from screener import db
from screener.update.fundamentals import update_fundamentals
from screener.update.fx import needed_currencies, update_fx
from screener.update.items import update_items
from screener.update.prices import update_prices
from screener.update.universe import add_items, read_ticker_file


def history(dates, closes, adj=None, dividends=None):
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    return pd.DataFrame({'open': closes, 'high': closes, 'low': closes, 'close': closes,
                         'adj_close': adj or closes, 'volume': 1000.0,
                         'dividend': dividends or [0.0] * len(closes), 'split': 0.0}, index=idx)


class FakeYahoo:
    def __init__(self, histories=None, infos=None, financials=None):
        self.histories = histories or {}
        self.infos = infos or {}
        self.financials = financials or {}
        self.calls = []

    def get_history_bulk(self, tickers, start, batch_size=50):
        self.calls.append((tuple(tickers), start))
        start = pd.Timestamp(start)
        return {t: h[h.index >= start] for t, h in self.histories.items() if t in tickers}

    def get_fx_history(self, currencies, start):
        return {c: 1.0 / self.histories[f'EUR{c}=X']['close'] for c in currencies if f'EUR{c}=X' in self.histories}

    def get_info(self, ticker):
        if self.infos.get(ticker) == 'raise':
            raise RuntimeError('HTTP 401')
        return self.infos.get(ticker, {'trailingPegRatio': None})

    def get_annual_financials(self, ticker):
        self.calls.append(('financials', ticker))
        if isinstance(self.financials.get(ticker), str):
            raise RuntimeError('HTTP 429')
        return self.financials.get(ticker, pd.DataFrame())


@pytest.fixture
def conn():
    c = db.connect(':memory:')
    yield c
    c.close()


def item_id(conn, ticker):
    return conn.execute('SELECT item_id FROM item WHERE ticker = ?', (ticker,)).fetchone()[0]


def test_read_ticker_file(tmp_path):
    f = tmp_path / 'u.csv'
    f.write_text('ticker,name\n# comment\nnokia.he, Nokia\n\nAAPL   # apple\nAAPL\nSAP.DE;SAP\n')
    assert read_ticker_file(f) == ['NOKIA.HE', 'AAPL', 'SAP.DE']


def test_migrations_are_idempotent(conn):
    db.migrate(conn)
    assert conn.execute('SELECT COUNT(*) FROM schema_version').fetchone()[0] >= 1


def test_add_items_keeps_existing(conn):
    assert add_items(conn, [{'ticker': 'A', 'name': 'first'}], 'csv:x') == (1, 0)
    assert add_items(conn, [{'ticker': 'A', 'name': 'second'}, {'ticker': 'B'}], 'csv:y') == (1, 1)
    assert tuple(conn.execute("SELECT name, source FROM item WHERE ticker = 'A'").fetchone()) == ('first', 'csv:x')


def test_update_prices_new_incremental_and_failure(conn):
    add_items(conn, [{'ticker': 'AAA'}, {'ticker': 'BAD'}], 'test')
    dates = pd.bdate_range(end=date.today(), periods=30)
    closes = [float(10 + i) for i in range(30)]
    yahoo = FakeYahoo({'AAA': history(dates[:20], closes[:20], dividends=[0.0] * 19 + [0.5])})

    stats = update_prices(conn, yahoo)
    assert stats['ok'] == 1 and stats['failed'] == 1
    assert conn.execute('SELECT COUNT(*) FROM price_daily').fetchone()[0] == 20
    assert conn.execute('SELECT amount FROM dividend').fetchone()[0] == 0.5
    err = conn.execute("SELECT last_error FROM fetch_log WHERE key = 'BAD'").fetchone()[0]
    assert err == 'no price data returned'

    # Ten more days appear; history before them is unchanged -> incremental append, no full refetch.
    yahoo.histories['AAA'] = history(dates, closes, dividends=[0.0] * 19 + [0.5] + [0.0] * 10)
    stats = update_prices(conn, yahoo, tickers=['AAA'])
    assert stats['refetched'] == 0
    assert conn.execute('SELECT COUNT(*) FROM price_daily').fetchone()[0] == 30
    assert yahoo.calls[-1][1] > dates[0].date()  # incremental start, not the full range


def test_update_prices_refetches_when_yahoo_readjusts(conn):
    add_items(conn, [{'ticker': 'AAA'}], 'test')
    dates = pd.bdate_range(end=date.today(), periods=30)
    closes = [100.0] * 30
    yahoo = FakeYahoo({'AAA': history(dates[:25], closes[:25])})
    update_prices(conn, yahoo)

    # A new dividend lowers all earlier adj_close values by 2 %.
    adj = [98.0] * 25 + [100.0] * 5
    yahoo.histories['AAA'] = history(dates, closes, adj=adj)
    stats = update_prices(conn, yahoo)
    assert stats['refetched'] == 1
    stored = [r[0] for r in conn.execute('SELECT adj_close FROM price_daily ORDER BY date')]
    assert stored == adj  # whole history replaced, not just the new days


def test_update_items_stores_info_and_logs_failures(conn):
    add_items(conn, [{'ticker': 'AAA'}, {'ticker': 'NOPE'}, {'ticker': 'ERR'}], 'test')
    info = {'quoteType': 'EQUITY', 'longName': 'Aaa Oyj', 'exchange': 'HEL', 'currency': 'EUR',
            'sector': 'Technology', 'marketCap': 1.5e9}
    stats = update_items(conn, FakeYahoo(infos={'AAA': info, 'ERR': 'raise'}))
    assert stats == {'selected': 3, 'ok': 1, 'failed': 2}
    row = conn.execute("SELECT name, exchange, market_cap, market_cap_date FROM item WHERE ticker = 'AAA'").fetchone()
    assert tuple(row) == ('Aaa Oyj', 'HEL', 1.5e9, date.today().isoformat())
    errors = dict(conn.execute('SELECT key, last_error FROM fetch_log WHERE last_error IS NOT NULL').fetchall())
    assert errors['NOPE'].startswith('no info returned') and 'HTTP 401' in errors['ERR']

    # A later answer without sector keeps the known sector.
    update_items(conn, FakeYahoo(infos={'AAA': {**info, 'sector': None}}), tickers=['AAA'])
    assert conn.execute("SELECT sector FROM item WHERE ticker = 'AAA'").fetchone()[0] == 'Technology'


def test_update_fx_uses_major_currency_and_inverts_pair(conn):
    add_items(conn, [{'ticker': 'L', 'currency': 'GBp'}, {'ticker': 'E', 'currency': 'EUR'}], 'test')
    dates = pd.bdate_range(end=date.today(), periods=3)
    yahoo = FakeYahoo({'EURGBP=X': history(dates, [0.8, 0.8, 0.8])})
    assert update_fx(conn, yahoo)['ok'] == 1
    rows = conn.execute('SELECT currency, eur_per_unit FROM fx_rate_daily').fetchall()
    assert {r[0] for r in rows} == {'GBP'} and rows[0][1] == pytest.approx(1.25)


def statements(rows):
    df = pd.DataFrame.from_dict(rows, orient='index', dtype=float)
    df.index = pd.to_datetime(df.index)
    return df


def test_update_fundamentals_stores_keeps_old_years_and_logs(conn):
    add_items(conn, [{'ticker': 'AAA', 'type': 'EQUITY'}, {'ticker': 'NONE', 'type': 'EQUITY'},
                     {'ticker': 'ERR', 'type': 'EQUITY'}, {'ticker': 'FUND', 'type': 'ETF'}], 'test')
    conn.execute("UPDATE item SET financial_currency = 'USD' WHERE ticker = 'AAA'")
    yahoo = FakeYahoo(financials={'AAA': statements({'2024-12-31': {'revenue': 100.0},
                                                     '2025-12-31': {'revenue': 110.0, 'ebit': 11.0}}),
                                  'ERR': 'raise'})
    stats = update_fundamentals(conn, yahoo)
    assert stats == {'selected': 3, 'ok': 1, 'failed': 2, 'years': 2}                # the ETF is not fetched
    rows = conn.execute('SELECT fiscal_year_end, currency, revenue, ebit FROM fundamental_annual ORDER BY 1').fetchall()
    assert [tuple(r) for r in rows] == [('2024-12-31', 'USD', 100.0, None), ('2025-12-31', 'USD', 110.0, 11.0)]
    errors = dict(conn.execute("SELECT key, last_error FROM fetch_log WHERE kind = 'fundamentals' "
                               'AND last_error IS NOT NULL').fetchall())
    assert errors['NONE'].startswith('no financial statements') and 'HTTP 429' in errors['ERR']

    # Within max_age_days only the ticker that raised is retried (an empty answer counts as fetched).
    yahoo.calls.clear()
    update_fundamentals(conn, yahoo)
    assert yahoo.calls == [('financials', 'ERR')]

    # A later answer with fewer years replaces the returned year and keeps the older one.
    yahoo.financials['AAA'] = statements({'2025-12-31': {'revenue': 111.0}})
    update_fundamentals(conn, yahoo, tickers=['AAA'])
    assert [r[0] for r in conn.execute('SELECT revenue FROM fundamental_annual ORDER BY fiscal_year_end')] == [100.0, 111.0]


def test_needed_currencies_include_financial_currency(conn):
    add_items(conn, [{'ticker': 'L', 'currency': 'GBp'}, {'ticker': 'E', 'currency': 'EUR'}], 'test')
    conn.execute("UPDATE item SET financial_currency = CASE ticker WHEN 'L' THEN 'USD' ELSE 'EUR' END")
    assert needed_currencies(conn) == ['GBP', 'USD']
