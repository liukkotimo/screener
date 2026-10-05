"""
Incremental update of daily prices and dividends.

Yahoo re-adjusts the whole history after every dividend (adj_close) and split (close and adj_close).
Appending new days to an old, differently adjusted history would corrupt returns, so each incremental
fetch re-downloads a few overlapping days. If a stored overlapping day no longer matches Yahoo, the item's
whole history is re-downloaded and replaced.
"""
import logging
import math
import sqlite3
from datetime import date, timedelta

import pandas as pd

from screener.db import log_fetch

logger = logging.getLogger(__name__)

OVERLAP_DAYS = 10       # calendar days re-downloaded before the last stored date
MATCH_TOLERANCE = 1e-4  # relative difference that counts as "Yahoo re-adjusted the history"


def _num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)


def select_items(conn: sqlite3.Connection, tickers: list[str] | None) -> list[sqlite3.Row]:
    sql = """SELECT i.item_id, i.ticker, (SELECT MAX(date) FROM price_daily p WHERE p.item_id = i.item_id) AS last_date
             FROM item i WHERE """
    if tickers:
        return conn.execute(sql + f'i.ticker IN ({",".join("?" * len(tickers))})',
                            [t.upper() for t in tickers]).fetchall()
    return conn.execute(sql + 'i.active = 1').fetchall()


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


def update_prices(conn: sqlite3.Connection, yahoo, tickers: list[str] | None = None, years: int = 5,
                  batch_size: int = 50) -> dict:
    items = select_items(conn, tickers)
    full_start = (pd.Timestamp.today().normalize() - pd.DateOffset(years=years)).date()
    stats = {'selected': len(items), 'ok': 0, 'failed': 0, 'refetched': 0, 'rows': 0}

    def fetch_full(group):
        history = yahoo.get_history_bulk([r['ticker'] for r in group], full_start, batch_size)
        for r in group:
            df = history.get(r['ticker'])
            if df is None:
                stats['failed'] += 1
                logger.warning(f'{r["ticker"]}: no price data returned')
                log_fetch(conn, 'price', r['ticker'], 'no price data returned')
            else:
                stats['rows'] += store_history(conn, r['item_id'], df, replace_all=True)
                stats['ok'] += 1
                log_fetch(conn, 'price', r['ticker'])
            conn.commit()

    new = [r for r in items if r['last_date'] is None]
    existing = sorted((r for r in items if r['last_date'] is not None), key=lambda r: r['last_date'])
    logger.info(f'update prices: {len(new)} new (since {full_start}), {len(existing)} incremental')

    for i in range(0, len(new), batch_size):
        fetch_full(new[i:i + batch_size])

    # Incremental: items sorted by last date, so each batch shares a similar start date.
    refetch = []
    for i in range(0, len(existing), batch_size):
        group = existing[i:i + batch_size]
        start = date.fromisoformat(group[0]['last_date']) - timedelta(days=OVERLAP_DAYS)
        history = yahoo.get_history_bulk([r['ticker'] for r in group], start, batch_size)
        for r in group:
            df = history.get(r['ticker'])
            if df is None:
                stats['failed'] += 1
                logger.warning(f'{r["ticker"]}: no price data returned')
                log_fetch(conn, 'price', r['ticker'], 'no price data returned')
                continue
            mismatch = history_mismatch(conn, r['item_id'], df, r['last_date'])
            if mismatch:
                logger.info(f'{r["ticker"]}: history re-adjusted by Yahoo ({mismatch}), re-downloading')
                refetch.append(r)
                continue
            stats['rows'] += store_history(conn, r['item_id'], df)
            stats['ok'] += 1
            log_fetch(conn, 'price', r['ticker'])
        conn.commit()

    stats['refetched'] = len(refetch)
    for i in range(0, len(refetch), batch_size):
        fetch_full(refetch[i:i + batch_size])
    return stats
