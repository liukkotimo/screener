# CLAUDE.md

Usage and behaviour: README.md. Writing screening profiles: doc/profiles.md.

## Design rules

- Simplicity and explainability come first; an earlier attempt failed by being too complex.
  No frameworks, registries or abstract base classes — plain functions and modules.
- Use `float`, not `Decimal`. EODHD rate limiting is the simple `_throttle` in `market_data/eodhd.py`,
  not pyrate_limiter. EODHD is the only data source; no provider abstraction.
- `screener/analytics/` holds pure functions: date-indexed pandas Series (normally `adj_close`) in,
  `float | None` out. No database or EODHD code there, so it stays portable.
- NULL means "not computable" and is never replaced by 0. Missing data must stay visible in `explain`.
- Profile field names are whitelisted before SQL is built; values only ever become SQL parameters.

## Common changes

- Adding a metric: a new `migrations/NNN_*.sql` adding the column, an entry in `METRICS` in `metrics.py`,
  the calculation in `compute_metrics` (pure helper in `analytics/`), a test, and a line in `doc/profiles.md`.
  Profiles pick up the new column automatically.
- Adding a filterable item attribute: `ATTRIBUTES` in `screening/profile.py` (plus a migration if it is a new
  column), and `doc/profiles.md`.
- Never edit an existing migration; add the next numbered file. (Exception, done once: when the data source
  changed from Yahoo to EODHD, migrations 001-005 were squashed into `001_schema.sql` because no old
  database survived. Don't repeat that for a database that holds data you want to keep.)

## Working conventions

- Tests: `venv/bin/pytest`. They use a fake EODHD client and synthetic data; never call EODHD from tests.
  Raw EODHD responses (`tests/fixtures/eodhd/`, gitignored) are licensed data: never commit them.
- The API key comes from `$EODHD_API_KEY` (or `.env`); never log, print or commit it.
- Mind the daily quota (fundamentals 10 calls, bulk 100, others 1); `screener status` shows today's usage.
- `data/screener.db` holds real data that took long to download. Don't delete or recreate it; experiment
  with `--db` pointing at a scratch file.
