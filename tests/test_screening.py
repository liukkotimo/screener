"""Profile validation, step evaluation (incl. NULL handling), re-runs, explain and prune."""
import json

import pytest

from screener import db
from screener.screening.engine import metric_columns, prune_runs, run_screen
from screener.screening.profile import ProfileError, parse_profile
from screener.screening.report import explain

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
