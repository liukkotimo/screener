"""Adding tickers to the item table: from a CSV/text file or from the Yahoo equity screener."""
import logging
import sqlite3
from datetime import date
from pathlib import Path

from screener.db import now_utc

logger = logging.getLogger(__name__)

# Yahoo exchange codes of the US over-the-counter markets (OTC Pink, OTCQX, OTCQB). Not imported: the same
# companies are listed on their home exchange, and OTC lines only slow down the updates.
EXCLUDED_EXCHANGES = {'PNK', 'OQX', 'OQB'}


def read_ticker_file(path: str | Path) -> list[str]:
    """
    One ticker per line; the first comma/semicolon/whitespace separated field is used.
    Blank lines, '#' comments and a 'ticker'/'symbol' header line are ignored.
    """
    tickers = []
    for line in Path(path).read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        ticker = line.replace(';', ',').split(',')[0].split()[0].strip().upper()
        if ticker in ('TICKER', 'SYMBOL'):
            continue
        tickers.append(ticker)
    return list(dict.fromkeys(tickers))


def add_items(conn: sqlite3.Connection, rows: list[dict], source: str) -> tuple[int, int]:
    """
    Insert items that do not exist yet; existing items (same ticker) are left untouched.
    rows: dicts with 'ticker' and optional item columns (name, type, exchange, ...).
    Returns (added, already_present).
    """
    allowed = {'name', 'type', 'exchange', 'exchange_name', 'currency', 'market_cap', 'market_cap_date'}
    added = 0
    for row in rows:
        cols = {k: v for k, v in row.items() if k in allowed and v is not None}
        cols.update(ticker=row['ticker'], source=source, added_at=now_utc())
        cur = conn.execute(
            f'INSERT OR IGNORE INTO item ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
            list(cols.values()))
        added += cur.rowcount
    conn.commit()
    return added, len(rows) - added


def import_ticker_file(conn: sqlite3.Connection, path: str | Path, source: str | None = None) -> tuple[int, int]:
    tickers = read_ticker_file(path)
    return add_items(conn, [{'ticker': t} for t in tickers], source or f'csv:{Path(path).stem}')


def import_yahoo_screener(conn: sqlite3.Connection, yahoo, region: str, min_market_cap: float) -> tuple[int, int]:
    """Add all equities the Yahoo screener returns for a region (base info comes along for free)."""
    quotes = yahoo.screen_equities(region, min_market_cap=min_market_cap)
    today = date.today().isoformat()
    rows = [{
        'ticker': q['symbol'],
        'name': q.get('longName') or q.get('shortName'),
        'type': q.get('quoteType'),
        'exchange': q.get('exchange'),
        'exchange_name': q.get('fullExchangeName'),
        'currency': q.get('currency'),
        'market_cap': q.get('marketCap'),
        'market_cap_date': today if q.get('marketCap') else None,
    } for q in quotes if q.get('symbol') and q.get('exchange') not in EXCLUDED_EXCHANGES]
    logger.info(f'{region}: skipped {len(quotes) - len(rows)} quotes (OTC or no symbol)')
    return add_items(conn, rows, f'yahoo_screener:{region}')
