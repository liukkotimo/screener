"""
Thin Yahoo Finance client on top of yfinance.

Kept free of database code so it can be reused elsewhere (e.g. moved back to the portfolio project).
Every method either returns data or raises / returns empty; callers decide how to log failures.
"""
import contextlib
import logging
import time
from datetime import date

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Yahoo quotes some exchanges in minor currency units (e.g. LSE in pence).
# currency code -> (major currency code, factor from quoted price to major unit)
MINOR_UNITS = {'GBp': ('GBP', 0.01), 'GBX': ('GBP', 0.01), 'ILA': ('ILS', 0.01), 'ZAc': ('ZAR', 0.01)}

# yf.download column -> our column name
_HISTORY_COLUMNS = {'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Adj Close': 'adj_close',
                    'Volume': 'volume', 'Dividends': 'dividend', 'Stock Splits': 'split'}


def major_currency(currency: str | None) -> tuple[str | None, float]:
    """('GBp') -> ('GBP', 0.01); ('USD') -> ('USD', 1.0)."""
    return MINOR_UNITS.get(currency, (currency, 1.0))


@contextlib.contextmanager
def quiet_yfinance():
    """Temporarily silence the 'yfinance' logger (it logs every failed ticker at ERROR level)."""
    lg = logging.getLogger('yfinance')
    old = lg.level
    lg.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        lg.setLevel(old)


class YahooFinance:
    def __init__(self, min_interval: float = 1.0):
        self.min_interval = min_interval  # seconds between outgoing calls
        self._last_call = 0.0

    def _throttle(self):
        """Simple blocking throttle between outgoing Yahoo calls."""
        wait = self._last_call + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get_info(self, ticker: str) -> dict:
        """Raw Yahoo info dict (name, quoteType, exchange, currency, sector, marketCap, ...). {} if none."""
        self._throttle()
        with quiet_yfinance():
            return yf.Ticker(ticker).info or {}

    def get_dividends(self, ticker: str) -> pd.Series:
        """Full dividend history, index = ex-date (naive, exchange-local), values per share in quote units."""
        self._throttle()
        with quiet_yfinance():
            s = yf.Ticker(ticker).dividends
        if s.index.tz is not None:
            s.index = s.index.tz_localize(None)
        return s

    def get_history_bulk(self, tickers: list[str], start: date, batch_size: int = 50) -> dict[str, pd.DataFrame]:
        """
        Daily history for many tickers via batched yf.download.

        Returns {ticker: DataFrame} with index = date (naive, exchange-local) and columns
        open, high, low, close, adj_close, volume, dividend, split. 'close' is split-adjusted,
        'adj_close' split- and dividend-adjusted. Rows without a close are dropped.
        Tickers Yahoo returned nothing for are absent from the result; a failing batch is logged and skipped.
        """
        tickers = list(dict.fromkeys(tickers))
        result = {}
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i:i + batch_size]
            self._throttle()
            try:
                with quiet_yfinance():
                    df = yf.download(batch, start=start, interval='1d', auto_adjust=False, actions=True,
                                     group_by='column', threads=True, progress=False)
            except Exception as e:  # one bad batch must not kill a long run
                logger.warning(f'download failed for batch {batch[0]}..{batch[-1]}: {e}')
                continue
            if df is None or df.empty:
                continue
            if not isinstance(df.columns, pd.MultiIndex):
                df.columns = pd.MultiIndex.from_product([df.columns, batch[:1]])
            for ticker in batch:
                if ticker not in df.columns.get_level_values(1):
                    continue
                t = df.xs(ticker, axis=1, level=1).rename(columns=_HISTORY_COLUMNS)
                t = t[[c for c in _HISTORY_COLUMNS.values() if c in t.columns]].dropna(subset=['close'])
                if t.empty:
                    continue
                if t.index.tz is not None:
                    t.index = t.index.tz_localize(None)
                result[ticker] = t
            logger.info(f'history {min(i + batch_size, len(tickers))}/{len(tickers)}')
        return result

    def get_fx_history(self, currencies: list[str], start: date) -> dict[str, pd.Series]:
        """
        EUR value of one unit of each MAJOR currency, from Yahoo pairs EUR<CUR>=X (which quote CUR per EUR).
        Returns {currency: Series indexed by date}. Missing currencies are absent.
        """
        pairs = {f'EUR{c}=X': c for c in currencies if c and c != 'EUR'}
        history = self.get_history_bulk(list(pairs), start)
        return {pairs[p]: 1.0 / df['close'] for p, df in history.items()}

    def screen_equities(self, region: str, min_market_cap: float = 5e8, page_size: int = 250,
                        max_rows: int = 20000) -> list[dict]:
        """
        Yahoo equity screener for one region, largest market cap first.

        region          Yahoo region code, e.g. 'us', 'fi', 'se', 'jp'.
        min_market_cap  Server-side lower bound. Yahoo does not document the currency, so keep it loose
                        and filter precisely later in a screening profile.
        Returns raw Yahoo quote dicts (symbol, shortName, longName, exchange, currency, marketCap, quoteType, ...).
        """
        eq = yf.EquityQuery
        query = eq('and', [eq('eq', ['region', region]), eq('gt', ['intradaymarketcap', min_market_cap])])
        rows, offset = [], 0
        while offset < max_rows:
            self._throttle()
            resp = yf.screen(query, offset=offset, size=page_size, sortField='intradaymarketcap', sortAsc=False)
            quotes = (resp or {}).get('quotes') or []
            rows.extend(quotes)
            offset += len(quotes)
            total = (resp or {}).get('total', 0)
            logger.info(f'screen {region}: {offset}/{total}')
            if not quotes or offset >= total:
                break
        return rows
