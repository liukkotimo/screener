"""
Throwaway Phase 0 spike: fetch raw EODHD responses into tests/fixtures/eodhd/ to answer the open questions
of the Yahoo -> EODHD migration. Not part of the package.

    venv/bin/python scripts/spike_eodhd.py            # everything
    venv/bin/python scripts/spike_eodhd.py eod div    # only some groups

The API key comes from $EODHD_API_KEY or .env and is never printed or written to a file.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'tests' / 'fixtures' / 'eodhd'
BASE = 'https://eodhd.com/api'

# universe/test20.csv converted to EODHD symbols (Yahoo suffix -> EODHD exchange code)
TEST20 = ['NOKIA.HE', 'SAMPO.HE', 'KNEBV.HE', 'VOLV-B.ST', 'ERIC-B.ST', 'NOVO-B.CO', 'AAPL.US', 'MSFT.US',
          'JNJ.US', 'SAP.XETRA', 'ASML.AS', 'MC.PA', 'NESN.SW', 'SHEL.LSE', 'ULVR.LSE', '7203.TSE', 'SPY.US',
          'IWDA.AS', 'VWCE.XETRA', 'VFIAX.US', 'NOSUCH.XX']
EXTRA = ['UPM.HE', 'NDA-FI.HE', 'INVE-B.ST', 'ATCO-A.ST', 'NVDA.US']  # NVDA: 10:1 split 2024-06-10
ALL = TEST20 + EXTRA


def api_key() -> str:
    key = os.environ.get('EODHD_API_KEY')
    if not key and (ROOT / '.env').exists():
        for line in (ROOT / '.env').read_text().splitlines():
            if line.strip().startswith('EODHD_API_KEY='):
                key = line.split('=', 1)[1].strip().strip('"\'')
    if not key:
        raise SystemExit('EODHD_API_KEY not set')
    return key


KEY = api_key()


def safe_name(s: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '_', s)


def get(path: str, name: str, **params):
    """GET BASE/path, save the body (JSON pretty-printed if possible) as OUT/name. Returns parsed JSON or None."""
    params = {'api_token': KEY, 'fmt': 'json', **params}
    t0 = time.monotonic()
    r = requests.get(f'{BASE}/{path}', params=params, timeout=60)
    dt = time.monotonic() - t0
    limit_headers = {k: v for k, v in r.headers.items() if 'ratelimit' in k.lower()}
    print(f'{r.status_code} {path} {({k: v for k, v in params.items() if k != "api_token"})} '
          f'{len(r.content)}B {dt:.1f}s {limit_headers}')
    try:
        data = r.json()
        if path == 'user':  # personal data must not end up in fixtures
            data = {k: ('<redacted>' if k in ('name', 'email', 'inviteToken') else v) for k, v in data.items()}
        text = json.dumps(data, indent=1, ensure_ascii=False)
    except ValueError:
        data, text = None, r.text
    (OUT / name).write_text(text.replace(KEY, '<api_token>'))
    return data


def group_user():
    get('user', 'user.json')


def group_exchanges():
    get('exchanges-list/', 'exchanges-list.json')
    for ex in ('HE', 'ST'):
        get(f'exchange-symbol-list/{ex}', f'exchange-symbol-list_{ex}.json')
        get(f'exchange-symbol-list/{ex}', f'exchange-symbol-list_{ex}_delisted.json', delisted=1)
        get(f'exchange-details/{ex}', f'exchange-details_{ex}.json')


def group_bulk():
    for ex in ('HE', 'ST'):
        get(f'eod-bulk-last-day/{ex}', f'eod-bulk-last-day_{ex}_extended.json', filter='extended')
        get(f'eod-bulk-last-day/{ex}', f'eod-bulk-last-day_{ex}_dividends.json', type='dividends')
        get(f'eod-bulk-last-day/{ex}', f'eod-bulk-last-day_{ex}_splits.json', type='splits')


def group_eod():
    for s in ALL:
        get(f'eod/{s}', f'eod_{safe_name(s)}.json', **{'from': '2021-01-01'})


def group_div():
    for s in ALL:
        get(f'div/{s}', f'div_{safe_name(s)}.json', **{'from': '2015-01-01'})
        get(f'splits/{s}', f'splits_{safe_name(s)}.json', **{'from': '2000-01-01'})


def group_fund():
    for s in ALL:
        get(f'fundamentals/{s}', f'fundamentals_{safe_name(s)}.json')
    # the filtered shape the client uses (flattened 'Section::Sub' keys), for tests/test_eodhd.py
    from screener.market_data.eodhd import FUNDAMENTALS_FILTER
    for s in ('NOKIA.HE', 'SPY.US'):
        get(f'fundamentals/{s}', f'fundamentals_{safe_name(s)}_filter.json', filter=FUNDAMENTALS_FILTER)


def group_fx():
    for pair in ('EURSEK', 'EURUSD', 'EURGBP', 'SEKEUR', 'EURDKK'):
        get(f'eod/{pair}.FOREX', f'eod_{pair}.FOREX.json', **{'from': '1990-01-01'})


def group_ratelimit():
    """Probe: a burst of cheap calls to see headers; then /user again to see call accounting."""
    for _ in range(5):
        get('eod/NOKIA.HE', 'probe_eod_NOKIA.HE_last.json', **{'from': '2026-10-01'})
    get('user', 'user_after.json')


GROUPS = {'user': group_user, 'exchanges': group_exchanges, 'bulk': group_bulk, 'eod': group_eod,
          'div': group_div, 'fund': group_fund, 'fx': group_fx, 'ratelimit': group_ratelimit}

if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    for g in sys.argv[1:] or [*GROUPS, 'user']:
        GROUPS[g]()
