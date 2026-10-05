"""
Screening engine: runs profile steps strictly in sequence and stores every candidate's outcome.

Each step is one parameterized SQL query over the survivors of the previous step (step 1: all active items).
Per candidate it returns the tested values, the result of every single condition and of the whole step.
Results follow SQL three-valued logic: a condition on a NULL value is unknown, unknown conditions in
any_of/all_of only decide the outcome when the known ones do not, and an unknown step result fails
the item with reason "missing data".
"""
import json
import logging
import sqlite3

from screener.db import now_utc
from screener.screening.profile import Group, Leaf, Profile, Step, format_value

logger = logging.getLogger(__name__)

NON_METRIC_COLUMNS = {'item_id', 'asof_date', 'calculated_at'}


def metric_columns(conn: sqlite3.Connection) -> set[str]:
    """Allowed metric names = item_metrics columns (so new metrics need no code change here)."""
    return {r['name'] for r in conn.execute('PRAGMA table_info(item_metrics)')} - NON_METRIC_COLUMNS


def latest_asof(conn: sqlite3.Connection) -> str | None:
    return conn.execute('SELECT MAX(asof_date) FROM item_metrics').fetchone()[0]


def _leaf_reason(leaf: Leaf, value, result, has_metrics: bool, asof) -> str:
    if result is None:
        if leaf.source == 'metric' and not has_metrics:
            return f'missing data: {leaf.field} (no metrics row as of {asof})'
        return f'missing data: {leaf.field}'
    return f'{leaf.field} = {format_value(value)}, required {leaf.op} {format_value(leaf.value)}'


def evaluate_step(conn: sqlite3.Connection, step: Step, run_id: int, asof: str | None) -> tuple[int, int]:
    """Evaluate one step for the survivors of step_no - 1 and store a screen_step_item row for each."""
    leaves = step.condition.leaves()
    fields = list(dict.fromkeys((leaf.source, leaf.field) for leaf in leaves))
    field_sql = [f'{"i" if s == "attr" else "m"}.{f}' for s, f in fields]   # whitelisted names only
    leaf_sql = [leaf.sql() for leaf in leaves]
    result_sql, result_params = step.condition.sql()

    if step.no == 1:
        where, where_params = 'i.active = 1', []
    else:
        where = ('i.item_id IN (SELECT item_id FROM screen_step_item '
                 'WHERE run_id = ? AND step_no = ? AND passed = 1)')
        where_params = [run_id, step.no - 1]

    sql = (f'SELECT i.item_id, m.item_id IS NOT NULL AS has_metrics, '
           f'{", ".join(field_sql)}, {", ".join(s for s, _ in leaf_sql)}, {result_sql} AS result '
           f'FROM item i LEFT JOIN item_metrics m ON m.item_id = i.item_id AND m.asof_date = ? '
           f'WHERE {where}')
    params = [p for _, ps in leaf_sql for p in ps] + result_params + [asof] + where_params

    rows = []
    for r in conn.execute(sql, params):
        values = list(r)[2:2 + len(fields)]
        leaf_results = list(r)[2 + len(fields):2 + len(fields) + len(leaves)]
        result = r['result']
        value_by_field = {f: v for (_, f), v in zip(fields, values)}
        if result == 1:
            reason = None
        else:
            reasons = [_leaf_reason(leaf, value_by_field[leaf.field], lr, r['has_metrics'], asof)
                       for leaf, lr in zip(leaves, leaf_results) if lr != 1]
            if result is None:  # only the unknown parts are relevant
                reasons = [x for x in reasons if x.startswith('missing data')]
            any_of_failed = isinstance(step.condition, Group) and step.condition.kind == 'any_of' and result == 0
            prefix = 'none of: ' if any_of_failed else ''
            reason = prefix + '; '.join(dict.fromkeys(reasons))
        rows.append((run_id, step.no, r['item_id'], json.dumps(value_by_field), 1 if result == 1 else 0, reason))

    n_passed = sum(row[4] for row in rows)
    conn.execute('INSERT INTO screen_step VALUES (?, ?, ?, ?, ?, ?)',
                 (run_id, step.no, step.definition_json, step.describe(), len(rows), n_passed))
    conn.executemany('INSERT INTO screen_step_item VALUES (?, ?, ?, ?, ?, ?)', rows)
    return len(rows), n_passed


def find_run(conn: sqlite3.Connection, run: str | int | None = 'latest', name: str | None = None) -> sqlite3.Row | None:
    """A run by id, or the latest (optionally the latest of one profile name)."""
    if run in (None, 'latest'):
        sql, params = 'SELECT * FROM screen_run', []
        if name:
            sql, params = sql + ' WHERE name = ?', [name]
        return conn.execute(sql + ' ORDER BY run_id DESC LIMIT 1', params).fetchone()
    return conn.execute('SELECT * FROM screen_run WHERE run_id = ?', (int(run),)).fetchone()


def run_screen(conn: sqlite3.Connection, profile: Profile, asof: str | None = None,
               from_step: int = 1, base_run: str | int | None = None) -> int:
    """
    Run a profile and return the new run_id.

    from_step > 1 re-uses the stored results of steps 1..from_step-1 from base_run (default: the latest run of
    the same profile name). Allowed only if those steps are defined identically and the as-of date matches.
    """
    if not 1 <= from_step <= len(profile.steps):
        raise ValueError(f'--from-step must be between 1 and {len(profile.steps)}')
    parent = None
    if from_step > 1:
        parent = find_run(conn, base_run or 'latest', None if base_run else profile.name)
        if parent is None:
            raise ValueError(f'no earlier run of profile {profile.name!r} to re-use')
        asof = asof or parent['asof_date']
        if asof != parent['asof_date']:
            raise ValueError(f'run {parent["run_id"]} used as-of {parent["asof_date"]}, not {asof}')
        stored = {r['step_no']: r['definition_json'] for r in
                  conn.execute('SELECT step_no, definition_json FROM screen_step WHERE run_id = ?', (parent['run_id'],))}
        for step in profile.steps[:from_step - 1]:
            if stored.get(step.no) != step.definition_json:
                raise ValueError(f'step {step.no} differs from run {parent["run_id"]}; '
                                 f're-run from step {step.no} or earlier')

    uses_metrics = any(leaf.source == 'metric' for s in profile.steps for leaf in s.condition.leaves())
    asof = asof or latest_asof(conn)
    if uses_metrics and asof is None:
        raise ValueError('no metrics calculated yet: run `screener metrics` first')
    if uses_metrics and not conn.execute('SELECT 1 FROM item_metrics WHERE asof_date = ? LIMIT 1', (asof,)).fetchone():
        raise ValueError(f'no metrics as of {asof}: run `screener metrics --asof {asof}`')

    cur = conn.execute(
        'INSERT INTO screen_run (name, created_at, asof_date, profile_path, profile_yaml, parent_run_id, rerun_from_step) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (profile.name, now_utc(), asof, profile.path, profile.yaml_text,
         parent['run_id'] if parent else None, from_step if parent else None))
    run_id = cur.lastrowid

    if parent:
        conn.execute('INSERT INTO screen_step SELECT ?, step_no, definition_json, description, n_in, n_passed '
                     'FROM screen_step WHERE run_id = ? AND step_no < ?', (run_id, parent['run_id'], from_step))
        conn.execute('INSERT INTO screen_step_item SELECT ?, step_no, item_id, value_json, passed, reason '
                     'FROM screen_step_item WHERE run_id = ? AND step_no < ?', (run_id, parent['run_id'], from_step))

    for step in profile.steps[from_step - 1:]:
        n_in, n_passed = evaluate_step(conn, step, run_id, asof)
        logger.info(f'step {step.no}: {n_in} -> {n_passed}  {step.describe()}')

    n_start = conn.execute('SELECT n_in FROM screen_step WHERE run_id = ? AND step_no = 1', (run_id,)).fetchone()[0]
    n_survivors = conn.execute('SELECT n_passed FROM screen_step WHERE run_id = ? AND step_no = ?',
                               (run_id, len(profile.steps))).fetchone()[0]
    conn.execute('UPDATE screen_run SET n_start = ?, n_survivors = ? WHERE run_id = ?', (n_start, n_survivors, run_id))
    conn.commit()
    return run_id


def prune_runs(conn: sqlite3.Connection, keep: int = 10, run_id: int | None = None) -> int:
    """Delete one run, or all but the newest `keep` runs of each profile name. Returns the number deleted."""
    if run_id is not None:
        ids = [run_id]
    else:
        ids = [r[0] for r in conn.execute(
            'SELECT run_id FROM (SELECT run_id, ROW_NUMBER() OVER (PARTITION BY name ORDER BY run_id DESC) AS rn '
            'FROM screen_run) WHERE rn > ?', (keep,))]
    for i in ids:
        conn.execute('DELETE FROM screen_step_item WHERE run_id = ?', (i,))
        conn.execute('DELETE FROM screen_step WHERE run_id = ?', (i,))
        conn.execute('DELETE FROM screen_run WHERE run_id = ?', (i,))
    conn.commit()
    return len(ids)
