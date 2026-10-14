"""
Updating item base info (name, type, exchange, currency, ISIN, market cap) from EODHD's exchange lists.

Per exchange of the selected items: the symbol list (1 call) and the last-day bulk prices with market caps
(100 calls), so refreshing every item costs 101 calls per exchange, independent of the number of items.
Sector, industry and financial currency come with the fundamentals (update/fundamentals.py).
"""
import logging
import sqlite3

from screener.db import log_fetch, now_utc

logger = logging.getLogger(__name__)


def exchange_of(ticker: str) -> str:
    """'VOLV-B.ST' -> 'ST' (EODHD symbols are CODE.EXCHANGE)."""
    return ticker.rsplit('.', 1)[1] if '.' in ticker else ''


def code_of(ticker: str) -> str:
    """'VOLV-B.ST' -> 'VOLV-B'."""
    return ticker.rsplit('.', 1)[0]


def symbol_columns(symbol: dict | None, bulk: dict | None, exchange: dict) -> dict:
    """Item columns from a symbol list row and a bulk row (either may be None). Missing values are None."""
    symbol, bulk = symbol or {}, bulk or {}
    cap = bulk.get('MarketCapitalization')
    return {
        'name': symbol.get('Name') or bulk.get('name') or None,
        'type': symbol.get('Type') or bulk.get('type') or None,
        'exchange': exchange['Code'],
        'exchange_name': exchange.get('Name'),
        'country': symbol.get('Country') or None,
        'currency': symbol.get('Currency') or None,
        'isin': symbol.get('Isin') or None,
        'market_cap': float(cap) if cap else None,  # quote currency; major unit
        'market_cap_date': bulk.get('date') if cap else None,
    }


def select_items(conn: sqlite3.Connection, tickers: list[str] | None) -> list[sqlite3.Row]:
    if tickers:
        marks = ','.join('?' * len(tickers))
        return conn.execute(f'SELECT item_id, ticker FROM item WHERE ticker IN ({marks}) ORDER BY ticker',
                            [t.upper() for t in tickers]).fetchall()
    return conn.execute('SELECT item_id, ticker FROM item WHERE active = 1 ORDER BY ticker').fetchall()


def update_items(conn: sqlite3.Connection, client, tickers: list[str] | None = None) -> dict:
    """Refresh the selected items (default: all active) from their exchanges' symbol lists and bulk prices."""
    items = select_items(conn, tickers)
    by_exchange = {}
    for item in items:
        by_exchange.setdefault(exchange_of(item['ticker']), []).append(item)
    exchanges = {e['Code']: e for e in client.get_exchanges()} if items else {}
    logger.info(f'update items: {len(items)} items on {len(by_exchange)} exchange(s): {", ".join(by_exchange)}')

    stats = {'selected': len(items), 'exchanges': len(by_exchange), 'ok': 0, 'failed': 0}
    for code, group in sorted(by_exchange.items()):
        exchange = exchanges.get(code)
        if exchange is None:
            symbols = bulk = {}
        else:
            symbols = {s['Code'].upper(): s for s in client.get_exchange_symbols(code)}
            bulk = {b['code'].upper(): b for b in client.get_bulk_last_day(code, extended=True)}
        for item in group:
            c = code_of(item['ticker'])
            if c not in symbols and c not in bulk:
                stats['failed'] += 1
                error = (f'not in the EODHD symbol list of {code} (delisted or wrong symbol?)' if exchange
                         else f'unknown EODHD exchange code "{code}"')
                logger.warning(f'{item["ticker"]}: {error}')
                log_fetch(conn, 'info', item['ticker'], error)
                continue
            cols = symbol_columns(symbols.get(c), bulk.get(c), exchange)
            # COALESCE keeps a known value when EODHD omits a field (e.g. no market cap on a quiet day).
            sets = ', '.join(f'{k} = COALESCE(?, {k})' for k in cols)
            conn.execute(f'UPDATE item SET {sets}, info_updated_at = ? WHERE item_id = ?',
                         [*cols.values(), now_utc(), item['item_id']])
            log_fetch(conn, 'info', item['ticker'])
            stats['ok'] += 1
        conn.commit()
    return stats
