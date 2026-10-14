"""
Incremental update of daily prices and dividends, one item at a time (3 EODHD calls: eod, div, splits).

EODHD recomputes the whole adjusted_close history after every dividend and split, and our split-adjusted
close changes after a split. Appending new days to an old, differently adjusted history would corrupt
returns, so each incremental fetch re-downloads a few overlapping days. If a stored overlapping day no longer
matches, the item's whole history is re-downloaded and replaced.
"""
import logging
import math
import sqlite3
from datetime import date, timedelta

import pandas as pd

from screener.db import log_fetch
from screener.market_data.eodhd import EodhdError, QuotaExhausted

logger = logging.getLogger(__name__)

OVERLAP_DAYS = 10       # calendar days re-downloaded before the last stored date
MATCH_TOLERANCE = 1e-4  # relative difference that counts as "the history was re-adjusted"


def _num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def select_items(conn: sqlite3.Connection, tickers: list[str] | None) -> list[sqlite3.Row]:
    sql = """SELECT i.item_id, i.ticker, i.currency,
                    (SELECT MAX(date) FROM price_daily p WHERE p.item_id = i.item_id) AS last_date
             FROM item i WHERE """
    if tickers:
        return conn.execute(sql + f'i.ticker IN ({",".join("?" * len(tickers))})',
                            [t.upper() for t in tickers]).fetchall()
    return conn.execute(sql + 'i.active = 1 ORDER BY i.ticker').fetchall()


def history_mismatch(conn: sqlite3.Connection, item_id: int, df: pd.DataFrame, last_date: str) -> str | None:
    """
    Compare downloaded rows with stored rows on overlapping dates before last_date (the last stored day may
    have been a partial, intraday row). Returns a description of the first mismatch, or None if consistent.
    """
    first = df.index.min().strftime('%Y-%m-%d')
    stored = conn.execute('SELECT date, close, adj_close FROM price_daily WHERE item_id = ? AND date >= ? AND date < ?',
                          (item_id, first, last_date)).fetchall()
    new = {d.strftime('%Y-%m-%d'): row for d, row in df.iterrows()}
    for s in stored:
        n = new.get(s['date'])
        if n is None:
            continue
        for col in ('close', 'adj_close'):
            old, cur = s[col], _num(n[col])
            if old is None or cur is None:
                continue
            if abs(cur - old) > MATCH_TOLERANCE * max(abs(old), 1e-12):
                return f'{col} on {s["date"]} changed {old:g} -> {cur:g}'
    return None


def store_history(conn: sqlite3.Connection, item_id: int, df: pd.DataFrame, replace_all: bool = False) -> int:
    """Upsert price rows and non-zero dividends. replace_all deletes the item's old prices/dividends first."""
    if replace_all:
        conn.execute('DELETE FROM price_daily WHERE item_id = ?', (item_id,))
        conn.execute('DELETE FROM dividend WHERE item_id = ?', (item_id,))
    rows = [(item_id, d.strftime('%Y-%m-%d'), _num(r.get('open')), _num(r.get('high')), _num(r.get('low')),
             _num(r['close']), _num(r.get('adj_close')), _num(r.get('volume')))
            for d, r in df.iterrows()]
    conn.executemany('INSERT OR REPLACE INTO price_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)
    if 'dividend' in df.columns:
        divs = df['dividend'].fillna(0)
        conn.executemany('INSERT OR REPLACE INTO dividend (item_id, ex_date, amount) VALUES (?, ?, ?)',
                         [(item_id, d.strftime('%Y-%m-%d'), float(a)) for d, a in divs[divs > 0].items()])
    return len(rows)


def _fetch(client, item, start: date) -> tuple[pd.DataFrame | None, str | None]:
    """(history, None) or (None, error). QuotaExhausted is passed on: the run must stop."""
    try:
        df = client.get_history(item['ticker'], start, item['currency'])
    except QuotaExhausted:
        raise
    except EodhdError as e:
        return None, 'no price data returned' if e.status == 404 else str(e)
    return (df, None) if not df.empty else (None, 'no price data returned')


def update_prices(conn: sqlite3.Connection, client, tickers: list[str] | None = None, years: int = 5) -> dict:
    items = select_items(conn, tickers)
    full_start = (pd.Timestamp.today().normalize() - pd.DateOffset(years=years)).date()
    stats = {'selected': len(items), 'ok': 0, 'failed': 0, 'refetched': 0, 'rows': 0}
    new = sum(1 for r in items if r['last_date'] is None)
    logger.info(f'update prices: {new} new (since {full_start}), {len(items) - new} incremental')

    def failed(item, error):
        stats['failed'] += 1
        logger.warning(f'{item["ticker"]}: {error}')
        log_fetch(conn, 'price', item['ticker'], error)

    try:
        for n, item in enumerate(items, 1):
            replace_all = item['last_date'] is None
            start = full_start if replace_all else date.fromisoformat(item['last_date']) - timedelta(days=OVERLAP_DAYS)
            df, error = _fetch(client, item, start)
            if not error and not replace_all:
                mismatch = history_mismatch(conn, item['item_id'], df, item['last_date'])
                if mismatch:
                    logger.info(f'{item["ticker"]}: history re-adjusted ({mismatch}), re-downloading')
                    stats['refetched'] += 1
                    replace_all = True
                    df, error = _fetch(client, item, full_start)
            if error:
                failed(item, error)
            else:
                stats['rows'] += store_history(conn, item['item_id'], df, replace_all)
                stats['ok'] += 1
                log_fetch(conn, 'price', item['ticker'])
            conn.commit()
            if n % 100 == 0:
                logger.info(f'update prices: {n}/{len(items)}')
    except QuotaExhausted as e:
        logger.error(f'{e}; stopping (re-run later to resume)')
    return stats
