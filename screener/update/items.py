"""Updating item base info (name, type, exchange, sector, market cap, ...) from Yahoo's info endpoint."""
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from screener.db import log_fetch, now_utc

logger = logging.getLogger(__name__)

# item column -> Yahoo info keys, first non-empty wins
INFO_FIELDS = {
    'name': ('longName', 'shortName'),
    'type': ('quoteType',),
    'exchange': ('exchange',),
    'exchange_name': ('fullExchangeName',),
    'country': ('country',),
    'currency': ('currency',),
    'sector': ('sector',),
    'industry': ('industry',),
    'market_cap': ('marketCap',),
    'total_assets': ('totalAssets',),
    'shares_outstanding': ('sharesOutstanding',),
}

# Stop the run (it is resumable) when Yahoo fails this many tickers in a row: it is probably blocking us.
MAX_CONSECUTIVE_FAILURES = 25


def info_to_columns(info: dict) -> dict:
    """Pick item columns from a Yahoo info dict. Missing values are None."""
    cols = {}
    for col, keys in INFO_FIELDS.items():
        cols[col] = next((info[k] for k in keys if info.get(k) not in (None, '')), None)
    return cols


def select_items(conn: sqlite3.Connection, tickers: list[str] | None, max_age_days: float) -> list[sqlite3.Row]:
    """Active items whose info is missing or older than max_age_days (or exactly the given tickers)."""
    if tickers:
        marks = ','.join('?' * len(tickers))
        return conn.execute(f'SELECT item_id, ticker FROM item WHERE ticker IN ({marks}) ORDER BY ticker',
                            [t.upper() for t in tickers]).fetchall()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec='seconds')
    return conn.execute(
        """SELECT item_id, ticker FROM item
           WHERE active = 1 AND (info_updated_at IS NULL OR info_updated_at < ?)
           ORDER BY info_updated_at IS NOT NULL, info_updated_at, ticker""", (cutoff,)).fetchall()


def update_items(conn: sqlite3.Connection, yahoo, tickers: list[str] | None = None,
                 max_age_days: float = 7) -> dict:
    """Fetch info for each selected item and store it. Failures are logged per ticker; the run continues."""
    items = select_items(conn, tickers, max_age_days)
    logger.info(f'update items: {len(items)} to fetch')
    ok = failed = consecutive = 0
    for n, item in enumerate(items, 1):
        try:
            info = yahoo.get_info(item['ticker'])
            # Yahoo answers unknown symbols with a near-empty dict, e.g. {'trailingPegRatio': None}.
            error = None if info.get('quoteType') else 'no info returned (unknown symbol?)'
        except Exception as e:
            info, error = None, f'{type(e).__name__}: {e}'
        if error:
            failed += 1
            consecutive += 1
            logger.warning(f'{item["ticker"]}: {error}')
            log_fetch(conn, 'info', item['ticker'], error)
            conn.commit()
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                logger.error(f'{consecutive} failures in a row, stopping (re-run later to resume)')
                break
            continue

        consecutive = 0
        cols = info_to_columns(info)
        now = now_utc()
        # COALESCE keeps a known value when Yahoo temporarily omits a field.
        sets = ', '.join(f'{c} = COALESCE(?, {c})' for c in cols)
        conn.execute(f'UPDATE item SET {sets}, info_updated_at = ?, '
                     'market_cap_date = CASE WHEN ? IS NOT NULL THEN ? ELSE market_cap_date END '
                     'WHERE item_id = ?',
                     [*cols.values(), now, cols['market_cap'], date.today().isoformat(), item['item_id']])
        conn.execute('INSERT OR REPLACE INTO item_info_raw (item_id, fetched_at, info_json) VALUES (?, ?, ?)',
                     (item['item_id'], now, json.dumps(info, default=str)))
        log_fetch(conn, 'info', item['ticker'])
        conn.commit()
        ok += 1
        if n % 50 == 0:
            logger.info(f'update items: {n}/{len(items)}')
    return {'selected': len(items), 'ok': ok, 'failed': failed}
