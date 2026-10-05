"""Command line interface: `screener <command> ...` (or `python -m screener <command> ...`)."""
import argparse
import logging
import os
import sys

from screener import db

logger = logging.getLogger('screener')


def _yahoo():
    from screener.market_data.yahoo_finance import YahooFinance
    return YahooFinance()


def cmd_import_tickers(conn, args):
    from screener.update.universe import import_ticker_file
    added, present = import_ticker_file(conn, args.file, args.source)
    print(f'{added} added, {present} already present')


def cmd_import_yahoo(conn, args):
    from screener.update.universe import import_yahoo_screener
    yahoo = _yahoo()
    for region in args.region:
        added, present = import_yahoo_screener(conn, yahoo, region, args.min_market_cap)
        print(f'{region}: {added} added, {present} already present')


def cmd_update(conn, args):
    from screener.update.fx import update_fx
    from screener.update.items import update_items
    from screener.update.prices import update_prices
    yahoo = _yahoo()
    what = ['items', 'prices', 'fx'] if args.what == 'all' else [args.what]
    if 'items' in what:
        print('items:', update_items(conn, yahoo, args.tickers, args.max_age_days))
    if 'prices' in what:
        print('prices:', update_prices(conn, yahoo, args.tickers, args.years))
    if 'fx' in what:
        print('fx:', update_fx(conn, yahoo, args.years))


def cmd_status(conn, args):
    if args.ticker:
        return _status_ticker(conn, args.ticker.upper())
    q = lambda sql: conn.execute(sql).fetchone()[0]
    print(f'items:      {q("SELECT COUNT(*) FROM item WHERE active = 1")} active, '
          f'{q("SELECT COUNT(*) FROM item WHERE info_updated_at IS NOT NULL")} with info')
    print(f'prices:     {q("SELECT COUNT(*) FROM price_daily")} rows, '
          f'{q("SELECT COUNT(DISTINCT item_id) FROM price_daily")} items, '
          f'latest {q("SELECT MAX(date) FROM price_daily")}')
    print(f'dividends:  {q("SELECT COUNT(*) FROM dividend")} rows')
    print(f'fx:         {q("SELECT COUNT(DISTINCT currency) FROM fx_rate_daily")} currencies, '
          f'latest {q("SELECT MAX(date) FROM fx_rate_daily")}')
    errors = conn.execute('SELECT kind, key, last_attempt_at, last_error FROM fetch_log '
                          'WHERE last_error IS NOT NULL ORDER BY kind, key').fetchall()
    print(f'fetch errors: {len(errors)}')
    for e in errors:
        print(f'  {e["kind"]:6} {e["key"]:15} {e["last_attempt_at"]}  {e["last_error"]}')


def _status_ticker(conn, ticker):
    item = conn.execute('SELECT * FROM item WHERE ticker = ?', (ticker,)).fetchone()
    if item is None:
        print(f'{ticker}: not in database')
        return
    for k in item.keys():
        print(f'{k:20} {item[k]}')
    p = conn.execute('SELECT COUNT(*), MIN(date), MAX(date) FROM price_daily WHERE item_id = ?',
                     (item['item_id'],)).fetchone()
    d = conn.execute('SELECT COUNT(*), MAX(ex_date) FROM dividend WHERE item_id = ?', (item['item_id'],)).fetchone()
    print(f'{"prices":20} {p[0]} rows, {p[1]} .. {p[2]}')
    print(f'{"dividends":20} {d[0]} rows, latest {d[1]}')
    for f in conn.execute('SELECT * FROM fetch_log WHERE key = ? ORDER BY kind', (ticker,)):
        print(f'{"fetch " + f["kind"]:20} attempt {f["last_attempt_at"]}, success {f["last_success_at"]}'
              + (f', error: {f["last_error"]}' if f['last_error'] else ''))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='screener', description=__doc__)
    p.add_argument('--db', default=os.environ.get('SCREENER_DB', str(db.DEFAULT_DB)),
                   help='SQLite file (default: $SCREENER_DB or data/screener.db)')
    p.add_argument('-v', '--verbose', action='store_true')
    sub = p.add_subparsers(dest='command', required=True)

    s = sub.add_parser('import-tickers', help='add tickers from a CSV/text file (first column)')
    s.add_argument('file')
    s.add_argument('--source', help='source label (default: csv:<file stem>)')
    s.set_defaults(func=cmd_import_tickers)

    s = sub.add_parser('import-yahoo', help='add equities from the Yahoo screener')
    s.add_argument('--region', action='append', required=True, help='Yahoo region code (fi, se, us, ...); repeatable')
    s.add_argument('--min-market-cap', type=float, default=5e8)
    s.set_defaults(func=cmd_import_yahoo)

    s = sub.add_parser('update', help='fetch data from Yahoo')
    s.add_argument('what', choices=['items', 'prices', 'fx', 'all'])
    s.add_argument('--tickers', nargs='+', help='only these tickers (items/prices)')
    s.add_argument('--years', type=int, default=5, help='history depth for new items/currencies (default 5)')
    s.add_argument('--max-age-days', type=float, default=7, help='refresh item info older than this (default 7)')
    s.set_defaults(func=cmd_update)

    s = sub.add_parser('status', help='data overview, or details of one ticker')
    s.add_argument('ticker', nargs='?')
    s.set_defaults(func=cmd_status)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(name)s: %(message)s', datefmt='%H:%M:%S')
    if not args.verbose:
        logging.getLogger('yfinance').setLevel(logging.CRITICAL)
    conn = db.connect(args.db)
    try:
        args.func(conn, args)
    except KeyboardInterrupt:
        print('interrupted (completed batches are saved; re-run to resume)', file=sys.stderr)
        sys.exit(130)
    finally:
        conn.close()
