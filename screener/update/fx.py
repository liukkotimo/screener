"""Incremental update of daily FX rates (EUR per unit of each major currency used by active items)."""
import logging
import sqlite3
from datetime import date, timedelta

import pandas as pd

from screener.db import log_fetch
from screener.market_data.yahoo_finance import major_currency

logger = logging.getLogger(__name__)

OVERLAP_DAYS = 5


def needed_currencies(conn: sqlite3.Connection) -> list[str]:
    """Major quote and financial-statement currencies of active items, EUR excluded (GBp -> GBP)."""
    rows = conn.execute('SELECT currency FROM item WHERE active = 1 AND currency IS NOT NULL UNION '
                        'SELECT financial_currency FROM item WHERE active = 1 AND financial_currency IS NOT NULL')
    return sorted({major_currency(r[0])[0] for r in rows} - {'EUR'})


def update_fx(conn: sqlite3.Connection, yahoo, years: int = 5) -> dict:
    currencies = needed_currencies(conn)
    last = dict(conn.execute('SELECT currency, MAX(date) FROM fx_rate_daily GROUP BY currency').fetchall())
    full_start = (pd.Timestamp.today().normalize() - pd.DateOffset(years=years)).date()
    # Two groups: currencies without history get the full range, others re-fetch a short overlap.
    groups = {
        full_start: [c for c in currencies if c not in last],
        None: [c for c in currencies if c in last],
    }
    stats = {'currencies': len(currencies), 'ok': 0, 'failed': 0, 'rows': 0}
    for start, group in groups.items():
        if not group:
            continue
        if start is None:
            start = date.fromisoformat(min(last[c] for c in group)) - timedelta(days=OVERLAP_DAYS)
        rates = yahoo.get_fx_history(group, start)
        for c in group:
            s = rates.get(c)
            if s is None or s.dropna().empty:
                stats['failed'] += 1
                logger.warning(f'{c}: no FX data returned (pair EUR{c}=X)')
                log_fetch(conn, 'fx', c, f'no data for EUR{c}=X')
                continue
            rows = [(c, d.strftime('%Y-%m-%d'), float(v)) for d, v in s.dropna().items()]
            conn.executemany('INSERT OR REPLACE INTO fx_rate_daily VALUES (?, ?, ?)', rows)
            log_fetch(conn, 'fx', c)
            stats['ok'] += 1
            stats['rows'] += len(rows)
        conn.commit()
    return stats
