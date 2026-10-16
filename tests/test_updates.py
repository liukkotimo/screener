"""Data update logic against a fake EODHD client (no network)."""
import json
from datetime import date

import pandas as pd
import pytest

from screener import db
from screener.market_data.eodhd import EodhdError, QuotaExhausted
from screener.update.fundamentals import update_fundamentals
from screener.update.fx import needed_currencies, update_fx
from screener.update.items import update_items
from screener.update.prices import update_prices
from screener.update.universe import add_items, exchange_rows, import_exchange, read_ticker_file


def history(dates, closes, adj=None, dividends=None):
    idx = pd.DatetimeIndex(pd.to_datetime(dates))
    return pd.DataFrame({'open': closes, 'high': closes, 'low': closes, 'close': closes,
                         'adj_close': adj or closes, 'volume': 1000.0,
                         'dividend': dividends or [0.0] * len(closes), 'split': 0.0}, index=idx)


class FakeEodhd:
    """Stands in for EodhdClient: histories by ticker (EUR<CUR>.FOREX for FX), raw fundamentals by ticker,
    symbol lists and bulk rows by exchange code. Records history/fundamentals calls."""

    EXCHANGES = [{'Code': 'HE', 'Name': 'Helsinki Exchange', 'Currency': 'EUR'},
                 {'Code': 'ST', 'Name': 'Stockholm Exchange', 'Currency': 'SEK'}]

    def __init__(self, histories=None, fundamentals=None, symbols=None, bulk=None, quota_calls=None):
        self.histories = histories or {}
        self.fundamentals = fundamentals or {}
        self.symbols = symbols or {}
        self.bulk = bulk or {}
        self.quota_calls = quota_calls  # history/fundamentals calls allowed before QuotaExhausted; None = no limit
        self.calls = []

    def _spend(self, call):
        if self.quota_calls is not None and len(self.calls) >= self.quota_calls:
            raise QuotaExhausted('daily EODHD quota nearly used up')
        self.calls.append(call)

    def get_history(self, symbol, start, currency=None):
        self._spend(('history', symbol, start))
        if symbol not in self.histories:
            raise EodhdError(f'eod/{symbol}: not found', 404)
        h = self.histories[symbol]
        return h[h.index >= pd.Timestamp(start)]

    def get_fx_history(self, currencies, start):
        return {c: 1.0 / self.histories[f'EUR{c}.FOREX']['close'] for c in currencies
                if f'EUR{c}.FOREX' in self.histories}

    def get_exchanges(self):
        return self.EXCHANGES

    def get_exchange_symbols(self, exchange, delisted=False):
        return self.symbols.get(exchange, [])

    def get_bulk_last_day(self, exchange, extended=True):
        return self.bulk.get(exchange, [])

    def get_fundamentals(self, symbol):
        self._spend(('fundamentals', symbol))
        f = self.fundamentals.get(symbol)
        if f == 'raise':
            raise EodhdError('HTTP 429 after 5 attempts', 429)
        return f or {'General': {'Code': symbol}, 'Financials::Income_Statement::yearly': 'NA'}


def fundamentals(years: dict, currency='USD', quarters: dict | None = None, **general) -> dict:
    """
    Raw fundamentals response: years (and quarters) = {period end: {EODHD income statement field: value}}.
    """
    def section(periods):
        return {d: {'date': d, 'currency_symbol': currency, **{k: str(v) for k, v in f.items()}}
                for d, f in periods.items()}
    return {'General': {'Type': 'Common Stock', **general}, 'Financials::Income_Statement::yearly': section(years),
            'Financials::Income_Statement::quarterly': section(quarters or {})}


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
    client = FakeEodhd({'AAA': history(dates[:20], closes[:20], dividends=[0.0] * 19 + [0.5])})

    stats = update_prices(conn, client)
    assert stats['ok'] == 1 and stats['failed'] == 1
    assert conn.execute('SELECT COUNT(*) FROM price_daily').fetchone()[0] == 20
    assert conn.execute('SELECT amount FROM dividend').fetchone()[0] == 0.5
    err = conn.execute("SELECT last_error FROM fetch_log WHERE key = 'BAD'").fetchone()[0]
    assert err == 'no price data returned'

    # Ten more days appear; history before them is unchanged -> incremental append, no full refetch.
    client.histories['AAA'] = history(dates, closes, dividends=[0.0] * 19 + [0.5] + [0.0] * 10)
    stats = update_prices(conn, client, tickers=['AAA'])
    assert stats['refetched'] == 0
    assert conn.execute('SELECT COUNT(*) FROM price_daily').fetchone()[0] == 30
    assert client.calls[-1][2] > dates[0].date()  # incremental start, not the full range


def test_update_prices_refetches_when_history_readjusted(conn):
    add_items(conn, [{'ticker': 'AAA'}], 'test')
    dates = pd.bdate_range(end=date.today(), periods=30)
    closes = [100.0] * 30
    client = FakeEodhd({'AAA': history(dates[:25], closes[:25])})
    update_prices(conn, client)

    # A new dividend lowers all earlier adj_close values by 2 %.
    adj = [98.0] * 25 + [100.0] * 5
    client.histories['AAA'] = history(dates, closes, adj=adj)
    stats = update_prices(conn, client)
    assert stats['refetched'] == 1
    stored = [r[0] for r in conn.execute('SELECT adj_close FROM price_daily ORDER BY date')]
    assert stored == adj  # whole history replaced, not just the new days


def test_update_items_from_symbol_list_and_bulk(conn):
    add_items(conn, [{'ticker': 'AAA.HE'}, {'ticker': 'GONE.HE'}, {'ticker': 'BAD.XX'}], 'test')
    symbols = {'HE': [{'Code': 'AAA', 'Name': 'Aaa Oyj', 'Country': 'Finland', 'Exchange': 'HE', 'Currency': 'EUR',
                       'Type': 'Common Stock', 'Isin': 'FI0000000001'}]}
    bulk = {'HE': [{'code': 'AAA', 'date': '2026-10-13', 'MarketCapitalization': 1.5e9}]}
    stats = update_items(conn, FakeEodhd(symbols=symbols, bulk=bulk))
    assert stats == {'selected': 3, 'exchanges': 2, 'ok': 1, 'failed': 2}
    row = conn.execute("SELECT name, type, exchange, exchange_name, isin, market_cap, market_cap_date "
                       "FROM item WHERE ticker = 'AAA.HE'").fetchone()
    assert tuple(row) == ('Aaa Oyj', 'Common Stock', 'HE', 'Helsinki Exchange', 'FI0000000001', 1.5e9, '2026-10-13')
    errors = dict(conn.execute('SELECT key, last_error FROM fetch_log WHERE last_error IS NOT NULL').fetchall())
    assert errors['GONE.HE'].startswith('not in the EODHD symbol list') and 'unknown EODHD exchange' in errors['BAD.XX']

    # A later bulk row without market cap keeps the known one.
    update_items(conn, FakeEodhd(symbols=symbols, bulk={'HE': [{'code': 'AAA', 'date': '2026-10-14'}]}),
                 tickers=['AAA.HE'])
    assert conn.execute("SELECT market_cap, market_cap_date FROM item WHERE ticker = 'AAA.HE'").fetchone()[:] == \
        (1.5e9, '2026-10-13')


def test_update_fx_uses_major_currency_and_inverts_pair(conn):
    add_items(conn, [{'ticker': 'L', 'currency': 'GBX'}, {'ticker': 'E', 'currency': 'EUR'}], 'test')
    dates = pd.bdate_range(end=date.today(), periods=3)
    client = FakeEodhd({'EURGBP.FOREX': history(dates, [0.8, 0.8, 0.8])})
    assert update_fx(conn, client)['ok'] == 1
    rows = conn.execute('SELECT currency, eur_per_unit FROM fx_rate_daily').fetchall()
    assert {r[0] for r in rows} == {'GBP'} and rows[0][1] == pytest.approx(1.25)


def test_update_fundamentals_stores_keeps_old_years_and_logs(conn):
    add_items(conn, [{'ticker': 'AAA', 'type': 'Common Stock'}, {'ticker': 'NONE', 'type': 'Common Stock'},
                     {'ticker': 'ERR', 'type': 'Common Stock'}, {'ticker': 'FUND', 'type': 'FUND'}], 'test')
    client = FakeEodhd(fundamentals={
        'AAA': fundamentals({'2024-12-31': {'totalRevenue': 100},
                             '2025-12-31': {'totalRevenue': 110, 'incomeBeforeTax': 9, 'interestExpense': 2}},
                            quarters={'2025-09-30': {'totalRevenue': 27}, '2025-12-31': {'totalRevenue': 30}},
                            Sector='Technology'),
        'ERR': 'raise'})
    stats = update_fundamentals(conn, client)
    assert stats == {'selected': 3, 'ok': 1, 'failed': 2, 'years': 2, 'quarters': 2}  # the fund is not fetched
    assert [tuple(r) for r in conn.execute('SELECT quarter_end, revenue FROM fundamental_quarterly ORDER BY 1')] == \
        [('2025-09-30', 27.0), ('2025-12-31', 30.0)]
    rows = conn.execute('SELECT fiscal_year_end, currency, revenue, ebit FROM fundamental_annual ORDER BY 1').fetchall()
    assert [tuple(r) for r in rows] == [('2024-12-31', 'USD', 100.0, None), ('2025-12-31', 'USD', 110.0, 11.0)]
    assert tuple(conn.execute("SELECT sector, financial_currency FROM item WHERE ticker = 'AAA'").fetchone()) == \
        ('Technology', 'USD')
    errors = dict(conn.execute("SELECT key, last_error FROM fetch_log WHERE kind = 'fundamentals' "
                               'AND last_error IS NOT NULL').fetchall())
    assert errors['NONE'].startswith('no financial statements') and 'HTTP 429' in errors['ERR']

    # Within max_age_days only the ticker that raised is retried (an answer without statements counts as fetched).
    client.calls.clear()
    update_fundamentals(conn, client)
    assert client.calls == [('fundamentals', 'ERR')]

    # A later answer with fewer years replaces the returned year and keeps the older one.
    client.fundamentals['AAA'] = fundamentals({'2025-12-31': {'totalRevenue': 111}})
    update_fundamentals(conn, client, tickers=['AAA'])
    assert [r[0] for r in conn.execute('SELECT revenue FROM fundamental_annual ORDER BY fiscal_year_end')] == [100.0, 111.0]


def test_needed_currencies_include_financial_currency(conn):
    add_items(conn, [{'ticker': 'L', 'currency': 'GBX'}, {'ticker': 'E', 'currency': 'EUR'}], 'test')
    conn.execute("UPDATE item SET financial_currency = CASE ticker WHEN 'L' THEN 'USD' ELSE 'EUR' END")
    assert needed_currencies(conn) == ['GBP', 'USD']


def test_update_fundamentals_limit_takes_largest_unfetched_first(conn):
    add_items(conn, [{'ticker': t, 'type': 'Common Stock', 'market_cap': mc}
                     for t, mc in (('SMALL', 1e8), ('BIG', 1e11), ('MID', 1e9), ('NOCAP', None))], 'test')
    client = FakeEodhd()
    update_fundamentals(conn, client, limit=2)
    assert client.calls == [('fundamentals', 'BIG'), ('fundamentals', 'MID')]
    client.calls.clear()
    update_fundamentals(conn, client, limit=2)                    # the next run continues with the rest
    assert client.calls == [('fundamentals', 'SMALL'), ('fundamentals', 'NOCAP')]


def test_dedupe_groups_by_isin_or_name_keeps_home_listing_then_most_liquid(conn):
    from screener.update.dedupe import deactivate_duplicates, find_duplicates, reset_duplicates
    rows = [  # ticker, name, exchange, isin, turnover
        ('NOKIA.HE', 'Nokia Oyj', 'HE', 'FI0009000681', 4e7),
        ('NOKIA.ST', 'Nokia Oyj', 'ST', 'FI0009000681', 5e7),           # more liquid, but not the home exchange
        ('NDA-FI.HE', 'Nordea Bank Abp', 'HE', 'FI4000297767', 1e8),
        ('NDA-SE.ST', 'Nordea Bank Abp (publ)', 'ST', 'FI4000297767', 2e8),  # other name, same ISIN
        ('FOO.ST', 'Foreign Co', 'ST', None, 300.0),                    # no ISIN: grouped by name, most liquid wins
        ('FOO.HE', 'Foreign Co', 'HE', None, 5e4),
        ('SOLO.HE', 'Only Once', 'HE', 'FI0000000009', 1e6),
    ]
    add_items(conn, [{'ticker': t, 'name': n, 'type': 'Common Stock', 'exchange': e, 'isin': i}
                     for t, n, e, i, _ in rows], 'test')
    for t, *_, turnover in rows:
        conn.execute('INSERT INTO item_metrics (item_id, asof_date, avg_value_traded_eur_3m, calculated_at) '
                     "VALUES (?, '2026-10-07', ?, 'x')", (item_id(conn, t), turnover))

    groups = find_duplicates(conn)
    assert [(k['ticker'], [o['ticker'] for o in others]) for k, others in groups] == [
        ('FOO.HE', ['FOO.ST']), ('NOKIA.HE', ['NOKIA.ST']), ('NDA-FI.HE', ['NDA-SE.ST'])]

    assert deactivate_duplicates(conn, groups) == 3
    active = {r[0] for r in conn.execute('SELECT ticker FROM item WHERE active = 1')}
    assert active == {'NOKIA.HE', 'NDA-FI.HE', 'FOO.HE', 'SOLO.HE'}
    assert conn.execute("SELECT duplicate_of FROM item WHERE ticker = 'NOKIA.ST'").fetchone()[0] == \
        item_id(conn, 'NOKIA.HE')
    assert find_duplicates(conn) == []

    assert reset_duplicates(conn) == 3
    assert conn.execute('SELECT COUNT(*) FROM item WHERE active = 1').fetchone()[0] == 7


def test_update_prices_stops_cleanly_when_quota_is_used_up(conn):
    add_items(conn, [{'ticker': t} for t in ('A.HE', 'B.HE', 'C.HE')], 'test')
    dates = pd.bdate_range(end=date.today(), periods=5)
    client = FakeEodhd({t: history(dates, [10.0] * 5) for t in ('A.HE', 'B.HE', 'C.HE')}, quota_calls=2)
    stats = update_prices(conn, client)
    assert stats['ok'] == 2 and stats['failed'] == 0                 # C.HE is left for the next run
    assert conn.execute('SELECT COUNT(DISTINCT item_id) FROM price_daily').fetchone()[0] == 2


def test_update_fundamentals_etf_snapshot_and_quota(conn):
    add_items(conn, [{'ticker': 'AAA.HE', 'type': 'Common Stock', 'market_cap': 2e9},
                     {'ticker': 'ETF1.HE', 'type': 'ETF', 'market_cap': 1e9},
                     {'ticker': 'BBB.HE', 'type': 'Common Stock', 'market_cap': 5e8}], 'test')
    stock = fundamentals({'2025-12-31': {'totalRevenue': 100}}, currency='EUR', Industry='Machinery')
    stock['Earnings::Trend'] = {'2027-12-31': {'date': '2027-12-31', 'period': '+1y', 'earningsEstimateAvg': '1.80'}}
    etf = {'General': {'Type': 'ETF'}, 'ETF_Data::TotalAssets': '2.5e10', 'Financials::Income_Statement::yearly': 'NA'}
    client = FakeEodhd(fundamentals={'AAA.HE': stock, 'ETF1.HE': etf}, quota_calls=2)

    stats = update_fundamentals(conn, client)                        # largest first; BBB.HE hits the quota
    # an ETF without statements is fine, and so is a stock without quarterly statements
    assert stats == {'selected': 3, 'ok': 2, 'failed': 0, 'years': 1, 'quarters': 0}
    assert conn.execute("SELECT fundamentals_updated_at FROM item WHERE ticker = 'BBB.HE'").fetchone()[0] is None
    assert conn.execute("SELECT total_assets FROM item WHERE ticker = 'ETF1.HE'").fetchone()[0] == 2.5e10
    row = conn.execute("SELECT forward_eps, forward_eps_period, forward_eps_date, industry, financial_currency "
                       "FROM item WHERE ticker = 'AAA.HE'").fetchone()
    assert tuple(row) == (1.8, '2027-12-31', date.today().isoformat(), 'Machinery', 'EUR')
    assert json.loads(conn.execute('SELECT info_json FROM item_info_raw').fetchone()[0])['General']['Industry'] == \
        'Machinery'

    # The estimate disappears: the snapshot is cleared, known attributes stay.
    client = FakeEodhd(fundamentals={'AAA.HE': fundamentals({'2025-12-31': {'totalRevenue': 100}})})
    update_fundamentals(conn, client, tickers=['AAA.HE'])
    row = conn.execute("SELECT forward_eps, forward_eps_period, forward_eps_date, industry "
                       "FROM item WHERE ticker = 'AAA.HE'").fetchone()
    assert tuple(row) == (None, None, None, 'Machinery')


SYMBOLS_ST = [
    {'Code': 'BIG-B', 'Name': 'Big AB', 'Country': 'Sweden', 'Exchange': 'ST', 'Currency': 'SEK',
     'Type': 'Common Stock', 'Isin': 'SE0000000001'},
    {'Code': 'SMALL', 'Name': 'Small AB', 'Country': 'Sweden', 'Exchange': 'ST', 'Currency': 'SEK',
     'Type': 'Common Stock', 'Isin': None},
    {'Code': 'EURO', 'Name': 'Euro Line', 'Country': 'Sweden', 'Exchange': 'ST', 'Currency': 'EUR',
     'Type': 'Common Stock', 'Isin': None},
    {'Code': 'NOCAP', 'Name': 'No Cap AB', 'Country': 'Sweden', 'Exchange': 'ST', 'Currency': 'SEK',
     'Type': 'Common Stock', 'Isin': None},
    {'Code': 'XACT', 'Name': 'Some ETF', 'Country': 'Sweden', 'Exchange': 'ST', 'Currency': 'SEK',
     'Type': 'ETF', 'Isin': None},
]
BULK_ST = [{'code': 'BIG-B', 'date': '2026-10-13', 'MarketCapitalization': 6e9},     # 600 M EUR at 0.1
           {'code': 'SMALL', 'date': '2026-10-13', 'MarketCapitalization': 4e9},     # 400 M EUR
           {'code': 'EURO', 'date': '2026-10-13', 'MarketCapitalization': 9e9},
           {'code': 'NOCAP', 'date': '2026-10-13'},
           {'code': 'XACT', 'date': '2026-10-13'}]


def test_exchange_rows_filters_type_otc_currency_and_eur_market_cap():
    exchange = {'Code': 'ST', 'Name': 'Stockholm Exchange', 'Currency': 'SEK'}
    otc = {**SYMBOLS_ST[0], 'Code': 'OTCLINE', 'Exchange': 'PINK'}
    rows, skipped = exchange_rows(SYMBOLS_ST + [otc], BULK_ST + [{'code': 'OTCLINE', 'MarketCapitalization': 1e12}],
                                  exchange, eur_per_unit=0.1, min_market_cap_eur=5e8)
    assert [r['ticker'] for r in rows] == ['BIG-B.ST']
    assert rows[0] == {'ticker': 'BIG-B.ST', 'name': 'Big AB', 'type': 'Common Stock', 'exchange': 'ST',
                       'exchange_name': 'Stockholm Exchange', 'country': 'Sweden', 'currency': 'SEK',
                       'isin': 'SE0000000001', 'market_cap': 6e9, 'market_cap_date': '2026-10-13'}
    assert skipped == {'market cap below minimum': 1, 'other currency': 1, 'no market cap': 1, 'type ETF': 1,
                       'OTC': 1}
    # LSE: symbols quoted in GBX belong to the GBP exchange.
    lse = {'Code': 'LSE', 'Name': 'London Exchange', 'Currency': 'GBP'}
    rows, _ = exchange_rows([{**SYMBOLS_ST[0], 'Currency': 'GBX'}], BULK_ST, lse, 1.2, 5e8)
    assert [r['ticker'] for r in rows] == ['BIG-B.LSE']


def test_import_exchange_adds_and_keeps_existing(conn):
    dates = pd.bdate_range(end=date.today(), periods=3)
    client = FakeEodhd({'EURSEK.FOREX': history(dates, [10.0] * 3)}, symbols={'ST': SYMBOLS_ST},
                       bulk={'ST': BULK_ST})
    add_items(conn, [{'ticker': 'BIG-B.ST', 'name': 'kept as is'}], 'csv:mine')
    added, present, skipped = import_exchange(conn, client, 'ST', 5e8)
    assert (added, present) == (0, 1) and skipped['market cap below minimum'] == 1
    added, present, _ = import_exchange(conn, client, 'ST', 3e8)    # SMALL (400 M EUR) now qualifies
    assert (added, present) == (1, 1)
    assert tuple(conn.execute("SELECT source, market_cap FROM item WHERE ticker = 'SMALL.ST'").fetchone()) == \
        ('eodhd:ST', 4e9)
    assert conn.execute("SELECT name FROM item WHERE ticker = 'BIG-B.ST'").fetchone()[0] == 'kept as is'
    with pytest.raises(SystemExit, match='unknown EODHD exchange'):
        import_exchange(conn, client, 'XX', 5e8)


def test_update_prices_full_splits_and_benchmarks(conn, no_benchmarks):
    no_benchmarks.write_text('benchmarks:\n  IDX.INDX: {currency: SEK, name: Index}\nmarket:\n  ST: IDX.INDX\n')
    add_items(conn, [{'ticker': 'AAA.ST', 'currency': 'SEK'}], 'test')
    dates = pd.bdate_range(end=date.today(), periods=30)
    h = history(dates, [10.0] * 30)
    h.loc[dates[10], 'split'] = 2.0
    client = FakeEodhd({'AAA.ST': h, 'IDX.INDX': history(dates, [100.0] * 30)})

    stats = update_prices(conn, client, years=15)
    assert stats['ok'] == 2                                          # the benchmark was added and fetched
    bench = conn.execute("SELECT active, source, currency, type FROM item WHERE ticker = 'IDX.INDX'").fetchone()
    assert tuple(bench) == (0, 'benchmark', 'SEK', 'INDEX')
    assert conn.execute('SELECT date, ratio FROM split').fetchall()[0][:] == (dates[10].strftime('%Y-%m-%d'), 2.0)
    assert 'SEK' in needed_currencies(conn)

    # --full re-downloads the whole history from `years` back, even when nothing changed
    client.calls.clear()
    update_prices(conn, client, tickers=['AAA.ST'], years=15, full=True)
    assert client.calls[0][2] < (pd.Timestamp.today() - pd.DateOffset(years=14)).date()
    assert conn.execute('SELECT COUNT(*) FROM split').fetchone()[0] == 1


def test_parse_annual_reads_shares_outstanding():
    from screener.market_data.eodhd import parse_annual
    data = {'Financials::Balance_Sheet::yearly': {
        '2025-12-31': {'date': '2025-12-31', 'currency_symbol': 'EUR', 'commonStockSharesOutstanding': '4881700000'}}}
    assert parse_annual(data)['shares_outstanding'].iloc[0] == 4881700000.0
