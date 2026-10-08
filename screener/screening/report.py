"""Printing screening results: funnel, survivors, eliminated items and per-item explanations."""
import json
import sqlite3

from screener.screening.engine import find_run, metric_columns
from screener.screening.profile import ProfileError, format_value, is_attribute, parse_profile


def _run_header(run) -> str:
    text = f'run {run["run_id"]}  {run["name"]}  as of {run["asof_date"]}  (created {run["created_at"]})'
    if run['parent_run_id']:
        text += f'\n  steps 1..{run["rerun_from_step"] - 1} re-used from run {run["parent_run_id"]}'
    return text


def _table(rows: list[list], headers: list[str]) -> str:
    cells = [headers] + [[format_value(c) if not isinstance(c, str) else c for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    return '\n'.join('  '.join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in cells)


def funnel(conn: sqlite3.Connection, run_id: int) -> str:
    steps = conn.execute('SELECT * FROM screen_step WHERE run_id = ? ORDER BY step_no', (run_id,)).fetchall()
    return _table([[f'step {s["step_no"]}', str(s['n_in']), '->', str(s['n_passed']), s['description']] for s in steps],
                  ['', 'in', '', 'passed', 'condition'])


def _merged_values(conn, run_id, item_ids) -> dict[int, dict]:
    values = {i: {} for i in item_ids}
    for r in conn.execute('SELECT item_id, value_json FROM screen_step_item WHERE run_id = ? ORDER BY step_no', (run_id,)):
        if r['item_id'] in values:
            values[r['item_id']].update(json.loads(r['value_json'] or '{}'))
    return values


def _show_fields(conn: sqlite3.Connection, run) -> list[str]:
    """The profile's `show:` columns, read from the profile text stored with the run (none for old runs)."""
    try:
        show = parse_profile(run['profile_yaml'], metric_columns(conn)).show
    except ProfileError:
        return []
    return [f for f in show if f not in ('ticker', 'name')]


def survivor_table(conn: sqlite3.Connection, run_id: int, name_width: int | None = None):
    """
    Survivors of the last step as (headers, rows): ticker, name, the profile's `show:` columns, then every
    other field tested in any step. `show:` values are current item attributes / metrics of the run's as-of
    date; tested values are the ones stored when the run was made.
    """
    run = conn.execute('SELECT * FROM screen_run WHERE run_id = ?', (run_id,)).fetchone()
    last = conn.execute('SELECT MAX(step_no) FROM screen_step WHERE run_id = ?', (run_id,)).fetchone()[0]
    items = conn.execute(
        'SELECT i.item_id, i.ticker, i.name FROM screen_step_item s JOIN item i USING (item_id) '
        'WHERE s.run_id = ? AND s.step_no = ? AND s.passed = 1 ORDER BY i.ticker', (run_id, last)).fetchall()
    tested = _merged_values(conn, run_id, [i['item_id'] for i in items])
    show = _show_fields(conn, run)
    tested_fields = [f for f in dict.fromkeys(f for v in tested.values() for f in v)
                     if f not in show and f not in ('ticker', 'name')]
    shown = {}
    if show and items:
        cols = ', '.join(f'{"i" if is_attribute(f) else "m"}.{f}' for f in show)   # whitelisted names only
        for r in conn.execute(
                f'SELECT i.item_id, {cols} FROM item i '
                f'LEFT JOIN item_metrics m ON m.item_id = i.item_id AND m.asof_date = ? '
                f'WHERE i.item_id IN (SELECT item_id FROM screen_step_item '
                f'WHERE run_id = ? AND step_no = ? AND passed = 1)', (run['asof_date'], run_id, last)):
            shown[r['item_id']] = list(r)[1:]
    rows = [[i['ticker'], (i['name'] or '')[:name_width], *shown.get(i['item_id'], [None] * len(show)),
             *[tested[i['item_id']].get(f) for f in tested_fields]] for i in items]
    return ['ticker', 'name', *show, *tested_fields], rows


def survivors(conn: sqlite3.Connection, run_id: int) -> str:
    headers, rows = survivor_table(conn, run_id, name_width=30)
    if not rows:
        return 'no survivors'
    return f'{len(rows)} survivors:\n' + _table(rows, headers)


def eliminated(conn: sqlite3.Connection, run_id: int, step_no: int) -> str:
    rows = conn.execute(
        'SELECT i.ticker, i.name, s.reason FROM screen_step_item s JOIN item i USING (item_id) '
        'WHERE s.run_id = ? AND s.step_no = ? AND s.passed = 0 ORDER BY s.reason, i.ticker', (run_id, step_no)).fetchall()
    if not rows:
        return f'nothing eliminated at step {step_no}'
    return (f'{len(rows)} eliminated at step {step_no}:\n'
            + _table([[r['ticker'], (r['name'] or '')[:30], r['reason']] for r in rows], ['ticker', 'name', 'reason']))


def show_run(conn: sqlite3.Connection, run: str | int, step_no: int | None = None) -> str:
    r = find_run(conn, run)
    if r is None:
        return f'run {run} not found'
    parts = [_run_header(r), funnel(conn, r['run_id'])]
    parts.append(eliminated(conn, r['run_id'], step_no) if step_no else survivors(conn, r['run_id']))
    return '\n\n'.join(parts)


def list_runs(conn: sqlite3.Connection, limit: int = 20) -> str:
    rows = conn.execute('SELECT * FROM screen_run ORDER BY run_id DESC LIMIT ?', (limit,)).fetchall()
    if not rows:
        return 'no runs yet'
    return _table([[str(r['run_id']), r['name'], r['asof_date'] or '', r['created_at'], str(r['n_start']),
                    str(r['n_survivors']), f'from run {r["parent_run_id"]} step {r["rerun_from_step"]}'
                    if r['parent_run_id'] else ''] for r in rows],
                  ['run', 'profile', 'as of', 'created', 'start', 'survivors', 're-run'])


def explain(conn: sqlite3.Connection, ticker: str, run: str | int = 'latest') -> str:
    r = find_run(conn, run)
    if r is None:
        return f'run {run} not found'
    item = conn.execute('SELECT i.item_id, i.ticker, i.name, i.active, d.ticker AS duplicate_of FROM item i '
                        'LEFT JOIN item d ON d.item_id = i.duplicate_of WHERE i.ticker = ?',
                        (ticker.upper(),)).fetchone()
    if item is None:
        return f'{ticker.upper()}: not in the database'
    lines = [_run_header(r), f'{item["ticker"]}  {item["name"] or ""}', '']
    steps = conn.execute(
        'SELECT st.step_no, st.description, si.value_json, si.passed, si.reason FROM screen_step st '
        'LEFT JOIN screen_step_item si ON si.run_id = st.run_id AND si.step_no = st.step_no AND si.item_id = ? '
        'WHERE st.run_id = ? ORDER BY st.step_no', (item['item_id'], r['run_id'])).fetchall()
    if steps and steps[0]['passed'] is None:
        why = ('probably added after the run' if item['active'] else
               f'inactive, duplicate listing of {item["duplicate_of"]}' if item['duplicate_of'] else 'inactive')
        return '\n'.join(lines + [f'not a candidate in this run ({why})'])
    for s in steps:
        if s['passed'] is None:
            break
        values = ', '.join(f'{k} = {format_value(v)}' for k, v in json.loads(s['value_json'] or '{}').items())
        lines.append(f'step {s["step_no"]}  {"PASS" if s["passed"] else "FAIL"}  {s["description"]}')
        lines.append(f'        {values}')
        if not s['passed']:
            lines += ['', f'eliminated at step {s["step_no"]}: {s["reason"]}']
            return '\n'.join(lines)
    lines += ['', f'survived all {len(steps)} steps']
    return '\n'.join(lines)
