# CLAUDE.md

Usage and behaviour: README.md. Writing screening profiles: doc/profiles.md.

## Design rules

- Simplicity and explainability come first; an earlier attempt failed by being too complex.
  No frameworks, registries or abstract base classes — plain functions and modules.
- Use `float`, not `Decimal`. Yahoo rate limiting is the simple `_throttle` in
  `market_data/yahoo_finance.py`, not pyrate_limiter.
- `screener/analytics/` holds pure functions: date-indexed pandas Series (normally `adj_close`) in,
  `float | None` out. No database or Yahoo code there, so it can move back to `~/Documents/portfolio` later.
- `~/Documents/portfolio` is read-only: copy code from it, never edit it.
- NULL means "not computable" and is never replaced by 0. Missing data must stay visible in `explain`.
- Profile field names are whitelisted before SQL is built; values only ever become SQL parameters.

## Common changes

- Adding a metric: a new `migrations/NNN_*.sql` adding the column, an entry in `METRICS` in `metrics.py`,
  the calculation in `compute_metrics` (pure helper in `analytics/`), a test, and a line in `doc/profiles.md`.
  Profiles pick up the new column automatically.
- Adding a filterable item attribute: `ATTRIBUTES` in `screening/profile.py` (plus a migration if it is a new
  column), and `doc/profiles.md`.
- Never edit an existing migration; add the next numbered file.

## Working conventions

- Tests: `venv/bin/pytest`. They use a fake Yahoo client and synthetic data; never call Yahoo from tests.
- `data/screener.db` holds real data that took long to download. Don't delete or recreate it; experiment
  with `--db` pointing at a scratch file.
