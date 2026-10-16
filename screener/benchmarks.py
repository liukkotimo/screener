"""
Benchmarks for relative strength: which series an item is compared with, and keeping their prices stored.

The mapping lives in benchmarks.yaml next to this file (or the file in $SCREENER_BENCHMARKS). Benchmarks are
stored as ordinary items with active = 0 and source 'benchmark', so price updates and price_daily work for
them unchanged while screens (active items only) never see them.
"""
import os
import sqlite3
from pathlib import Path

import yaml

from screener.db import now_utc

DEFAULT_PATH = Path(__file__).parent / 'benchmarks.yaml'
SOURCE = 'benchmark'


def load_config(path: str | Path | None = None) -> dict:
    """{'benchmarks': {ticker: {currency, name, type}}, 'market': {exchange: ticker}, 'world': ticker or None}."""
    path = Path(path or os.environ.get('SCREENER_BENCHMARKS') or DEFAULT_PATH)
    cfg = yaml.safe_load(path.read_text()) or {}
    benchmarks = {t.upper(): v or {} for t, v in (cfg.get('benchmarks') or {}).items()}
    market = {e.upper(): t.upper() for e, t in (cfg.get('market') or {}).items()}
    world = cfg.get('world').upper() if cfg.get('world') else None
    unknown = {*market.values(), world} - {None, *benchmarks}
    if unknown:
        raise SystemExit(f'{path}: benchmark(s) used but not listed under benchmarks: {", ".join(sorted(unknown))}')
    return {'benchmarks': benchmarks, 'market': market, 'world': world}


def market_benchmark(item: dict, config: dict) -> str | None:
    """The home-market benchmark of an item, by its listing exchange (the ticker suffix); None if not mapped."""
    exchange = item.get('exchange') or (item['ticker'].rsplit('.', 1)[1] if '.' in item['ticker'] else '')
    return config['market'].get(exchange.upper())


def ensure_items(conn: sqlite3.Connection, config: dict) -> int:
    """Insert the configured benchmarks that are not items yet (inactive). Returns the number added."""
    added = 0
    for ticker, b in config['benchmarks'].items():
        if conn.execute('SELECT 1 FROM item WHERE ticker = ?', (ticker,)).fetchone():
            continue
        conn.execute('INSERT INTO item (ticker, name, type, exchange, currency, source, active, added_at) '
                     'VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
                     (ticker, b.get('name'), b.get('type', 'INDEX'), ticker.rsplit('.', 1)[-1], b.get('currency'),
                      SOURCE, now_utc()))
        added += 1
    conn.commit()
    return added
