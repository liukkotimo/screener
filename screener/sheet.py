"""Export one screening run to one worksheet of a Google Sheet (needs `pip install gspread`, see README)."""
import sqlite3

from screener.screening.engine import find_run
from screener.screening.report import survivor_table

SCOPES = ['https://www.googleapis.com/auth/spreadsheets']


def run_rows(conn: sqlite3.Connection, run_id: int) -> tuple[list[list], list[int]]:
    """
    The worksheet content of a run as rows of cells, and the 1-based numbers of the two header rows:

        run 12  falling_knives  as of 2026-10-06  (created ...)

        step  in    out   condition
        1     7620  7615  equities only: type == EQUITY
        ...

        3 survivors
        ticker  name  <show: columns>  <other tested fields>
        ...
    """
    run = conn.execute('SELECT * FROM screen_run WHERE run_id = ?', (run_id,)).fetchone()
    rows = [[f'run {run_id}  {run["name"]}  as of {run["asof_date"]}  (created {run["created_at"]})']]
    if run['parent_run_id']:
        rows.append([f'steps 1..{run["rerun_from_step"] - 1} re-used from run {run["parent_run_id"]}'])
    rows.append([])
    steps_header = len(rows) + 1
    rows.append(['step', 'in', 'out', 'condition'])
    for s in conn.execute('SELECT * FROM screen_step WHERE run_id = ? ORDER BY step_no', (run_id,)):
        rows.append([s['step_no'], s['n_in'], s['n_passed'], s['description']])
    headers, survivors = survivor_table(conn, run_id)
    rows += [[], [f'{len(survivors)} survivors']]
    survivors_header = len(rows) + 1
    rows += [headers, *survivors]
    return [['' if c is None else c for c in r] for r in rows], [steps_header, survivors_header]


def default_tab(conn: sqlite3.Connection, run_id: int) -> str:
    return f'{find_run(conn, run_id)["name"]} run {run_id}'[:100]


def export_run(conn: sqlite3.Connection, run_id: int, sheet: str, tab: str | None = None,
               credentials: str | None = None) -> str:
    """
    Write a run to worksheet `tab` (default "<profile> run <id>") of the spreadsheet `sheet` (id or URL).
    An existing tab of that name is overwritten, so exporting the same run twice is safe.
    `credentials` is a service account JSON file (default: gspread's ~/.config/gspread/service_account.json);
    the spreadsheet must be shared with the service account's e-mail address. Returns the tab name.
    """
    try:
        import gspread
        from gspread.utils import rowcol_to_a1
    except ImportError:
        raise ValueError('Google Sheets export needs gspread: pip install gspread  (or pip install -e .[sheets])')

    rows, header_rows = run_rows(conn, run_id)
    tab = tab or default_tab(conn, run_id)
    n_cols = max(len(r) for r in rows)
    try:
        client = gspread.service_account(filename=credentials, scopes=SCOPES) if credentials \
            else gspread.service_account(scopes=SCOPES)
        spreadsheet = client.open_by_url(sheet) if sheet.startswith('http') else client.open_by_key(sheet)
        try:
            ws = spreadsheet.worksheet(tab)
            ws.clear()
            ws.resize(rows=len(rows), cols=n_cols)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(tab, rows=len(rows), cols=n_cols)
        ws.update(values=rows, range_name='A1', value_input_option='RAW')   # RAW: numbers stay numbers, text stays text
        for r in header_rows:
            ws.format(f'A{r}:{rowcol_to_a1(r, n_cols)}', {'textFormat': {'bold': True}})
    except FileNotFoundError as e:
        raise ValueError(f'credentials file not found: {e.filename}') from e
    except gspread.SpreadsheetNotFound as e:
        raise ValueError('spreadsheet not found: check the id/URL and share it with the service account '
                         'e-mail address (client_email in the credentials file) as Editor') from e
    except gspread.exceptions.GSpreadException as e:
        raise ValueError(f'Google Sheets error: {e}') from e
    return tab
