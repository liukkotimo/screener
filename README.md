# screener

Explainable screener for stocks, ETFs and funds on Yahoo Finance data, stored in a single SQLite file.
Design goal: for any item you can see exactly which data it has and at which screening step it was
eliminated and why.

## Setup

```bash
python3.13 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install -e .          # provides the `screener` command (or use `python -m screener`)
```

The database defaults to `data/screener.db` (override with `--db FILE` or `$SCREENER_DB`).
The schema is created and migrated automatically.

## Building the universe

```bash
screener import-tickers universe/test20.csv                  # one Yahoo ticker per line (first column)
screener import-yahoo --region fi --region se --min-market-cap 5e8   # equities from the Yahoo screener
```

Existing tickers are never overwritten by an import.

## Updating data

```bash
screener update items     # name, type, exchange, country, currency, sector, market cap ... (Yahoo info)
screener update prices    # daily OHLC, close, adj_close, volume + dividends, incremental
screener update fx        # daily EUR rates for every currency used by active items
screener update all       # all of the above
```

Options: `--tickers T1 T2` (items/prices only), `--years 5` (history depth for new items),
`--max-age-days 7` (item info older than this is refreshed).

- Updates are incremental and resumable: work is committed per ticker/batch; an interrupted run just
  continues next time.
- A failing ticker is logged and skipped. The latest outcome per ticker is kept in `fetch_log`.
- Yahoo re-adjusts past prices after dividends and splits. Each update re-downloads a few overlapping days;
  if they no longer match the stored ones, that item's full history is re-downloaded.
- Prices and dividends are stored in the quote unit Yahoo uses (LSE in pence, `GBp`); `market_cap` is in the
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
- `market_cap` is Yahoo's snapshot scaled by the close price change from the snapshot date to as-of, so
  backdated runs do not use today's market cap. Funds/ETFs usually have no market cap; use `total_assets_eur`.
- `dividend_yield` uses the plain close (adj_close would inflate it).

The calculations live in `screener/analytics/` as plain functions on a pandas Series (no database code).

## Screening

A profile is a YAML file with strictly sequential steps; each step only sees the survivors of the previous
one (step 1: all active items). See `profiles/` for examples (`falling_knives.yaml`, `steady_dividend.yaml`,
`example.yaml`).

```yaml
name: example
steps:
  - {attr: exchange, op: in, value: [HEL, STO, NYQ]}
  - {metric: market_cap_eur, op: ">", value: 1000000000}
  - label: big fall within 1 or 3 years       # optional, shown in output
    any_of:
      - {metric: perf_1y, op: "<", value: -0.33}
      - {metric: perf_3y, op: "<", value: -0.33}
```

- `attr`: item columns `ticker name type exchange exchange_name country currency sector industry source`
  (text comparison is case-sensitive; values as Yahoo gives them, e.g. exchange `HEL`, type `EQUITY`).
- `metric`: any `item_metrics` column (`screener metrics --list`).
- `op`: `<  <=  >  >=  ==  !=  between [lo, hi]  in [..]  not_in [..]`.
- `any_of` / `all_of` combine conditions within one step (may be nested).

Missing data follows SQL logic: a condition on a NULL value is unknown. `any_of` passes if any part is true;
`all_of` fails if any part is false; when the known parts do not decide, the item fails with reason
`missing data: <metric>`. Items without a metrics row for the as-of date fail metric steps the same way.

```bash
screener screen --profile profiles/falling_knives.yaml          # uses the latest metrics as-of date
screener screen --profile profiles/falling_knives.yaml --from-step 6   # re-use stored steps 1-5
screener explain NOVO-B.CO               # every step with tested values; where and why it was eliminated
screener explain NOVO-B.CO --run 12
screener runs                            # list runs
screener show-run 12                     # funnel and survivors
screener show-run 12 --step 4            # everything eliminated at step 4, with reasons
screener prune --keep 10                 # keep the newest 10 runs per profile (or --run ID)
```

Every candidate of every step is stored in `screen_step_item` with its tested values, pass/fail and reason.
`--from-step N` copies steps 1..N-1 from the latest run of the same profile (or `--base-run ID`); it refuses
if any of those steps changed or the as-of date differs.

## Inspecting

```bash
screener status            # row counts, latest dates, all fetch errors
screener status SHEL.L     # one item: attributes, price range, dividends, last fetch outcomes
```

## Tests

```bash
venv/bin/pytest
```
