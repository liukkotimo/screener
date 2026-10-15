# screener

Explainable screener for stocks, ETFs and funds on [EODHD](https://eodhd.com) data, stored in a single SQLite file.
Design goal: for any item you can see exactly which data it has and at which screening step it was
eliminated and why.

## Setup

```bash
python3.13 -m venv venv            # 3.13 is just that I prefer it. Should work with 3.10++
source venv/bin/activate           # For bash, select command for your environment
pip install -r requirements.txt
pip install -e .                   # provides the `screener` command (or use `python -m screener`)
```

The database defaults to `data/screener.db` (override with `--db FILE` or `$SCREENER_DB`).
The schema is created and migrated automatically.

Market data comes from EODHD and needs your own API key (a plan with fundamentals, e.g. "All-In-One").
Put it in the environment or in a `.env` file (`KEY=value` lines) in the directory you run screener from;
variables already set in the shell take precedence:

```bash
EODHD_API_KEY=your-key
```

EODHD counts calls against a daily quota: price history, dividends, splits and symbol lists cost 1 call,
fundamentals 10, a whole exchange's last-day bulk prices 100. Every update prints the calls it used,
`screener status` shows today's total, and a run stops by itself before it would leave less than 1000 calls
of the quota (re-run it the next day to continue).

## Building the universe

```bash
screener import-tickers universe/test20.csv        # one EODHD symbol (CODE.EXCHANGE) per line, first column
screener import-exchange HE ST --min-market-cap 5e8  # common stocks of Helsinki and Stockholm, >= 500 M EUR
```

Existing tickers are never overwritten by an import. `import-exchange` takes EODHD exchange codes (`HE`
Helsinki, `ST` Stockholm, `CO` Copenhagen, `OL` Oslo, `XETRA`, `LSE`, `US`, ...). It imports common stocks
quoted in the exchange's currency, skips OTC lines and, using the exchange's last-day bulk prices, everything
below the minimum market cap (in EUR, converted at the latest FX rate). It prints how many symbols were
skipped and why. ETFs and funds can be added with `import-tickers`.

A company can be listed on several exchanges (Nokia on `HE` and `ST`). After importing (and once
`screener metrics` has run, for turnover), keep one listing per company:

```bash
screener dedupe            # dry run: kept listing and the ones that would be dropped, per company
screener dedupe --apply    # deactivate the others (active = 0, duplicate_of = the kept item)
screener dedupe --reset    # reactivate everything deactivated as a duplicate
```

Listings are grouped by ISIN, or by type and exact name. The home-exchange listing is kept (the exchange of
the ISIN's country, e.g. `NOKIA.HE` over `NOKIA.ST`), otherwise the most liquid one. Share classes with the same
name (Volvo A and B) count as one company. Re-run it after an import; nothing is deleted.

## Updating data

```bash
screener update items     # name, type, exchange, currency, ISIN, market cap (101 calls per exchange)
screener update prices    # daily OHLC, close, adj_close, volume + dividends, incremental (3 calls per item)
screener update fundamentals  # statements, sector, industry, forward EPS (10 calls per stock/ETF)
screener update fx        # daily EUR rates for every quote and reporting currency used by active items
screener update all       # all of the above
```

Options: `--tickers T1 T2` (items/prices/fundamentals), `--years 5` (history depth for new items),
`--fundamentals-max-age-days 90` (fundamentals older than this are refreshed).

`update items` reads each exchange's symbol list and last-day bulk prices, so it costs the same for one item
as for all items of an exchange. Fundamentals cost 10 calls per item: ~1 000 stocks take ~10 000 calls.
Like the other updates it can be interrupted and resumed; items fetched within the max age are skipped.
To spread it over several days, use `--limit N` (e.g. `screener update fundamentals --limit 2000`): each run
takes the never-fetched items first, largest market cap first, then the oldest.
Statements are stored per fiscal year in the company's reporting currency (`financial_currency`, which may
differ from the quote currency) and never deleted. Sector and industry are only known after this step.

- Updates are incremental and resumable: work is committed per ticker/batch; an interrupted run just
  continues next time.
- A failing ticker is logged and skipped. The latest outcome per ticker is kept in `fetch_log`.
- EODHD gives raw prices plus an `adjusted_close` that it recomputes after every dividend and split. We store
  `close` split-adjusted (computed from EODHD's split history) and `adj_close` as EODHD gives it. Each update
  re-downloads a few overlapping days; if they no longer match the stored ones, that item's full history is
  re-downloaded.
- Prices and dividends are stored in the quote unit EODHD uses (LSE in pence, `GBX`); `market_cap` is in the
  major currency (GBP). FX rates are stored as EUR per unit of the major currency.

## Metrics

```bash
screener metrics                     # all active items, as of the latest price date
screener metrics --asof 2026-06-30   # backfill another date (one row per item and as-of date)
screener metrics --list              # definitions
```

Rules that apply to every metric:

- Returns, drawdowns and risk use `adj_close` (dividends and splits included); performance is plain total
  return, not annualized.
- A window endpoint on a weekend/holiday uses the last price on or before it, at most 7 days older.
  Otherwise the metric is NULL. The same holds when the item's last price is more than 7 days before as-of.
- NULL always means "not computable" (short history, stale, missing FX or volume); it is never replaced by 0.
  A dividend yield of 0 means a full year of prices and no dividends.
- `volatility_1y` needs at least 200 daily returns, `cvar_95` at least 500 (about two years of the 3-year window).
- `market_cap` is EODHD's snapshot scaled by the close price change from the snapshot date to as-of, so
  backdated runs do not use today's market cap. ETFs have no market cap; use `total_assets_eur`.
- `dividend_yield` uses the plain close (adj_close would inflate it).
- Fundamental metrics use annual statements only; a fiscal year counts from 90 days after its end, and
  statements older than ~18 months give NULL. Valuation ratios convert statement figures and price/market cap
  to EUR. Details: [profiles.md](doc/profiles.md).

The calculations live in `screener/analytics/` as plain functions on a pandas Series (no database code).

## Screening

A profile is a YAML file with strictly sequential steps; each step only sees the survivors of the previous
one (step 1: all active items). Profiles are your own files: keep them in `profiles/`, which is gitignored so
they stay private. [profiles.md](doc/profiles.md) explains the format, attributes and metrics, and has
[complete examples](doc/profiles.md#examples) to copy as a starting point.

```bash
screener screen --profile profiles/my_profile.yaml                # uses the latest metrics as-of date
screener screen --profile profiles/my_profile.yaml --from-step 6   # re-use stored steps 1-5
screener explain NOVO-B.CO               # every step with tested values; where and why it was eliminated
screener explain NOVO-B.CO --run 12
screener runs                            # list runs
screener show-run 12                     # funnel and survivors
screener show-run 12 --step 4            # everything eliminated at step 4, with reasons
screener export-sheet 12                 # run to a Google Sheet tab, see below
screener prune --keep 10                 # keep the newest 10 runs per profile (or --run ID)
```

Every candidate of every step is stored in `screen_step_item` with its tested values, pass/fail and reason.
`--from-step N` copies steps 1..N-1 from the latest run of the same profile (or `--base-run ID`); it refuses
if any of those steps changed or the as-of date differs.

## Google Sheets export

One run goes to one worksheet tab: a step summary on top (`step`, `in`, `out`, `condition`), then the survivors
with the profile's `show:` columns first and every field the steps tested after them.

```bash
pip install -e .[sheets]                       # installs gspread (optional dependency)
screener screen --profile profiles/my_profile.yaml --to-sheet
screener export-sheet                          # latest run;  or: screener export-sheet 12
```

The tab is named `<profile> run <id>` (`--tab NAME` to change it); exporting the same tab again overwrites it.
One-time setup (service account, no browser login needed afterwards):

1. In Google Cloud Console create a project, enable the *Google Sheets API*, create a *service account* and
   download its JSON key (keep it outside the repo).
2. Share your spreadsheet with the service account's e-mail address (`client_email` in the key file) as Editor.
3. Tell screener where things are, either with options or environment variables:
   `--sheet ID_OR_URL` / `$SCREENER_SHEET` and `--credentials FILE` / `$SCREENER_GOOGLE_CREDENTIALS`
   (default key file: `~/.config/gspread/service_account.json`). Environment variables can also go in a
   `.env` file (`KEY=value` lines) in the directory you run screener from; variables already set in the
   shell take precedence.

## Inspecting

```bash
screener status            # row counts, latest dates, all fetch errors
screener status NOKIA.HE   # one item: attributes, duplicates (none / is KEPT / has OTHERS), price range,
                           # dividends, statements, last fetch outcomes
screener items             # every item: id, ticker, name, ISIN, type, source, active, last update (local time)
screener items --active    # only active items
```

## Tests

```bash
venv/bin/pytest
```

## Data and disclaimer

This project retrieves market data from EODHD with your own API key. The data is not covered by this
project's license and remains subject to [EODHD's terms](https://eodhd.com/financial-apis/terms-conditions):
for a non-professional subscription the data may be stored and analysed for private, non-commercial use, but
not redistributed or shown to others in any form. Keep the database, exported Google Sheets and any raw
responses to yourself, and don't share your API key.

This software is for research and educational purposes only and is not financial advice.
No data is included in this repository.

## License

Copyright (C) 2026 Timo Liukko. Licensed under the GNU General Public License, version 3 or
(at your option) any later version (GPL-3.0-or-later). See [LICENSE](LICENSE).
