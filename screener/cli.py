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
    from screener.update.fundamentals import update_fundamentals
    from screener.update.fx import update_fx
    from screener.update.items import update_items
    from screener.update.prices import update_prices
    yahoo = _yahoo()
    # fx last: it needs the financial currencies that items fetches
    what = ['items', 'prices', 'fundamentals', 'fx'] if args.what == 'all' else [args.what]
    if 'items' in what:
        print('items:', update_items(conn, yahoo, args.tickers, args.max_age_days))
    if 'prices' in what:
        print('prices:', update_prices(conn, yahoo, args.tickers, args.years))
    if 'fundamentals' in what:
        print('fundamentals:', update_fundamentals(conn, yahoo, args.tickers, args.fundamentals_max_age_days))
    if 'fx' in what:
        print('fx:', update_fx(conn, yahoo, args.years))


def cmd_metrics(conn, args):
    from screener.metrics import METRICS, calc_metrics
    if args.list:
        for name, desc in METRICS.items():
            print(f'{name:25} {desc}')
        return
    print('metrics:', calc_metrics(conn, args.asof, args.tickers))


def cmd_screen(conn, args):
    from screener.screening.engine import metric_columns, run_screen
    from screener.screening.profile import ProfileError, load_profile
    from screener.screening.report import funnel, survivors
    try:
        profile = load_profile(args.profile, metric_columns(conn))
        run_id = run_screen(conn, profile, args.asof, args.from_step, args.base_run)
    except (ProfileError, ValueError) as e:
        sys.exit(f'error: {e}')
    run = conn.execute('SELECT * FROM screen_run WHERE run_id = ?', (run_id,)).fetchone()
    print(f'run {run_id}  {profile.name}  as of {run["asof_date"]}')
    if run['parent_run_id']:
        print(f'steps 1..{args.from_step - 1} re-used from run {run["parent_run_id"]}')
    print()
    print(funnel(conn, run_id), end='\n\n')
    print(survivors(conn, run_id))
    print(f'\nwhy was X eliminated?  screener explain <ticker> --run {run_id}')


def cmd_explain(conn, args):
    from screener.screening.report import explain
    print(explain(conn, args.ticker, args.run))


def cmd_runs(conn, args):
    from screener.screening.report import list_runs
    print(list_runs(conn, args.limit))


def cmd_show_run(conn, args):
    from screener.screening.report import show_run
    print(show_run(conn, args.run, args.step))


def cmd_prune(conn, args):
    from screener.screening.engine import prune_runs
    print(f'{prune_runs(conn, args.keep, args.run)} run(s) deleted')


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
    print(f'statements: {q("SELECT COUNT(*) FROM fundamental_annual")} fiscal years, '
          f'{q("SELECT COUNT(DISTINCT item_id) FROM fundamental_annual")} items')
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
    annual = conn.execute('SELECT * FROM fundamental_annual WHERE item_id = ? ORDER BY fiscal_year_end',
                          (item['item_id'],)).fetchall()
    if annual:
        print(f'annual statements ({annual[0]["currency"]}):')
        cols = [k for k in annual[0].keys() if k not in ('item_id', 'currency')]
        for c in cols:
            print(f'  {c:24}' + ''.join(f'{_fmt(r[c]):>12}' for r in annual))
    for f in conn.execute('SELECT * FROM fetch_log WHERE key = ? ORDER BY kind', (ticker,)):
        print(f'{"fetch " + f["kind"]:20} attempt {f["last_attempt_at"]}, success {f["last_success_at"]}'
              + (f', error: {f["last_error"]}' if f['last_error'] else ''))


def _fmt(v) -> str:
    """Compact number for tables: 1.23e9 -> '1.23G'; text and None unchanged."""
    if not isinstance(v, (int, float)):
        return str(v)
    for div, suffix in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k')):
        if abs(v) >= div:
            return f'{v / div:.2f}{suffix}'
    return f'{v:.2f}'


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
    s.add_argument('what', choices=['items', 'prices', 'fundamentals', 'fx', 'all'])
    s.add_argument('--tickers', nargs='+', help='only these tickers (items/prices/fundamentals)')
    s.add_argument('--years', type=int, default=5, help='history depth for new items/currencies (default 5)')
    s.add_argument('--max-age-days', type=float, default=7, help='refresh item info older than this (default 7)')
    s.add_argument('--fundamentals-max-age-days', type=float, default=30,
                   help='refresh annual statements older than this (default 30)')
    s.set_defaults(func=cmd_update)

    s = sub.add_parser('metrics', help='calculate metrics into item_metrics (after `update prices`)')
    s.add_argument('--asof', help='YYYY-MM-DD (default: latest price date)')
    s.add_argument('--tickers', nargs='+', help='only these tickers')
    s.add_argument('--list', action='store_true', help='list metric definitions and exit')
    s.set_defaults(func=cmd_metrics)

    s = sub.add_parser('screen', help='run a screening profile')
    s.add_argument('--profile', required=True, help='YAML profile file')
    s.add_argument('--asof', help='metrics as-of date (default: latest calculated)')
    s.add_argument('--from-step', type=int, default=1, help='re-use stored results of earlier steps')
    s.add_argument('--base-run', help='run to re-use steps from (default: latest run of this profile)')
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser('explain', help='show at which step and why an item was eliminated')
    s.add_argument('ticker')
    s.add_argument('--run', default='latest', help='run id or "latest" (default)')
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser('runs', help='list screening runs')
    s.add_argument('--limit', type=int, default=20)
    s.set_defaults(func=cmd_runs)

    s = sub.add_parser('show-run', help='funnel and survivors of a run, or items eliminated at one step')
    s.add_argument('run', nargs='?', default='latest', help='run id or "latest" (default)')
    s.add_argument('--step', type=int, help='list items eliminated at this step, with reasons')
    s.set_defaults(func=cmd_show_run)

    s = sub.add_parser('prune', help='delete old screening runs')
    g = s.add_mutually_exclusive_group()
    g.add_argument('--keep', type=int, default=10, help='keep the newest N runs per profile (default 10)')
    g.add_argument('--run', type=int, help='delete just this run')
    s.set_defaults(func=cmd_prune)

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
