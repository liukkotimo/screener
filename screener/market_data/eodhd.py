"""
Thin EODHD (eodhd.com) client on top of requests.

Kept free of database code so it can be reused elsewhere. HTTP calls go through EodhdClient (throttle,
retries, call counting); parsing is done by plain functions below so tests can run them on stored responses.

Call costs (EODHD's daily quota): eod, div, splits and exchange-symbol-list 1 call; fundamentals 10;
eod-bulk-last-day 100; user 0.
"""
import logging
import os
import time
from datetime import date

import pandas as pd
import requests

logger = logging.getLogger(__name__)

BASE_URL = 'https://eodhd.com/api'

# EODHD quotes some exchanges in minor currency units (LSE prices in pence, 'GBX'); dividends and market cap
# are in the major unit. currency code -> (major currency code, factor from quoted price to major unit)
MINOR_UNITS = {'GBX': ('GBP', 0.01), 'GBp': ('GBP', 0.01), 'ILA': ('ILS', 0.01), 'ZAC': ('ZAR', 0.01)}

# fundamentals sections fetched (the call costs 10 whatever the filter; the filter keeps the response small)
FUNDAMENTALS_FILTER = ','.join([
    'General', 'Highlights', 'Valuation', 'SharesStats', 'ETF_Data::TotalAssets', 'Earnings::Trend',
    'Financials::Income_Statement::yearly', 'Financials::Balance_Sheet::yearly', 'Financials::Cash_Flow::yearly'])

# EODHD statement field -> our fundamental_annual column, per statement section.
# ebit and ebitda are not taken from EODHD's own fields (inconsistent, e.g. Nokia 2025 ebit < pretax income)
# but derived in parse_annual: ebit = pretax income + interest expense, ebitda = ebit + D&A.
_STATEMENT_FIELDS = {
    'Financials::Income_Statement::yearly': {
        'totalRevenue': 'revenue', 'incomeBeforeTax': 'pretax_income', 'taxProvision': 'tax_provision',
        'interestExpense': 'interest_expense', 'netIncomeApplicableToCommonShares': 'net_income'},
    'Financials::Balance_Sheet::yearly': {
        'shortLongTermDebtTotal': 'total_debt', 'cashAndShortTermInvestments': 'cash_and_st_investments',
        'totalStockholderEquity': 'stockholders_equity', 'commonStockSharesOutstanding': 'shares_outstanding'},
    'Financials::Cash_Flow::yearly': {
        'totalCashFromOperatingActivities': 'operating_cash_flow', 'capitalExpenditures': 'capital_expenditure',
        'freeCashFlow': 'free_cash_flow', 'dividendsPaid': 'dividends_paid'},
}
FUNDAMENTAL_COLUMNS = ('revenue', 'ebit', 'ebitda', 'pretax_income', 'tax_provision', 'interest_expense',
                       'net_income', 'operating_cash_flow', 'capital_expenditure', 'free_cash_flow',
                       'dividends_paid', 'total_debt', 'cash_and_st_investments', 'stockholders_equity',
                       'shares_outstanding')
# Outflows stored as positive amounts (EODHD gives them positive today; abs() keeps it robust).
_POSITIVE_COLUMNS = ('interest_expense', 'capital_expenditure', 'dividends_paid')

HISTORY_COLUMNS = ('open', 'high', 'low', 'close', 'adj_close', 'volume', 'dividend', 'split')


class EodhdError(Exception):
    """A failed EODHD request. status is the HTTP status (404 = unknown symbol), None for network errors."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class QuotaExhausted(EodhdError):
    """The next call would leave less than EodhdClient.reserve of today's quota. Runs stop; re-run tomorrow."""


def major_currency(currency: str | None) -> tuple[str | None, float]:
    """('GBX') -> ('GBP', 0.01); ('USD') -> ('USD', 1.0)."""
    return MINOR_UNITS.get(currency, (currency, 1.0))


def _num(v) -> float | None:
    """EODHD numbers come as numbers, numeric strings, None or 'NA'."""
    if v is None or v == '' or v == 'NA':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _section(data: dict, key: str) -> dict:
    """A fundamentals section; EODHD returns the string 'NA' for sections an item does not have."""
    v = data.get(key)
    return v if isinstance(v, dict) else {}


def split_ratio(text: str) -> float | None:
    """'10.000000/1.000000' -> 10.0 (new shares per old share); '1.000000/5.000000' -> 0.2."""
    try:
        new, old = (float(x) for x in text.split('/'))
        return new / old if new > 0 and old > 0 else None
    except (AttributeError, ValueError):
        return None


def parse_history(eod: list[dict], dividends: list[dict], splits: list[dict],
                  currency: str | None = None) -> pd.DataFrame:
    """
    Daily history as a DataFrame indexed by date with HISTORY_COLUMNS, ascending.

    EODHD's open/high/low/close are RAW (not split-adjusted); here they are divided by the product of all
    later split ratios, so 'close' is split-adjusted (dividend yields need that). EODHD's volume is already
    split-adjusted and kept as is. adj_close is EODHD's adjusted_close (splits and dividends). Dividends are EODHD's split-adjusted
    'value' per ex-date, converted to the quote unit (EODHD gives GBP while LSE prices are in GBX); ex-dates
    without a price row (e.g. announced future dividends) are dropped. split is the ratio on split days, else 0.
    """
    rows = [r for r in eod if _num(r.get('close'))]
    if not rows:
        return pd.DataFrame(columns=list(HISTORY_COLUMNS))
    df = pd.DataFrame(rows).set_index('date')
    df.index = pd.to_datetime(df.index)
    df = df.rename(columns={'adjusted_close': 'adj_close'})
    df = df[['open', 'high', 'low', 'close', 'adj_close', 'volume']].apply(pd.to_numeric, errors='coerce')
    df = df.sort_index()

    df['split'] = 0.0
    factor = pd.Series(1.0, index=df.index)  # product of the ratios of all later splits
    for s in splits:
        ratio, day = split_ratio(s.get('split')), pd.Timestamp(s['date'])
        if ratio is None:
            continue
        factor[factor.index < day] *= ratio
        if day in df.index:
            df.loc[day, 'split'] = ratio
    for col in ('open', 'high', 'low', 'close'):
        df[col] = df[col] / factor

    major, unit_factor = major_currency(currency)
    df['dividend'] = 0.0
    for d in dividends:
        amount, day = _num(d.get('value')), pd.Timestamp(d['date'])
        if not amount or day not in df.index:
            continue
        if unit_factor != 1.0 and d.get('currency') == major:
            amount /= unit_factor
        df.loc[day, 'dividend'] += amount
    return df[list(HISTORY_COLUMNS)]


def parse_annual(data: dict) -> pd.DataFrame:
    """
    Annual statements from a fundamentals response, indexed by fiscal year end (ascending), with
    FUNDAMENTAL_COLUMNS plus 'currency' (the statement currency of that year). NaN where not reported;
    years without any figure are dropped. Empty DataFrame if there are no statements.
    """
    years = {}
    for section, fields in _STATEMENT_FIELDS.items():
        for key, row in _section(data, section).items():
            if not isinstance(row, dict):
                continue
            y = years.setdefault(row.get('date') or key, {'currency': None})
            y['currency'] = y['currency'] or row.get('currency_symbol')
            for field, col in fields.items():
                y[col] = _num(row.get(field))
            if section.startswith('Financials::Income_Statement'):
                if y['net_income'] is None:
                    y['net_income'] = _num(row.get('netIncome'))
                y['_dna'] = _num(row.get('depreciationAndAmortization'))
                if y['_dna'] is None:
                    y['_dna'] = _num(row.get('reconciledDepreciation'))
    if not years:
        return pd.DataFrame(columns=['currency', *FUNDAMENTAL_COLUMNS])
    df = pd.DataFrame.from_dict(years, orient='index')
    df.index = pd.to_datetime(df.index)
    df = df.reindex(columns=['currency', *FUNDAMENTAL_COLUMNS, '_dna'])
    num_cols = [*FUNDAMENTAL_COLUMNS, '_dna']
    df[num_cols] = df[num_cols].astype(float)
    df['ebit'] = df['pretax_income'] + df['interest_expense'].abs()
    df['ebitda'] = df['ebit'] + df['_dna']
    for c in _POSITIVE_COLUMNS:
        df[c] = df[c].abs()
    df = df.drop(columns='_dna').dropna(how='all', subset=list(FUNDAMENTAL_COLUMNS))
    return df.sort_index()


def forward_eps(data: dict) -> tuple[float | None, str | None]:
    """
    Consensus EPS estimate for the next fiscal year: the latest Earnings::Trend entry with period '+1y'
    (earningsEstimateAvg, financial currency). Returns (eps, fiscal year end of that estimate).
    Highlights.EPSEstimateNextYear is not used: it is stale for non-December fiscal years.
    """
    trend = [t for t in _section(data, 'Earnings::Trend').values()
             if isinstance(t, dict) and t.get('period') == '+1y' and _num(t.get('earningsEstimateAvg')) is not None]
    if not trend:
        return None, None
    latest = max(trend, key=lambda t: t['date'])
    return _num(latest['earningsEstimateAvg']), latest['date']


def parse_info(data: dict) -> dict:
    """Item columns from a fundamentals response. Missing values are None."""
    g, h, s = _section(data, 'General'), _section(data, 'Highlights'), _section(data, 'SharesStats')
    etf_assets = data.get('ETF_Data::TotalAssets')
    annual_ccy = [r.get('currency_symbol') for _, r in sorted(_section(data, 'Financials::Income_Statement::yearly')
                                                              .items(), reverse=True) if isinstance(r, dict)]
    eps, eps_period = forward_eps(data)
    return {
        'name': g.get('Name') or None,
        'type': g.get('Type') or None,
        'country': g.get('CountryName') or None,
        'currency': g.get('CurrencyCode') or None,
        'isin': g.get('ISIN') or None,
        'sector': g.get('Sector') or None,
        'industry': g.get('Industry') or None,
        'market_cap': _num(h.get('MarketCapitalization')),
        'shares_outstanding': _num(s.get('SharesOutstanding')),
        'total_assets': _num(etf_assets) if not isinstance(etf_assets, dict) else None,
        'financial_currency': next((c for c in annual_ccy if c), None),
        'forward_eps': eps,
        'forward_eps_period': eps_period,
    }


def raw_info(data: dict) -> dict:
    """The descriptive part of a fundamentals response (General, Highlights, Valuation), kept for debugging."""
    return {k: data.get(k) for k in ('General', 'Highlights', 'Valuation')}


def api_key() -> str:
    """$EODHD_API_KEY (the CLI loads .env into the environment first)."""
    key = os.environ.get('EODHD_API_KEY', '').strip()
    if not key:
        raise SystemExit('EODHD_API_KEY is not set (environment or .env)')
    return key


class EodhdClient:
    """
    Blocking EODHD client: throttle between calls, retries on 429/5xx, counts quota calls spent and refuses
    a call (QuotaExhausted) that would leave less than `reserve` calls of today's quota.
    """

    COSTS = {'fundamentals': 10, 'eod-bulk-last-day': 100, 'user': 0}  # endpoint -> calls; default 1
    QUOTA_RECHECK_CALLS = 2000  # re-read /api/user after this many calls (other programs may use the key too)

    def __init__(self, key: str | None = None, min_interval: float = 0.1, retries: int = 4, timeout: float = 60,
                 reserve: int = 1000):
        self._key = key or api_key()
        self.min_interval = min_interval  # seconds between outgoing calls (~10/s; EODHD allows 1000/min)
        self.retries = retries
        self.timeout = timeout
        self.reserve = reserve  # calls of the daily quota left untouched for manual use
        self.calls = 0  # quota calls spent by this client
        self._quota_left = None  # calls left today at the last /api/user check
        self._calls_at_check = 0
        self._last_call = 0.0
        self._session = requests.Session()

    def _check_quota(self, cost: int):
        """Raise QuotaExhausted if spending `cost` calls would cut into the reserve."""
        if self._quota_left is None or self.calls - self._calls_at_check >= self.QUOTA_RECHECK_CALLS:
            u = self.get_usage()
            self._quota_left, self._calls_at_check = u['limit'] - u['used'], self.calls
        left = self._quota_left - (self.calls - self._calls_at_check)
        if left - cost < self.reserve:
            raise QuotaExhausted(f'daily EODHD quota nearly used up ({left} calls left, reserve {self.reserve})')

    def _throttle(self):
        """Simple blocking throttle between outgoing calls."""
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def _get(self, path: str, **params):
        """GET BASE_URL/path as JSON. Retries 429, 5xx and network errors with backoff; raises EodhdError."""
        endpoint = path.split('/')[0]
        what = f'{path} {params}' if params else path  # for messages; never contains the key
        cost = self.COSTS.get(endpoint, 1)
        if cost:
            self._check_quota(cost)
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                r = self._session.get(f'{BASE_URL}/{path}', timeout=self.timeout,
                                      params={**params, 'api_token': self._key, 'fmt': 'json'})
            except requests.RequestException as e:
                status, error = None, f'{type(e).__name__}'
            else:
                status = r.status_code
                if status in (200, 404):  # EODHD charges for unknown symbols too
                    self.calls += cost
                if status == 200:
                    try:
                        return r.json()
                    except ValueError:
                        raise EodhdError(f'{what}: response is not JSON', status) from None
                if status == 404:
                    raise EodhdError(f'{what}: not found', 404)
                if status != 429 and status < 500:
                    raise EodhdError(f'{what}: HTTP {status} {r.text[:100].strip()}', status)
                error = f'HTTP {status}'
            if attempt == self.retries:
                raise EodhdError(f'{what}: {error} after {self.retries + 1} attempts', status)
            delay = 2 ** (attempt + 1)
            if status == 429:
                delay = max(delay, _num(r.headers.get('Retry-After')) or 0, 30)
            logger.warning(f'{what}: {error}, retrying in {delay:g}s')
            time.sleep(delay)

    def get_usage(self) -> dict:
        """Today's quota from /api/user: {'used', 'limit', 'extra', 'date'} (free call)."""
        u = self._get('user')
        return {'used': u.get('apiRequests'), 'limit': u.get('dailyRateLimit'), 'extra': u.get('extraLimit'),
                'date': u.get('apiRequestsDate')}

    def get_exchanges(self) -> list[dict]:
        """All exchanges: dicts with Code, Name, Country, Currency, CountryISO2, OperatingMIC."""
        return self._get('exchanges-list/')

    def get_exchange_symbols(self, exchange: str, delisted: bool = False) -> list[dict]:
        """Listed symbols of an exchange: dicts with Code, Name, Country, Exchange, Currency, Type, Isin."""
        return self._get(f'exchange-symbol-list/{exchange}', **({'delisted': 1} if delisted else {}))

    def get_bulk_last_day(self, exchange: str, extended: bool = True) -> list[dict]:
        """
        Last trading day of every symbol of an exchange (100 calls). Extended adds MarketCapitalization
        (quote currency), name, type, EMAs and 250-day high/low.
        """
        return self._get(f'eod-bulk-last-day/{exchange}', **({'filter': 'extended'} if extended else {}))

    def get_history(self, symbol: str, start: date, currency: str | None = None) -> pd.DataFrame:
        """
        Daily prices since start with dividends and splits (3 calls), see parse_history.
        currency: the item's quote currency, needed to put LSE dividends into pence. Raises EodhdError.
        """
        since = {'from': start.isoformat()}
        eod = self._get(f'eod/{symbol}', **since)
        divs = self._get(f'div/{symbol}', **since)
        splits = self._get(f'splits/{symbol}', **since)
        return parse_history(eod, divs, splits, currency)

    def get_fundamentals(self, symbol: str) -> dict:
        """Raw fundamentals response, filtered to FUNDAMENTALS_FILTER (10 calls). Parse with parse_info/annual."""
        return self._get(f'fundamentals/{symbol}', filter=FUNDAMENTALS_FILTER)

    def get_fx_history(self, currencies: list[str], start: date) -> dict[str, pd.Series]:
        """
        EUR value of one unit of each MAJOR currency, from EUR<CUR>.FOREX (which quotes CUR per EUR; the
        reverse pairs have only 4 decimals). Returns {currency: Series indexed by date}. Failed ones are absent.
        """
        result = {}
        for c in currencies:
            if not c or c == 'EUR':
                continue
            try:
                rows = self._get(f'eod/EUR{c}.FOREX', **{'from': start.isoformat()})
            except QuotaExhausted:
                raise
            except EodhdError as e:
                logger.warning(f'FX {c}: {e}')
                continue
            s = pd.Series({pd.Timestamp(r['date']): _num(r.get('close')) for r in rows}, dtype=float)
            s = s[s > 0]
            if not s.empty:
                result[c] = 1.0 / s.sort_index()
        return result
