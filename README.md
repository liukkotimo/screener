# screener

Explainable screener for stocks, ETFs and funds on Yahoo Finance data, stored in a single SQLite file.
Design goal: for any item you can see exactly which data it has and (later) at which screening step it was
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

## Inspecting

```bash
screener status            # row counts, latest dates, all fetch errors
screener status SHEL.L     # one item: attributes, price range, dividends, last fetch outcomes
```

## Tests

```bash
venv/bin/pytest
```
