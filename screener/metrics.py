"""
Calculating item_metrics rows from stored prices, dividends and FX rates.

compute_metrics() is pure (DataFrames in, dict out); calc_metrics() does the database work around it.
"""
import logging
import sqlite3

import pandas as pd

from screener.analytics.dividends import trailing_yield
from screener.analytics.returns import (days_since_low, drawdown_from_high, max_drawdown, performance,
                                        value_at, window)
from screener.analytics.risk import cvar, volatility
from screener.db import now_utc
from screener.market_data.yahoo_finance import major_currency

logger = logging.getLogger(__name__)

# Metric column -> definition. Shown by `screener metrics --list` and used in explanations.
METRICS = {
    'perf_1m': 'total return over 1 month (adj_close, not annualized)',
    'perf_3m': 'total return over 3 months',
    'perf_6m': 'total return over 6 months',
    'perf_1y': 'total return over 1 year',
    'perf_3y': 'total return over 3 years (not annualized)',
    'drawdown_52w': 'adj_close vs. its 52-week high: last / max - 1 (<= 0)',
    'drawdown_3y': 'adj_close vs. its 3-year high: last / max - 1 (<= 0)',
    'days_since_low_52w': 'calendar days since the lowest adj_close of the last 52 weeks',
    'max_drawdown_3y': 'worst peak-to-trough fall of adj_close within 3 years (<= 0)',
    'volatility_1y': 'annualized std. dev. of daily returns over 1 year (min 200 returns)',
    'cvar_95': 'avg of the worst 5% daily returns over 3 years, as positive loss (min 500 returns)',
    'price_eur': 'last close in EUR',
    'market_cap': 'Yahoo market cap scaled to as-of by the close price change, major currency',
    'market_cap_eur': 'market_cap in EUR at the as-of FX rate',
    'total_assets_eur': 'fund/ETF total assets (Yahoo, latest) in EUR at the as-of FX rate',
    'avg_value_traded_eur_3m': 'average daily close * volume over 3 months, in EUR (NULL if no volume data)',
    'dividend_yield': 'dividends (ex-date) over the last 12 months / close',
    'last_price_date': 'date of the last price on or before as-of',
    'n_obs': 'number of stored prices on or before as-of',
}

HISTORY_YEARS = 3  # longest metric window; prices older than this (plus a margin) are not loaded


def compute_metrics(prices: pd.DataFrame, dividends: pd.Series, item: dict, fx: pd.Series | None,
                    asof) -> dict:
    """
    prices     DataFrame indexed by date with close, adj_close, volume (quote units, e.g. pence)
    dividends  Series indexed by ex-date (quote units)
    item       dict with currency, market_cap, market_cap_date, total_assets
    fx         EUR per unit of the item's major currency, indexed by date; None for EUR items
    Returns {column: value} for item_metrics; None wherever a value cannot be computed.
    """
    asof = pd.Timestamp(asof)
    prices = prices[prices.index <= asof]
    close = prices['close']
    adj = prices['adj_close'] if prices['adj_close'].notna().any() else close
    major, unit_factor = major_currency(item.get('currency'))
    eur_per_unit = 1.0 if major == 'EUR' else (value_at(fx, asof) if fx is not None else None)

    def to_eur(v):  # v in major currency
        return None if v is None or eur_per_unit is None else v * eur_per_unit

    m = {
        'last_price_date': close.index[-1].strftime('%Y-%m-%d') if len(close) else None,
        'n_obs': int(close.notna().sum()),
        'perf_1m': performance(adj, asof, 1),
        'perf_3m': performance(adj, asof, 3),
        'perf_6m': performance(adj, asof, 6),
        'perf_1y': performance(adj, asof, 12),
        'perf_3y': performance(adj, asof, 36),
        'drawdown_52w': drawdown_from_high(adj, asof, 12),
        'drawdown_3y': drawdown_from_high(adj, asof, 36),
        'days_since_low_52w': days_since_low(adj, asof, 12),
        'max_drawdown_3y': max_drawdown(adj, asof, 36),
        'volatility_1y': volatility(adj, asof, 12),
        'cvar_95': cvar(adj, asof, 36),
        'dividend_yield': trailing_yield(dividends, close, asof, 12),
    }

    last_close = value_at(close, asof)
    m['price_eur'] = to_eur(last_close * unit_factor) if last_close is not None else None

    # Yahoo's market cap is a snapshot from market_cap_date; scale it by the price change to as-of.
    market_cap = None
    if item.get('market_cap') and item.get('market_cap_date') and last_close is not None:
        close_then = value_at(close, item['market_cap_date'])
        if close_then:
            market_cap = item['market_cap'] * last_close / close_then
    m['market_cap'] = market_cap
    m['market_cap_eur'] = to_eur(market_cap)
    m['total_assets_eur'] = to_eur(item.get('total_assets'))

    w = window(close, asof, 3)
    value_traded = None
    if w is not None:
        vol = prices['volume'].reindex(w.index.drop(w.index[0])).fillna(0)
        if vol.sum() > 0:
            value_traded = float((w.reindex(vol.index) * vol).mean()) * unit_factor
    m['avg_value_traded_eur_3m'] = to_eur(value_traded)
    return m


def default_asof(conn: sqlite3.Connection) -> str | None:
    return conn.execute('SELECT MAX(date) FROM price_daily').fetchone()[0]


def load_fx(conn: sqlite3.Connection) -> dict[str, pd.Series]:
    df = pd.read_sql_query('SELECT currency, date, eur_per_unit FROM fx_rate_daily', conn, parse_dates=['date'])
    return {c: g.set_index('date')['eur_per_unit'].sort_index() for c, g in df.groupby('currency')}


def calc_metrics(conn: sqlite3.Connection, asof: str | None = None, tickers: list[str] | None = None) -> dict:
    """Compute and store metrics for active items (or the given tickers) at asof (default: latest price date)."""
    asof = asof or default_asof(conn)
    if asof is None:
        raise SystemExit('no price data: run `screener update prices` first')
    oldest = (pd.Timestamp(asof) - pd.DateOffset(years=HISTORY_YEARS, days=30)).strftime('%Y-%m-%d')
    fx = load_fx(conn)

    sql = 'SELECT item_id, ticker, currency, market_cap, market_cap_date, total_assets FROM item WHERE '
    if tickers:
        items = conn.execute(sql + f'ticker IN ({",".join("?" * len(tickers))})', [t.upper() for t in tickers])
    else:
        items = conn.execute(sql + 'active = 1')
    items = [dict(r) for r in items.fetchall()]
    logger.info(f'metrics as of {asof}: {len(items)} items')

    stored = no_prices = 0
    for n, item in enumerate(items, 1):
        prices = pd.read_sql_query(
            'SELECT date, close, adj_close, volume FROM price_daily WHERE item_id = ? AND date BETWEEN ? AND ? '
            'ORDER BY date', conn, params=(item['item_id'], oldest, asof), parse_dates=['date'], index_col='date')
        if prices.empty:
            no_prices += 1
            conn.execute('DELETE FROM item_metrics WHERE item_id = ? AND asof_date = ?', (item['item_id'], asof))
            continue
        divs = pd.read_sql_query('SELECT ex_date, amount FROM dividend WHERE item_id = ? AND ex_date <= ?', conn,
                                 params=(item['item_id'], asof), parse_dates=['ex_date'],
                                 index_col='ex_date')['amount']
        m = compute_metrics(prices, divs, item, fx.get(major_currency(item['currency'])[0]), asof)
        # n_obs counts all history up to as-of, not only the loaded window
        m['n_obs'] = conn.execute('SELECT COUNT(*) FROM price_daily WHERE item_id = ? AND date <= ?',
                                  (item['item_id'], asof)).fetchone()[0]
        cols = {'item_id': item['item_id'], 'asof_date': asof, **m, 'calculated_at': now_utc()}
        conn.execute(f'INSERT OR REPLACE INTO item_metrics ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                     list(cols.values()))
        stored += 1
        if n % 200 == 0:
            conn.commit()
            logger.info(f'metrics {n}/{len(items)}')
    conn.commit()
    return {'asof': asof, 'items': len(items), 'stored': stored, 'no_prices': no_prices}
