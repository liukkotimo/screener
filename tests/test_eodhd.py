"""
EODHD client and parsers, without network.

Responses below are hand-written in EODHD's shapes (numbers as strings, 'NA' for missing sections, flattened
filter keys) with made-up values; raw EODHD data is licensed and not committed. The tests at the end also
run on real responses saved during the migration spike in tests/fixtures/eodhd/ (gitignored), if present.
The file names are <endpoint>_<symbol>.json (e.g. eod_NVDA.US.json); fundamentals_*_filter.json use
market_data.eodhd.FUNDAMENTALS_FILTER.
"""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from screener.market_data import eodhd
from screener.market_data.eodhd import (EodhdClient, EodhdError, QuotaExhausted, forward_eps, major_currency,
                                        parse_annual, parse_history, parse_info, split_ratio)

RAW = Path(__file__).parent / 'fixtures' / 'eodhd'


def bar(day, close, adj=None, volume=1000):
    return {'date': day, 'open': close, 'high': close, 'low': close, 'close': close,
            'adjusted_close': adj if adj is not None else close, 'volume': volume}


# ---------------------------------------------------------------- prices, dividends, splits

def test_split_ratio():
    assert split_ratio('10.000000/1.000000') == 10.0
    assert split_ratio('1.000000/5.000000') == 0.2
    assert split_ratio('0/1') is None and split_ratio(None) is None and split_ratio('x') is None


def test_parse_history_split_adjusts_prices_not_volume():
    # 10:1 split effective 2024-06-10: EODHD's close is raw (1000 -> 100), volume already split-adjusted.
    eod = [bar('2024-06-06', 1000.0, adj=99.0, volume=5000), bar('2024-06-07', 1010.0, adj=100.0, volume=6000),
           bar('2024-06-10', 102.0, volume=7000)]
    df = parse_history(eod, [], [{'date': '2024-06-10', 'split': '10.000000/1.000000'}])
    assert list(df.columns) == list(eodhd.HISTORY_COLUMNS)
    assert list(df['close']) == pytest.approx([100.0, 101.0, 102.0])
    assert list(df['open']) == pytest.approx([100.0, 101.0, 102.0])
    assert list(df['adj_close']) == [99.0, 100.0, 102.0]           # EODHD's, unchanged
    assert list(df['volume']) == [5000, 6000, 7000]                # unchanged
    assert list(df['split']) == [0.0, 0.0, 10.0]


def test_parse_history_multiplies_later_splits_and_handles_reverse_split():
    eod = [bar('2020-01-02', 800.0), bar('2021-01-04', 200.0), bar('2022-01-03', 100.0)]
    splits = [{'date': '2021-01-04', 'split': '4.000000/1.000000'}, {'date': '2022-01-03', 'split': '1.000000/2.000000'}]
    df = parse_history(eod, [], splits)
    assert list(df['close']) == pytest.approx([800 / 4 * 2, 200 * 2, 100.0])


def test_parse_history_dividends_in_quote_unit_and_only_on_price_days():
    eod = [bar('2026-05-13', 4000.0), bar('2026-05-14', 3950.0), bar('2026-05-15', 3960.0)]
    divs = [{'date': '2026-05-14', 'value': 0.4046, 'unadjustedValue': 0.4046, 'currency': 'GBP'},
            {'date': '2026-11-19', 'value': 0.41, 'unadjustedValue': 0.41, 'currency': 'GBP'}]  # announced, future
    df = parse_history(eod, divs, [], currency='GBX')
    assert df['dividend'].to_dict() == {pd.Timestamp('2026-05-13'): 0.0, pd.Timestamp('2026-05-14'): pytest.approx(40.46),
                                        pd.Timestamp('2026-05-15'): 0.0}


def test_parse_history_uses_split_adjusted_dividend_value():
    eod = [bar('2024-03-05', 850.0), bar('2024-06-10', 120.0)]
    divs = [{'date': '2024-03-05', 'value': 0.004, 'unadjustedValue': 0.04, 'currency': 'USD'}]
    splits = [{'date': '2024-06-10', 'split': '10.000000/1.000000'}]
    df = parse_history(eod, divs, splits, currency='USD')
    assert df.loc['2024-03-05', 'dividend'] == pytest.approx(0.004)  # matches the split-adjusted close 85.0
    assert df.loc['2024-03-05', 'close'] == pytest.approx(85.0)


def test_parse_history_drops_rows_without_close_and_empty_input():
    df = parse_history([bar('2026-01-02', 10.0), {**bar('2026-01-05', 0.0)}, {**bar('2026-01-06', 1.0), 'close': None}],
                       [], [])
    assert list(df.index) == [pd.Timestamp('2026-01-02')]
    assert parse_history([], [], []).empty


def test_major_currency():
    assert major_currency('GBX') == ('GBP', 0.01)
    assert major_currency('SEK') == ('SEK', 1.0)
    assert major_currency(None) == (None, 1.0)


# ---------------------------------------------------------------- fundamentals

def statement(day, currency='EUR', **fields):
    return {'date': day, 'filing_date': day, 'currency_symbol': currency,
            **{k: None if v is None else f'{v:.2f}' for k, v in fields.items()}}


STOCK = {
    'General': {'Code': 'AAA', 'Type': 'Common Stock', 'Name': 'Aaa Oyj', 'Exchange': 'HE', 'CurrencyCode': 'EUR',
                'CountryName': 'Finland', 'ISIN': 'FI0000000001', 'Sector': 'Industrials',
                'Industry': 'Specialty Industrial Machinery'},
    'Highlights': {'MarketCapitalization': 2000000000, 'EPSEstimateNextYear': 9.99},
    'Valuation': {'ForwardPE': 12.3},
    'SharesStats': {'SharesOutstanding': 100000000},
    'ETF_Data::TotalAssets': 'NA',
    'Earnings::Trend': {
        '2025-12-31': {'date': '2025-12-31', 'period': '+1y', 'earningsEstimateAvg': '1.0000'},  # stale entry
        '2026-12-31': {'date': '2026-12-31', 'period': '0y', 'earningsEstimateAvg': '1.5000'},
        '2027-12-31': {'date': '2027-12-31', 'period': '+1y', 'earningsEstimateAvg': '1.8000'},
    },
    'Financials::Income_Statement::yearly': {
        '2025-12-31': statement('2025-12-31', totalRevenue=1000, incomeBeforeTax=90, interestExpense=10,
                                taxProvision=20, netIncomeApplicableToCommonShares=None, netIncome=70,
                                depreciationAndAmortization=None, reconciledDepreciation=50, ebit=999),
        '2024-12-31': statement('2024-12-31', totalRevenue=900, incomeBeforeTax=80, interestExpense=None,
                                taxProvision=18, netIncomeApplicableToCommonShares=62,
                                depreciationAndAmortization=45),
        '1999-12-31': statement('1999-12-31'),                     # no figures at all: dropped
    },
    'Financials::Balance_Sheet::yearly': {
        '2025-12-31': statement('2025-12-31', shortLongTermDebtTotal=300, cashAndShortTermInvestments=100,
                                totalStockholderEquity=600),
    },
    'Financials::Cash_Flow::yearly': {
        '2025-12-31': statement('2025-12-31', totalCashFromOperatingActivities=150, capitalExpenditures=-40,
                                freeCashFlow=110, dividendsPaid=-60),
    },
}

ETF = {'General': {'Code': 'ETF1', 'Type': 'ETF', 'Name': 'Some ETF', 'CurrencyCode': 'EUR', 'ISIN': None},
       'Highlights': 'NA', 'Valuation': 'NA', 'SharesStats': 'NA', 'ETF_Data::TotalAssets': '24771393536.00',
       'Earnings::Trend': 'NA', 'Financials::Income_Statement::yearly': 'NA',
       'Financials::Balance_Sheet::yearly': 'NA', 'Financials::Cash_Flow::yearly': 'NA'}


def test_parse_annual_derives_ebit_and_ebitda():
    a = parse_annual(STOCK)
    assert [d.strftime('%Y-%m-%d') for d in a.index] == ['2024-12-31', '2025-12-31']
    y = a.loc['2025-12-31']
    assert y['currency'] == 'EUR'
    assert y['revenue'] == 1000 and y['pretax_income'] == 90 and y['tax_provision'] == 20
    assert y['ebit'] == 100                     # pretax + interest, not EODHD's own ebit field (999)
    assert y['ebitda'] == 150                   # + reconciledDepreciation when depreciationAndAmortization is missing
    assert y['net_income'] == 70                # netIncome when the common-shareholders line is missing
    assert y['total_debt'] == 300 and y['cash_and_st_investments'] == 100 and y['stockholders_equity'] == 600
    assert y['capital_expenditure'] == 40 and y['dividends_paid'] == 60      # outflows stored positive
    assert y['operating_cash_flow'] == 150 and y['free_cash_flow'] == 110


def test_parse_annual_without_interest_expense_has_no_ebit():
    y = parse_annual(STOCK).loc['2024-12-31']
    assert pd.isna(y['interest_expense']) and pd.isna(y['ebit']) and pd.isna(y['ebitda'])
    assert y['revenue'] == 900 and pd.isna(y['total_debt'])     # balance sheet year missing: NaN, not 0


def test_parse_annual_without_statements():
    a = parse_annual(ETF)
    assert a.empty and 'ebit' in a.columns


def test_forward_eps_takes_latest_next_year_estimate():
    assert forward_eps(STOCK) == (1.8, '2027-12-31')
    assert forward_eps(ETF) == (None, None)
    no_estimate = {'Earnings::Trend': {'2027-12-31': {'date': '2027-12-31', 'period': '+1y',
                                                      'earningsEstimateAvg': None}}}
    assert forward_eps(no_estimate) == (None, None)


def test_parse_info_stock_and_etf():
    assert parse_info(STOCK) == {
        'name': 'Aaa Oyj', 'type': 'Common Stock', 'country': 'Finland', 'currency': 'EUR', 'isin': 'FI0000000001',
        'sector': 'Industrials', 'industry': 'Specialty Industrial Machinery', 'market_cap': 2e9,
        'shares_outstanding': 1e8, 'total_assets': None, 'financial_currency': 'EUR',
        'forward_eps': 1.8, 'forward_eps_period': '2027-12-31'}
    info = parse_info(ETF)
    assert info['type'] == 'ETF' and info['total_assets'] == pytest.approx(24771393536.0)
    assert info['market_cap'] is None and info['financial_currency'] is None and info['isin'] is None


# ---------------------------------------------------------------- HTTP client (requests replaced by a fake)

class FakeResponse:
    def __init__(self, status_code, data=None, text='', headers=None):
        self.status_code, self._data, self.text, self.headers = status_code, data, text, headers or {}

    def json(self):
        if self._data is None:
            raise ValueError('not json')
        return self._data


class FakeSession:
    """Answers GETs from a list of responses, or by path for /user. Records the requested paths."""

    def __init__(self, responses, used=0, limit=100000):
        self.responses, self.used, self.limit, self.paths = list(responses), used, limit, []

    def get(self, url, params=None, timeout=None):
        path = url.split('/api/', 1)[1]
        if path == 'user':
            return FakeResponse(200, {'apiRequests': self.used, 'dailyRateLimit': self.limit, 'extraLimit': 0,
                                      'apiRequestsDate': '2026-10-14'})
        self.paths.append(path)
        return self.responses.pop(0)


def client_with(responses, **kw):
    c = EodhdClient(key='SECRET-KEY', min_interval=0, **kw)
    c._session = FakeSession(responses)
    return c


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(eodhd.time, 'sleep', sleeps.append)
    return sleeps


def test_client_retries_rate_limit_and_server_errors(no_sleep):
    c = client_with([FakeResponse(429), FakeResponse(503), FakeResponse(200, [bar('2026-01-02', 1.0)])])
    assert c._get('eod/AAA.HE') == [bar('2026-01-02', 1.0)]
    assert c._session.paths == ['eod/AAA.HE'] * 3
    assert no_sleep[0] >= 30 and len(no_sleep) == 2        # a 429 waits at least 30 s
    assert c.calls == 1


def test_client_gives_up_after_retries(no_sleep):
    c = client_with([FakeResponse(500)] * 3, retries=2)
    with pytest.raises(EodhdError, match='HTTP 500 after 3 attempts') as e:
        c._get('eod/AAA.HE')
    assert e.value.status == 500


def test_client_not_found_and_other_errors_fail_at_once_without_the_key():
    c = client_with([FakeResponse(404, text='Ticker Not Found.'), FakeResponse(401, text='Unauthenticated')])
    with pytest.raises(EodhdError) as e:
        c.get_history('NOSUCH.XX', date(2026, 1, 1))
    assert e.value.status == 404 and c.calls == 1           # EODHD charges for a 404 too
    with pytest.raises(EodhdError) as e:
        c._get('eod/AAA.HE', **{'from': '2026-01-01'})
    assert e.value.status == 401 and 'SECRET-KEY' not in str(e.value)
    assert len(c._session.paths) == 2                       # no retries


def test_client_counts_call_costs():
    c = client_with([FakeResponse(200, STOCK), FakeResponse(200, []), FakeResponse(200, [])])
    c.get_fundamentals('AAA.HE')
    c.get_bulk_last_day('HE')
    c.get_exchange_symbols('HE')
    assert c.calls == 10 + 100 + 1
    assert c._session.paths[0] == 'fundamentals/AAA.HE'


def test_quota_guard_keeps_the_reserve():
    c = client_with([FakeResponse(200, [])] * 3, reserve=1000)
    c._session.used, c._session.limit = 100000 - 1105, 100000
    c.get_bulk_last_day('HE')                               # 1105 - 100 = 1005 left: fine
    with pytest.raises(QuotaExhausted):
        c.get_fundamentals('AAA.HE')                        # would leave 995
    c.get_exchange_symbols('HE')                            # 1 call still fits
    assert c.calls == 101


def test_get_fx_history_inverts_eur_pairs():
    c = client_with([FakeResponse(200, [bar('2026-10-13', 11.0), bar('2026-10-14', 10.0)]),
                     FakeResponse(404, text='Ticker Not Found.')])
    fx = c.get_fx_history(['SEK', 'EUR', 'XXX'], date(2026, 10, 1))
    assert c._session.paths == ['eod/EURSEK.FOREX', 'eod/EURXXX.FOREX']
    assert list(fx) == ['SEK'] and list(fx['SEK']) == pytest.approx([1 / 11, 0.1])


def test_api_key_from_environment(monkeypatch):
    monkeypatch.setenv('EODHD_API_KEY', ' abc ')
    assert eodhd.api_key() == 'abc'
    monkeypatch.delenv('EODHD_API_KEY')
    with pytest.raises(SystemExit):
        eodhd.api_key()


# ---------------------------------------------------------------- real responses (local only, gitignored)

def raw(name):
    path = RAW / f'{name}.json'
    if not path.exists():
        pytest.skip(f'raw EODHD fixture {path.name} not present')
    return json.loads(path.read_text())


def test_raw_nvda_split_gives_continuous_close():
    df = parse_history(raw('eod_NVDA.US'), raw('div_NVDA.US'), raw('splits_NVDA.US'), 'USD')
    before, after = df.loc['2024-06-07', 'close'], df.loc['2024-06-10', 'close']
    assert before == pytest.approx(120.888) and 0.9 < after / before < 1.1
    assert df.loc['2024-06-10', 'split'] == 10.0


def test_raw_lse_dividends_in_pence():
    df = parse_history(raw('eod_SHEL.LSE'), raw('div_SHEL.LSE'), raw('splits_SHEL.LSE'), 'GBX')
    paid = df.loc[df['dividend'] > 0, 'dividend']
    assert 15 < paid.iloc[-1] < 60                          # tens of pence, like the ~3800p price


def test_raw_nokia_fundamentals():
    data = raw('fundamentals_NOKIA.HE_filter')
    y = parse_annual(data).loc['2025-12-31']
    assert y['ebit'] == pytest.approx(915e6 + 251e6) and y['revenue'] == pytest.approx(19889e6)
    info = parse_info(data)
    assert info['isin'] == 'FI0009000681' and info['financial_currency'] == 'EUR' and info['forward_eps'] > 0
