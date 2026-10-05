"""SQLite connection, schema migrations and small shared helpers."""
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / 'migrations'
DEFAULT_DB = Path('data/screener.db')


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open the database (creating it if needed) and apply any pending migrations."""
    if str(path) != ':memory:':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Apply migrations/NNN_*.sql files newer than the stored schema version, in order."""
    conn.execute('CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)')
    current = conn.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] or 0
    for path in sorted(MIGRATIONS_DIR.glob('[0-9][0-9][0-9]_*.sql')):
        version = int(path.name[:3])
        if version <= current:
            continue
        logger.info(f'applying migration {path.name}')
        conn.executescript(f'BEGIN;\n{path.read_text()}\nINSERT INTO schema_version VALUES ({version});\nCOMMIT;')


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def log_fetch(conn: sqlite3.Connection, kind: str, key: str, error: str | None = None) -> None:
    """Record the outcome of one fetch. error=None means success."""
    now = now_utc()
    conn.execute(
        """INSERT INTO fetch_log (kind, key, last_attempt_at, last_success_at, last_error)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (kind, key) DO UPDATE SET
               last_attempt_at = excluded.last_attempt_at,
               last_success_at = COALESCE(excluded.last_success_at, last_success_at),
               last_error = excluded.last_error""",
        (kind, key, now, None if error else now, error))
