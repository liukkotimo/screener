"""Profile validation, step evaluation (incl. NULL handling), re-runs, explain and prune."""
import json
import sys
import types

import pytest

from screener import db
from screener.screening.engine import metric_columns, prune_runs, run_screen
from screener.screening.profile import ProfileError, parse_profile
from screener.screening.report import explain, survivor_table, survivors
from screener.sheet import export_run, run_rows

ASOF = '2026-03-31'


@pytest.fixture
def conn():
    c = db.connect(':memory:')
    items = [  # ticker, exchange, type, metrics (None = no metrics row)
        ('AAA', 'HEL', 'EQUITY', {'perf_1y': -0.5, 'perf_3y': None, 'perf_3m': 0.01, 'market_cap_eur': 2e9}),
        ('BBB', 'HEL', 'EQUITY', {'perf_1y': -0.1, 'perf_3y': None, 'perf_3m': 0.02, 'market_cap_eur': 5e9}),
        ('CCC', 'STO', 'EQUITY', {'perf_1y': -0.1, 'perf_3y': -0.2, 'perf_3m': 0.30, 'market_cap_eur': 3e9}),
        ('DDD', 'NYQ', 'ETF', None),
        ('EEE', 'LSE', 'EQUITY', {'perf_1y': -0.9, 'perf_3y': -0.9, 'perf_3m': 0.0, 'market_cap_eur': 9e9}),
        ('FFF', None, 'EQUITY', {'perf_1y': -0.9, 'perf_3y': -0.9, 'perf_3m': 0.0, 'market_cap_eur': 9e9}),
    ]
    for ticker, exchange, type_, m in items:
        cur = c.execute("INSERT INTO item (ticker, exchange, type, added_at) VALUES (?, ?, ?, 'x')",
                        (ticker, exchange, type_))
        if m is not None:
            cols = {'item_id': cur.lastrowid, 'asof_date': ASOF, 'calculated_at': 'x', **m}
            c.execute(f'INSERT INTO item_metrics ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                      list(cols.values()))
    c.commit()
    yield c
    c.close()


def profile(conn, text):
    return parse_profile(text, metric_columns(conn))


EXAMPLE = """
name: example
steps:
  - {attr: exchange, op: in, value: [HEL, STO, NYQ]}
  - {metric: market_cap_eur, op: ">", value: 1000000000}
  - {metric: perf_3m, op: between, value: [-0.05, 0.05]}
  - any_of:
      - {metric: perf_1y, op: "<", value: -0.33}
      - {metric: perf_3y, op: "<", value: -0.33}
"""


def outcomes(conn, run_id, step_no):
    rows = conn.execute('SELECT i.ticker, s.passed, s.reason, s.value_json FROM screen_step_item s '
                        'JOIN item i USING (item_id) WHERE run_id = ? AND step_no = ?', (run_id, step_no))
    return {r[0]: (r[1], r[2], json.loads(r[3])) for r in rows}


# --- profile validation --------------------------------------------------------------------------------------

@pytest.mark.parametrize('step, message', [
    ('{metric: perf_2y, op: "<", value: 0}', "unknown metric 'perf_2y'"),
    ('{attr: "exchange; DROP TABLE item", op: "==", value: x}', 'unknown attr'),
    ('{metric: perf_1y, op: like, value: 0}', "unknown op 'like'"),
    ('{metric: perf_1y, op: between, value: [0.5, 0.1]}', 'between needs'),
    ('{metric: perf_1y, op: in, value: []}', 'in needs a non-empty list'),
    ('{metric: perf_1y, op: "<", value: abc}', 'needs a number'),
    ('{metric: perf_1y, attr: type, op: "<", value: 1}', 'exactly one of'),
    ('{metric: perf_1y, op: "<", value: 1, colour: red}', 'unknown key'),
    ('{any_of: []}', 'non-empty list'),
])
def test_profile_validation_errors(conn, step, message):
    with pytest.raises(ProfileError, match=message):
        profile(conn, f'name: x\nsteps:\n  - {step}\n')


def test_profile_needs_name_and_steps(conn):
    with pytest.raises(ProfileError, match='name'):
        profile(conn, 'steps: []')


# --- evaluation ----------------------------------------------------------------------------------------------

def test_sequential_steps_and_reasons(conn):
    run_id = run_screen(conn, profile(conn, EXAMPLE))
    s1 = outcomes(conn, run_id, 1)
    assert len(s1) == 6                                       # every active item is a step-1 candidate
    assert s1['EEE'][:2] == (0, 'exchange = LSE, required in [HEL, STO, NYQ]')
    assert s1['FFF'][:2] == (0, 'missing data: exchange')

    s2 = outcomes(conn, run_id, 2)
    assert set(s2) == {'AAA', 'BBB', 'CCC', 'DDD'}            # only step-1 survivors
    assert s2['DDD'][1] == f'missing data: market_cap_eur (no metrics row as of {ASOF})'

    s3 = outcomes(conn, run_id, 3)
    assert s3['CCC'][:2] == (0, 'perf_3m = 0.3, required between [-0.05, 0.05]')

    s4 = outcomes(conn, run_id, 4)
    assert s4['AAA'][0] == 1                                  # one true condition is enough despite NULL
    assert s4['BBB'][:2] == (0, 'missing data: perf_3y')      # false + unknown -> unknown -> missing data
    assert s4['AAA'][2] == {'perf_1y': -0.5, 'perf_3y': None}

    run = conn.execute('SELECT n_start, n_survivors, asof_date FROM screen_run WHERE run_id = ?', (run_id,)).fetchone()
    assert tuple(run) == (6, 1, ASOF)


def test_any_of_all_false_and_all_of(conn):
    p = profile(conn, """
name: groups
steps:
  - any_of:
      - {metric: perf_1y, op: "<", value: -0.6}
      - {metric: perf_3y, op: "<", value: -0.6}
  - all_of:
      - {attr: type, op: "==", value: EQUITY}
      - {metric: perf_3y, op: ">", value: 0}
""")
    run_id = run_screen(conn, p)
    s1 = outcomes(conn, run_id, 1)
    assert s1['CCC'][1] == 'none of: perf_1y = -0.1, required < -0.6; perf_3y = -0.2, required < -0.6'
    assert s1['EEE'][0] == 1
    s2 = outcomes(conn, run_id, 2)
    assert s2['EEE'][1] == 'perf_3y = -0.9, required > 0'     # a false part decides all_of


def test_rerun_from_step_reuses_and_validates(conn):
    first = run_screen(conn, profile(conn, EXAMPLE))
    changed_last = EXAMPLE.replace('value: -0.33}\n      - {metric: perf_3y', 'value: -0.05}\n      - {metric: perf_3y')
    second = run_screen(conn, profile(conn, changed_last), from_step=4)
    assert outcomes(conn, second, 1) == outcomes(conn, first, 1)   # copied
    assert outcomes(conn, second, 4)['BBB'][0] == 1                 # re-evaluated with the new threshold
    parent = conn.execute('SELECT parent_run_id, rerun_from_step FROM screen_run WHERE run_id = ?', (second,)).fetchone()
    assert tuple(parent) == (first, 4)

    changed_first = EXAMPLE.replace('[HEL, STO, NYQ]', '[HEL]')
    with pytest.raises(ValueError, match='step 1 differs'):
        run_screen(conn, profile(conn, changed_first), from_step=4)
    with pytest.raises(ValueError, match='as-of'):
        run_screen(conn, profile(conn, EXAMPLE), asof='2020-01-01', from_step=2)


def test_metric_steps_need_metrics(conn):
    with pytest.raises(ValueError, match='no metrics as of'):
        run_screen(conn, profile(conn, EXAMPLE), asof='2020-01-01')


def test_explain(conn):
    run_id = run_screen(conn, profile(conn, EXAMPLE))
    assert 'eliminated at step 3: perf_3m = 0.3' in explain(conn, 'CCC', run_id)
    assert 'survived all 4 steps' in explain(conn, 'aaa')
    assert 'not in the database' in explain(conn, 'ZZZ')
    conn.execute("INSERT INTO item (ticker, added_at) VALUES ('NEW', 'x')")
    assert 'not a candidate in this run' in explain(conn, 'NEW')


def test_prune_keeps_newest_per_profile(conn):
    p = profile(conn, EXAMPLE)
    ids = [run_screen(conn, p) for _ in range(3)]
    assert prune_runs(conn, keep=1) == 2
    assert [r[0] for r in conn.execute('SELECT run_id FROM screen_run')] == [ids[-1]]
    assert conn.execute('SELECT COUNT(DISTINCT run_id) FROM screen_step_item').fetchone()[0] == 1


# --- show: columns and survivors table -------------------------------------------------------------------------

SHOW = EXAMPLE.replace('steps:', 'show: [exchange, perf_1y, ticker, perf_1y, dividend_yield]\nsteps:')


def test_show_validation(conn):
    assert profile(conn, SHOW).show == ('exchange', 'perf_1y', 'ticker', 'dividend_yield')   # deduplicated
    assert profile(conn, EXAMPLE).show == ()
    for bad, message in (('[nonsense]', "unknown attribute or metric 'nonsense'"), ('exchange', 'expected a list'),
                         ('[{metric: perf_1y}]', 'unknown attribute or metric')):
        with pytest.raises(ProfileError, match=message):
            profile(conn, EXAMPLE.replace('steps:', f'show: {bad}\nsteps:'))


def test_survivor_table_puts_show_columns_first(conn):
    plain = survivor_table(conn, run_screen(conn, profile(conn, EXAMPLE)))
    assert plain[0] == ['ticker', 'name', 'exchange', 'market_cap_eur', 'perf_3m', 'perf_1y', 'perf_3y']
    assert plain[1] == [['AAA', '', 'HEL', 2e9, 0.01, -0.5, None]]

    headers, rows = survivor_table(conn, run_screen(conn, profile(conn, SHOW)))
    # show columns (without ticker/name, deduplicated), then the remaining tested fields; no repeated columns
    assert headers == ['ticker', 'name', 'exchange', 'perf_1y', 'dividend_yield', 'market_cap_eur', 'perf_3m', 'perf_3y']
    assert rows == [['AAA', '', 'HEL', -0.5, None, 2e9, 0.01, None]]


def test_survivors_text_uses_show_columns(conn):
    text = survivors(conn, run_screen(conn, profile(conn, SHOW)))
    assert text.startswith('1 survivors:') and 'dividend_yield' in text


def test_survivor_table_without_survivors(conn):
    p = profile(conn, 'name: none\nshow: [exchange]\nsteps:\n  - {metric: perf_1y, op: "<", value: -5}\n')
    assert survivor_table(conn, run_screen(conn, p)) == (['ticker', 'name', 'exchange'], [])
    assert survivors(conn, 1) == 'no survivors'


# --- Google Sheet export ---------------------------------------------------------------------------------------

def test_run_rows_layout(conn):
    run_id = run_screen(conn, profile(conn, SHOW))
    rows, header_rows = run_rows(conn, run_id)
    assert rows[0][0].startswith(f'run {run_id}  example  as of {ASOF}')
    assert rows[1] == []
    assert header_rows == [3, 10]
    assert rows[2] == ['step', 'in', 'out', 'condition']
    assert rows[3] == [1, 6, 4, "exchange in [HEL, STO, NYQ]"]
    assert rows[4][:3] == [2, 4, 3]
    assert rows[6] == [4, 2, 1, '(perf_1y < -0.33 OR perf_3y < -0.33)']
    assert rows[7:9] == [[], ['1 survivors']]
    assert rows[9][:4] == ['ticker', 'name', 'exchange', 'perf_1y']
    assert rows[10][:4] == ['AAA', '', 'HEL', -0.5]
    assert rows[10][4] == ''                      # NULL becomes an empty cell, never 0


class FakeSheets:
    """Stands in for the gspread module: records what would be written."""
    def __init__(self, existing=()):
        self.tabs = {name: [] for name in existing}   # tab name -> log of calls
        outer = self

        class GSpreadException(Exception):
            pass

        class WorksheetNotFound(GSpreadException):
            pass

        class SpreadsheetNotFound(GSpreadException):
            pass

        class Worksheet:
            def __init__(self, name):
                self.name = name

            def clear(self):
                outer.tabs[self.name].append('clear')

            def resize(self, rows, cols):
                outer.tabs[self.name].append(('resize', rows, cols))

            def update(self, values, range_name, value_input_option):
                outer.tabs[self.name].append(('update', values, range_name, value_input_option))

            def format(self, rng, fmt):
                outer.tabs[self.name].append(('format', rng, fmt))

        class Spreadsheet:
            def worksheet(self, name):
                if name not in outer.tabs:
                    raise WorksheetNotFound(name)
                return Worksheet(name)

            def add_worksheet(self, name, rows, cols):
                outer.tabs[name] = [('add', rows, cols)]
                return Worksheet(name)

        class Client:
            def open_by_key(self, key):
                outer.opened = key
                return Spreadsheet()

            def open_by_url(self, url):
                outer.opened = url
                return Spreadsheet()

        self.module = types.ModuleType('gspread')
        self.module.WorksheetNotFound = WorksheetNotFound
        self.module.SpreadsheetNotFound = SpreadsheetNotFound
        self.module.exceptions = types.SimpleNamespace(GSpreadException=GSpreadException)
        self.module.service_account = lambda **kw: (setattr(outer, 'auth', kw), Client())[1]
        self.utils = types.ModuleType('gspread.utils')
        self.utils.rowcol_to_a1 = lambda r, c: f'{"ABCDEFGHIJ"[c - 1]}{r}'
        self.module.utils = self.utils

    def install(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'gspread', self.module)
        monkeypatch.setitem(sys.modules, 'gspread.utils', self.utils)


def test_export_run_creates_and_overwrites_tab(conn, monkeypatch):
    run_id = run_screen(conn, profile(conn, SHOW))
    fake = FakeSheets()
    fake.install(monkeypatch)
    assert export_run(conn, run_id, 'SHEETID', credentials='key.json') == f'example run {run_id}'
    assert fake.opened == 'SHEETID' and fake.auth['filename'] == 'key.json'
    log = fake.tabs[f'example run {run_id}']
    rows, _ = run_rows(conn, run_id)
    assert log[0] == ('add', len(rows), 8)
    assert log[1] == ('update', rows, 'A1', 'RAW')
    assert [c[1] for c in log if c[0] == 'format'] == ['A3:H3', 'A10:H10']

    export_run(conn, run_id, 'https://docs.google.com/spreadsheets/d/x/edit', tab='mine')   # new tab, URL accepted
    assert fake.opened.startswith('https://')
    again = FakeSheets(existing=['mine'])
    again.install(monkeypatch)
    export_run(conn, run_id, 'SHEETID', tab='mine')
    assert again.tabs['mine'][:2] == ['clear', ('resize', len(rows), 8)]   # existing tab is cleared, then rewritten


def test_export_run_errors(conn, monkeypatch):
    run_id = run_screen(conn, profile(conn, EXAMPLE))
    monkeypatch.setitem(sys.modules, 'gspread', None)           # import fails
    with pytest.raises(ValueError, match='pip install gspread'):
        export_run(conn, run_id, 'SHEETID')
    fake = FakeSheets()
    fake.install(monkeypatch)
    fake.module.service_account = lambda **kw: (_ for _ in ()).throw(FileNotFoundError(2, 'no', 'key.json'))
    with pytest.raises(ValueError, match='credentials file not found: key.json'):
        export_run(conn, run_id, 'SHEETID', credentials='key.json')


def test_load_dotenv(tmp_path, monkeypatch):
    from screener.cli import load_dotenv
    env = tmp_path / '.env'
    env.write_text('# comment\nSCREENER_SHEET="abc"\nexport SCREENER_DB=x.db\n\nSCREENER_TEST_KEEP=new\n')
    monkeypatch.delenv('SCREENER_SHEET', raising=False)
    monkeypatch.delenv('SCREENER_DB', raising=False)
    monkeypatch.setenv('SCREENER_TEST_KEEP', 'old')
    load_dotenv(env)
    import os
    assert os.environ['SCREENER_SHEET'] == 'abc'
    assert os.environ['SCREENER_DB'] == 'x.db'
    assert os.environ['SCREENER_TEST_KEEP'] == 'old'
