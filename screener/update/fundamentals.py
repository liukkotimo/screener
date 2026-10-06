"""Updating annual financial statements (fundamental_annual) from Yahoo, for active equities."""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

from screener.db import log_fetch, now_utc
from screener.market_data.yahoo_finance import FUNDAMENTAL_COLUMNS
from screener.update.items import MAX_CONSECUTIVE_FAILURES

logger = logging.getLogger(__name__)


def select_items(conn: sqlite3.Connection, tickers: list[str] | None, max_age_days: float) -> list[sqlite3.Row]:
    """Active equities whose statements are missing or older than max_age_days (or exactly the given tickers)."""
    if tickers:
        marks = ','.join('?' * len(tickers))
        return conn.execute(f'SELECT item_id, ticker, financial_currency FROM item WHERE ticker IN ({marks}) '
                            'ORDER BY ticker', [t.upper() for t in tickers]).fetchall()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec='seconds')
    return conn.execute(
        """SELECT item_id, ticker, financial_currency FROM item
           WHERE active = 1 AND type = 'EQUITY' AND (fundamentals_updated_at IS NULL OR fundamentals_updated_at < ?)
           ORDER BY fundamentals_updated_at IS NOT NULL, fundamentals_updated_at, ticker""", (cutoff,)).fetchall()


def store_annual(conn: sqlite3.Connection, item_id: int, currency: str | None, annual: pd.DataFrame) -> int:
    """Insert or replace one row per fiscal year. Years Yahoo no longer returns are kept."""
    cols = ['item_id', 'fiscal_year_end', 'currency', *FUNDAMENTAL_COLUMNS]
    rows = [[item_id, d.strftime('%Y-%m-%d'), currency,
             *(None if pd.isna(row.get(c)) else float(row[c]) for c in FUNDAMENTAL_COLUMNS)]
            for d, row in annual.iterrows()]
    conn.executemany(f'INSERT OR REPLACE INTO fundamental_annual ({", ".join(cols)}) '
                     f'VALUES ({", ".join("?" * len(cols))})', rows)
    return len(rows)


def update_fundamentals(conn: sqlite3.Connection, yahoo, tickers: list[str] | None = None,
                        max_age_days: float = 30) -> dict:
    """Fetch annual statements for each selected item. Failures are logged per ticker; the run continues."""
    items = select_items(conn, tickers, max_age_days)
    logger.info(f'update fundamentals: {len(items)} to fetch')
    ok = failed = consecutive = years = 0
    for n, item in enumerate(items, 1):
        try:
            annual = yahoo.get_annual_financials(item['ticker'])
            error = None if not annual.empty else 'no financial statements returned'
        except Exception as e:
            annual, error = None, f'{type(e).__name__}: {e}'
        if error:
            failed += 1
            consecutive += 1
            logger.warning(f'{item["ticker"]}: {error}')
            log_fetch(conn, 'fundamentals', item['ticker'], error)
            if annual is not None:  # Yahoo answered without statements: don't ask again before max_age_days
                conn.execute('UPDATE item SET fundamentals_updated_at = ? WHERE item_id = ?',
                             (now_utc(), item['item_id']))
            conn.commit()
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                logger.error(f'{consecutive} failures in a row, stopping (re-run later to resume)')
                break
            continue

        consecutive = 0
        years += store_annual(conn, item['item_id'], item['financial_currency'], annual)
        conn.execute('UPDATE item SET fundamentals_updated_at = ? WHERE item_id = ?', (now_utc(), item['item_id']))
        log_fetch(conn, 'fundamentals', item['ticker'])
        conn.commit()
        ok += 1
        if n % 50 == 0:
            logger.info(f'update fundamentals: {n}/{len(items)}')
    return {'selected': len(items), 'ok': ok, 'failed': failed, 'years': years}
