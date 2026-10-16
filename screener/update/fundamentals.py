"""
Updating fundamentals from EODHD: one fundamentals request (10 calls) per item fills the annual and quarterly
statements (fundamental_annual, fundamental_quarterly) and the item's sector, industry, financial currency, ISIN and forward EPS.
Common stocks and ETFs (for total assets) are fetched.
"""
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from screener.db import log_fetch, now_utc
from screener.market_data.eodhd import (FUNDAMENTAL_COLUMNS, QuotaExhausted, parse_annual, parse_info, parse_quarterly,
                                         raw_info)

logger = logging.getLogger(__name__)

FETCH_TYPES = ('Common Stock', 'ETF')
STATEMENT_TYPES = ('Common Stock',)  # a fetch without statements counts as an error only for these
# Stop the run (it is resumable) when this many tickers fail in a row: something is wrong (key, network, ...).
MAX_CONSECUTIVE_FAILURES = 25

# item columns from parse_info: COALESCE keeps a known value when EODHD omits one
_KEEP_KNOWN = ('isin', 'sector', 'industry', 'shares_outstanding', 'total_assets', 'financial_currency')
# forward EPS is a snapshot: replaced as returned (NULL when there is no estimate any more)
_REPLACE = ('forward_eps', 'forward_eps_period')


def select_items(conn: sqlite3.Connection, tickers: list[str] | None, max_age_days: float,
                 limit: int | None = None) -> list[sqlite3.Row]:
    """Active stocks/ETFs whose fundamentals are missing or older than max_age_days (or exactly the given tickers)."""
    if tickers:
        marks = ','.join('?' * len(tickers))
        return conn.execute(f'SELECT item_id, ticker, type FROM item WHERE ticker IN ({marks}) '
                            'ORDER BY ticker', [t.upper() for t in tickers]).fetchall()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec='seconds')
    return conn.execute(
        f"""SELECT item_id, ticker, type FROM item
            WHERE active = 1 AND type IN ({",".join("?" * len(FETCH_TYPES))})
              AND (fundamentals_updated_at IS NULL OR fundamentals_updated_at < ?)
            ORDER BY fundamentals_updated_at IS NOT NULL, fundamentals_updated_at, market_cap DESC, ticker
            LIMIT ?""", (*FETCH_TYPES, cutoff, -1 if limit is None else limit)).fetchall()


def store_statements(conn: sqlite3.Connection, item_id: int, statements: pd.DataFrame,
                     table: str = 'fundamental_annual', key: str = 'fiscal_year_end') -> int:
    """
    Insert or replace one row per period (fiscal year, or quarter with table='fundamental_quarterly',
    key='quarter_end'). Periods EODHD no longer returns are kept.
    """
    cols = ['item_id', key, 'currency', *FUNDAMENTAL_COLUMNS]
    rows = [[item_id, d.strftime('%Y-%m-%d'), None if pd.isna(row.get('currency')) else row['currency'],
             *(None if pd.isna(row.get(c)) else float(row[c]) for c in FUNDAMENTAL_COLUMNS)]
            for d, row in statements.iterrows()]
    conn.executemany(f'INSERT OR REPLACE INTO {table} ({", ".join(cols)}) '
                     f'VALUES ({", ".join("?" * len(cols))})', rows)
    return len(rows)


def store_info(conn: sqlite3.Connection, item_id: int, info: dict, raw: dict) -> None:
    sets = [f'{c} = COALESCE(?, {c})' for c in _KEEP_KNOWN] + [f'{c} = ?' for c in _REPLACE]
    conn.execute(f'UPDATE item SET {", ".join(sets)}, forward_eps_date = ?, fundamentals_updated_at = ? '
                 'WHERE item_id = ?',
                 [*(info.get(c) for c in _KEEP_KNOWN), *(info.get(c) for c in _REPLACE),
                  date.today().isoformat() if info.get('forward_eps') is not None else None, now_utc(), item_id])
    conn.execute('INSERT OR REPLACE INTO item_info_raw (item_id, fetched_at, info_json) VALUES (?, ?, ?)',
                 (item_id, now_utc(), json.dumps(raw, default=str)))


def update_fundamentals(conn: sqlite3.Connection, client, tickers: list[str] | None = None,
                        max_age_days: float = 90, limit: int | None = None) -> dict:
    """Fetch fundamentals for each selected item. Failures are logged per ticker; the run continues."""
    items = select_items(conn, tickers, max_age_days, limit)
    logger.info(f'update fundamentals: {len(items)} to fetch ({len(items) * 10} calls)')
    ok = failed = consecutive = years = quarters = 0
    for n, item in enumerate(items, 1):
        try:
            data = client.get_fundamentals(item['ticker'])
            error = None if isinstance(data, dict) and isinstance(data.get('General'), dict) \
                else 'no fundamentals returned'
        except QuotaExhausted as e:
            logger.error(f'{e}; stopping (re-run later to resume)')
            break
        except Exception as e:
            data, error = None, f'{type(e).__name__}: {e}'
        annual = parse_annual(data) if not error else None
        if not error and annual.empty and item['type'] in STATEMENT_TYPES:
            error = 'no financial statements returned'
        if error:
            failed += 1
            consecutive += 1
            logger.warning(f'{item["ticker"]}: {error}')
            log_fetch(conn, 'fundamentals', item['ticker'], error)
            if annual is not None:  # EODHD answered without statements: keep what it gave, don't ask again soon
                store_info(conn, item['item_id'], parse_info(data), raw_info(data))
            conn.commit()
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                logger.error(f'{consecutive} failures in a row, stopping (re-run later to resume)')
                break
            continue

        consecutive = 0
        years += store_statements(conn, item['item_id'], annual)
        # no quarterly statements (e.g. half-year reporters) is not an error: the TTM metrics are NULL
        quarters += store_statements(conn, item['item_id'], parse_quarterly(data), 'fundamental_quarterly',
                                     'quarter_end')
        store_info(conn, item['item_id'], parse_info(data), raw_info(data))
        log_fetch(conn, 'fundamentals', item['ticker'])
        conn.commit()
        ok += 1
        if n % 50 == 0:
            logger.info(f'update fundamentals: {n}/{len(items)}')
    return {'selected': len(items), 'ok': ok, 'failed': failed, 'years': years, 'quarters': quarters}
