"""Adding tickers to the item table: from a CSV/text file or from an EODHD exchange symbol list."""
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from screener.db import now_utc
from screener.market_data.eodhd import major_currency

logger = logging.getLogger(__name__)

# EODHD types imported by import-exchange. ETFs and funds can still be added with import-tickers.
IMPORT_TYPES = {'Common Stock'}
# EODHD exchange values of the US over-the-counter markets. Not imported: the same companies are listed on
# their home exchange, and OTC lines only slow down the updates. (Only the US symbol list contains them.)
EXCLUDED_EXCHANGES = {'PINK', 'OTCQB', 'OTCQX', 'OTCGREY', 'OTCCE', 'OTCMKTS', 'OTC', 'OTCBB', 'OTCPK'}


def read_ticker_file(path: str | Path) -> list[str]:
    """
    One ticker (EODHD symbol, e.g. NOKIA.HE) per line; the first comma/semicolon/whitespace separated field
    is used. Blank lines, '#' comments and a 'ticker'/'symbol' header line are ignored.
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
    allowed = {'name', 'type', 'exchange', 'exchange_name', 'country', 'currency', 'isin', 'market_cap',
               'market_cap_date'}
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


def latest_eur_per_unit(client, currency: str) -> float:
    """EUR per unit of a major currency, from the last ~10 days of EODHD FX (1 call; EUR needs none)."""
    if currency == 'EUR':
        return 1.0
    rates = client.get_fx_history([currency], date.today() - timedelta(days=10)).get(currency)
    if rates is None or rates.empty:
        raise SystemExit(f'no FX rate for {currency}: cannot apply the EUR market cap minimum')
    return float(rates.iloc[-1])


def exchange_rows(symbols: list[dict], bulk: list[dict], exchange: dict, eur_per_unit: float,
                  min_market_cap_eur: float) -> tuple[list[dict], dict]:
    """
    Pure selection for import_exchange: item rows for symbols that are of an IMPORT_TYPES type, not OTC,
    quoted in the exchange currency and have a bulk market cap of at least min_market_cap_eur.
    The bulk market cap is in the (major) quote currency; eur_per_unit converts it.
    Returns (rows, {reason: count of skipped symbols}).
    """
    caps = {b['code']: b for b in bulk}
    rows, skipped = [], {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for s in symbols:
        if s.get('Type') not in IMPORT_TYPES:
            skip(f'type {s.get("Type")}')
        elif s.get('Exchange') in EXCLUDED_EXCHANGES:
            skip('OTC')
        elif major_currency(s.get('Currency'))[0] != major_currency(exchange['Currency'])[0]:
            skip('other currency')  # e.g. EUR lines on Stockholm; LSE's GBX equals its GBP
        elif not (caps.get(s['Code']) or {}).get('MarketCapitalization'):
            skip('no market cap')
        elif float(caps[s['Code']]['MarketCapitalization']) * eur_per_unit < min_market_cap_eur:
            skip('market cap below minimum')
        else:
            b = caps[s['Code']]
            rows.append({
                'ticker': f'{s["Code"]}.{exchange["Code"]}'.upper(),
                'name': s.get('Name'),
                'type': s['Type'],
                'exchange': exchange['Code'],
                'exchange_name': exchange.get('Name'),
                'country': s.get('Country'),
                'currency': s.get('Currency'),
                'isin': s.get('Isin'),
                'market_cap': float(b['MarketCapitalization']),
                'market_cap_date': b.get('date'),
            })
    return rows, skipped


def import_exchange(conn: sqlite3.Connection, client, exchange_code: str,
                    min_market_cap_eur: float) -> tuple[int, int, dict]:
    """
    Add an exchange's listed equities with a market cap of at least min_market_cap_eur (EUR).
    Calls: exchanges list 1 + symbol list 1 + bulk last day 100 + FX 1.
    Returns (added, already_present, {reason: skipped count}).
    """
    exchange = next((e for e in client.get_exchanges() if e['Code'] == exchange_code), None)
    if exchange is None:
        raise SystemExit(f'unknown EODHD exchange code: {exchange_code}')
    major, _ = major_currency(exchange['Currency'])
    eur_per_unit = latest_eur_per_unit(client, major)
    symbols = client.get_exchange_symbols(exchange_code)
    bulk = client.get_bulk_last_day(exchange_code, extended=True)
    rows, skipped = exchange_rows(symbols, bulk, exchange, eur_per_unit, min_market_cap_eur)
    logger.info(f'{exchange_code}: {len(symbols)} symbols, {len(rows)} selected, skipped {skipped}')
    added, present = add_items(conn, rows, f'eodhd:{exchange_code}')
    return added, present, skipped
