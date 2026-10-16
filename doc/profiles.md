# Writing screening profiles

A profile is a YAML file that filters the item universe in **strictly sequential steps**. Step 1 sees all
active items; each later step sees only the survivors of the step before. Every candidate's tested values
and pass/fail reason are stored, so `screener explain TICKER` shows where and why an item dropped out.

```bash
screener screen --profile profiles/my_profile.yaml
screener show-run <run_id>              # funnel + survivors
screener show-run <run_id> --step 3     # who was eliminated at step 3, and why
```

Parsing and validation: `screener/screening/profile.py`. Metric definitions: `screener/metrics.py`
(`screener metrics --list`).

## File structure

```yaml
name: my_profile                # required; runs are grouped by this name
description: one-line summary   # optional
show: [sector, perf_1y, pe_forward]   # optional; extra columns for the survivors list (see below)
steps:                          # required, non-empty list
  - {attr: type, op: ==, value: Common Stock}
  - label: shown in output      # optional on any step
    metric: market_cap_eur
    op: ">="
    value: 1000000000
  - label: either condition
    any_of:                     # OR; all_of = AND. Groups can be nested.
      - {metric: perf_1y, op: "<=", value: -0.25}
      - {metric: perf_3y, op: "<=", value: -0.40}
  - label: the 10 cheapest of those
    metric: pe_forward
    bottom: 10                  # ranking step: keep the 10 lowest (top: 10 = highest)
```

No other top-level or step keys are allowed; unknown keys, attributes, metrics or operators are rejected
with an error listing the valid choices.

`show` is an optional list of attribute or metric names (anything valid after `attr:` / `metric:`). The
survivors list (`screen`, `show-run`, Google Sheet) then has these columns right after `ticker` and `name`,
followed by every other field that any step tested, so nothing a step looked at is hidden. Values are the
item's current attributes and the metrics of the run's as-of date. It does not filter anything.

A step is either a **condition** (exactly one of `attr` / `metric`, plus `op` and `value`), a **group**
(exactly one of `any_of` / `all_of` with a non-empty list of conditions or groups) or a **ranking step**
(`metric` plus `top` or `bottom`, see [below](#ranking-steps-top--bottom)). `label` is only meaningful at
step level.

## Operators

| op | value | notes |
|---|---|---|
| `<` `<=` `>` `>=` | number | metrics need a number (except `last_price_date`, `fiscal_year_end` and `ttm_quarter_end`, which take an ISO date string) |
| `==` `!=` | number or text | |
| `between` | `[low, high]` | inclusive, numbers, `low <= high` |
| `in` / `not_in` | non-empty list | |

YAML gotchas: quote operators that start with `<`, `>` or `!` (`op: ">="`, `op: "!="` — an unquoted `!`
starts a YAML tag and the file fails to load); `==` can stay unquoted. Write numbers as plain digits
(`1000000000`) or with a dot (`1.0e+9`) — YAML reads `1e9` as text and validation fails.

## Missing data (NULL)

A condition on a NULL value is *unknown*, not false. `any_of` passes if any part is true; `all_of` fails
if any part is false. When the known parts don't decide, the item **fails** with reason
`missing data: <field>`. `not_in` / `!=` also fail on NULL — they do not let items with an unknown value
through. Items without a metrics row for the as-of date fail every metric step.

Practical consequence: a condition on a metric that needs long history (e.g. `perf_3y`, `cvar_95`)
silently removes young listings. If that is not intended, put it inside an `any_of` with an alternative.

## Ranking steps (`top` / `bottom`)

A ranking step keeps the n candidates with the highest (`top: n`) or lowest (`bottom: n`) value of one
metric, e.g. "10 best dividend payers in Helsinki":

```yaml
steps:
  - {attr: exchange, op: ==, value: HE}
  - {attr: type, op: ==, value: Common Stock}
  - label: 10 best dividend payers
    metric: dividend_yield
    top: 10
```

- `top` means highest value and `bottom` lowest, not "best": for `pe_forward`, `volatility_1y` or `cvar_95` the
  best ones are `bottom`.
- It ranks only **this step's candidates**, the survivors of the previous step. Filter with ordinary steps
  first (exchange, type, size), then rank. A ranking step can also go in the middle ("top 50 by size, then
  the cheap ones"); later steps only see its survivors.
- **Ties are included**: items with the same value as the n-th one pass too, so there can be more than n.
- Items without a value (NULL) are not ranked; they fail with `missing data: <metric>`.
- The tested values include the rank (`dividend_yield_rank = 3`). An eliminated item reads e.g.
  `rank 14 of 80 by dividend_yield (top 10 kept)`, where 80 is the number of candidates that have a value.
- When the last step is a ranking step, the survivors list is ordered by rank instead of by ticker.
- Only metrics can be ranked, and only as a whole step (not inside `any_of` / `all_of`). n is a positive
  whole number.

## Attributes (`attr:`)

Text columns of the `item` table, from EODHD (exchange symbol list and fundamentals). Comparison is
**case-sensitive** and must match EODHD's spelling exactly.

| attr | meaning | example values |
|---|---|---|
| `ticker` | EODHD symbol `CODE.EXCHANGE` | `NOKIA.HE`, `VOLV-B.ST`, `SHEL.LSE`, `AAPL.US` |
| `name` | company / fund name | |
| `type` | EODHD type | `Common Stock`, `Preferred Stock`, `ETF`, `FUND` |
| `exchange` | EODHD exchange code (the ticker suffix) | `HE`, `ST`, `CO`, `OL`, `XETRA`, `LSE`, `US` |
| `exchange_name` | readable exchange | `Helsinki Exchange`, `Stockholm Exchange` |
| `country` | country of the listing (not the company: `NOKIA.ST` has `Sweden`) | `Finland`, `Sweden`, `USA` |
| `currency` | quote currency (may be a minor unit) | `EUR`, `SEK`, `USD`, `GBX` (pence) |
| `sector` | EODHD sector | `Basic Materials`, `Communication Services`, `Consumer Cyclical`, `Consumer Defensive`, `Energy`, `Financial Services`, `Healthcare`, `Industrials`, `Real Estate`, `Technology`, `Utilities` |
| `industry` | EODHD industry (finer than sector) | `Banks - Regional`, `Software - Application` |
| `isin` | ISIN | `FI0009000681` |
| `source` | how the ticker entered the universe | `csv:test20`, `eodhd:HE` |

Tip: the same company is often listed on several exchanges (e.g. Nokia on `HE` and `ST`, Nordea on `HE`,
`ST` and `CO`). `screener dedupe --apply` keeps one listing per company (see README); otherwise filter on
`exchange` early to avoid duplicates and illiquid secondary listings. Funds/ETFs have no `sector`, and
sector/industry are only filled after `screener update fundamentals`.

To check actual values in your database: `screener status TICKER` shows one item's attributes.

## Metrics (`metric:`)

Columns of `item_metrics`, calculated per item for an as-of date (default: the latest calculated date).
Any column added to that table is automatically usable.

Rules for all metrics:
- **Fractions, not percent**: `-0.25` = −25 %, `0.05` = 5 %.
- Returns, drawdowns and risk use `adj_close` (dividends and splits included). `perf_*` are plain total
  return over the window, **not annualized**; `cagr_*` and growth rates are annual.
- Windows are calendar periods (`5y` = as-of minus 5 years), not counts of trading days. The whole window
  must be covered by stored prices, so `cagr_10y` is NULL for an item with 6 years of history.
- NULL = not computable (history too short, price older than 7 days at as-of, missing FX or volume).
  Never replaced by 0.
- Money values are in EUR at the as-of FX rate.

### Performance

| metric | definition | typical range |
|---|---|---|
| `perf_1m` | total return over 1 month | −0.3 … 0.3 |
| `perf_3m` | total return over 3 months | |
| `perf_6m` | total return over 6 months | |
| `perf_1y` | total return over 1 year | −0.8 … 2 |
| `perf_3y` | total return over 3 years (not annualized) | |
| `perf_5y` | total return over 5 years (not annualized) | |
| `cagr_5y` | annual growth rate over 5 years: `(1 + perf_5y) ** (1/5) − 1` | −0.2 … 0.3 |
| `cagr_10y` | annual growth rate over 10 years (needs 10 years of prices) | |

### Trend and relative strength

| metric | definition | typical range |
|---|---|---|
| `ma_200_distance` | last price / 200-day moving average − 1 | −0.3 … 0.3 |
| `ma_200_slope_3m` | 200-day moving average now / 3 months ago − 1 | −0.1 … 0.1 |
| `relative_strength_market_6m` | `perf_6m` minus the home-market benchmark's return over the same dates | −0.3 … 0.3 |
| `relative_strength_world_6m` | `perf_6m` minus the world benchmark's return (MSCI World ETF) | |

The 200-day average is the mean of the last 200 stored prices; both averages of the slope need 200 full
prices, within at most 366 calendar days (no large gaps).

Relative strength is `stock return − benchmark return`: +15 % against +8 % gives `0.07`, −10 % against
−20 % gives `0.10`. The benchmark is read on the item's own first and last window dates, and its return is
converted into the item's currency (via EUR) when the currencies differ. Benchmarks are configured in
`screener/benchmarks.yaml` (`$SCREENER_BENCHMARKS` for another file): the home market by the item's listing
exchange (never by currency), plus one world benchmark. Total-return series are used where EODHD has them:
OMX Helsinki GI, OMX Stockholm GI, S&P 500 TR, STOXX Europe 600 NR. EODHD has no MSCI World index, so the
world benchmark is the iShares Core MSCI World UCITS ETF (Acc) on Xetra (`EUNL.XETRA`, EUR, dividends
reinvested, net of its 0.20 % fee, since 2009). An exchange without a benchmark, or a benchmark without
stored prices or FX, gives NULL (`screener metrics` lists the benchmarks it could not use). No other
benchmark is substituted.

### Drawdown and timing

| metric | definition | range |
|---|---|---|
| `drawdown_52w` | last price vs. 52-week high: `last / max − 1` | ≤ 0 (0 = at the high) |
| `drawdown_3y` | last price vs. 3-year high | ≤ 0 |
| `drawdown_5y` | last price vs. 5-year high | ≤ 0 |
| `drawdown_available_history` | last price vs. the high of all stored history (needs ≥ 1 year) | ≤ 0 |
| `max_drawdown_3y` | worst peak-to-trough fall within 3 years | ≤ 0 |
| `max_drawdown_5y` | worst peak-to-trough fall within 5 years | ≤ 0 |
| `max_drawdown_10y` | worst peak-to-trough fall within 10 years | ≤ 0 |
| `days_since_low_52w` | calendar days since the lowest price of the last 52 weeks | 0 (at the low now) … ~365 |
| `days_since_low_3y` | calendar days since the lowest price of the last 3 years | 0 … ~1095 |
| `recovery_from_low_3y` | last price vs. 3-year low: `last / min − 1` | ≥ 0 (0 = at the low) |

`drawdown_*` is *where the price is now* relative to the high; `max_drawdown_*` is the *worst fall that
happened* in the window, even if fully recovered since. A stock that fell from 100 to 50 and then rose to a
new high of 120 has `drawdown_5y = 0` and `max_drawdown_5y = -0.5`. `drawdown_available_history` depends on
how much history is stored (`screener update prices --years N --full`), so compare it only between items
with similar history.

### Risk

| metric | definition | typical range |
|---|---|---|
| `volatility_1y` | annualized std. dev. of daily returns over 1 year (needs ≥ 200 returns) | 0.15 (calm) … 0.6+ (wild) |
| `volatility_3y` | the same over 3 years (needs ≥ 500 returns) | |
| `downside_volatility_3y` | `sqrt(mean(min(r, 0)²)) × sqrt(252)` over 3 years: only falls count, the mean is over all days (needs ≥ 500 returns) | 0.1 … 0.4 |
| `ulcer_index_3y` | `sqrt(mean(drawdown²))`, each day's drawdown from the running high since the window start; deep *and* long falls raise it (needs ≥ 500 returns) | 0.02 (calm) … 0.3 |
| `cvar_95` | average of the worst 5 % daily returns over 3 years, as a **positive** loss (needs ≥ 500 returns) | 0.03 … 0.10 |

Note the sign: lower `cvar_95` / `ulcer_index_3y` = less risk, so filter with `<=`.

### Size, liquidity, price

| metric | definition |
|---|---|
| `market_cap` | EODHD market cap scaled to as-of by the price change, in the major quote currency (GBP, not GBX) |
| `market_cap_eur` | `market_cap` in EUR — use this for cross-market comparison |
| `total_assets_eur` | ETF total assets in EUR (ETFs have no market cap) |
| `avg_value_traded_eur_3m` | average daily close × volume over 3 months, in EUR (NULL if no volume data) |
| `price_eur` | last close in EUR |

Rough size scale: 1e9 € (`1000000000`) small/mid cap, 1e10 € (`10000000000`) large cap.
Liquidity: ≥ 1e6 €/day (`1000000`) is comfortably tradable for a private investor.

### Dividends

| metric | definition |
|---|---|
| `dividend_yield` | dividends with ex-date in the last 12 months / last close. 0 = full year of prices, no dividends |

Uses the plain close, not adj_close. A one-off special dividend inflates it; a cut dividend is not yet
visible until the next ex-date.

### Fundamentals (annual statements)

From EODHD's annual income statement, balance sheet and cash flow (`screener update fundamentals`), common
stocks only. Rules:
- Only **annual** figures here; the `*_ttm` metrics below use quarterly ones. The latest fiscal year used is the newest one that ended
  at least 90 days before as-of (assumed published by then); it is shown in `fiscal_year_end`. If that year is
  more than ~18 months old, all fundamental metrics are NULL.
- Ratios within one statement need no currency conversion. Valuation metrics compare statement figures
  (in the statement currency `financial_currency`, e.g. USD for Shell) with market cap / price in EUR at the
  as-of FX rate.
- Values that make a ratio meaningless (EBIT or EBITDA ≤ 0, revenue ≤ 0, invested capital ≤ 0) give NULL.
- EBIT is pretax income + interest expense and EBITDA is EBIT + depreciation and amortization (computed by
  us, not EODHD's own EBIT field). A company that reports no interest expense therefore has no EBIT.
- EODHD has up to ~30 fiscal years; stored years are kept, restatements overwrite.

| metric | definition | typical range |
|---|---|---|
| `roic` | EBIT × (1 − tax rate) / (equity + total debt − cash). Tax rate = tax / pretax income, limited to 0 … 0.4; 0 for a loss | 0.05 … 0.30 |
| `roic_avg_4y` | mean `roic` of the latest 4 consecutive fiscal years; NULL unless all 4 are computable | |
| `roic_avg_10y` | mean `roic` of the latest 10 consecutive fiscal years (all 10 computable) | |
| `roic_min_5y` | lowest `roic` of the latest 5 consecutive fiscal years | |
| `roic_std_5y` | population std. dev. of `roic` over the latest 5 consecutive fiscal years; low = stable | 0 … 0.05 |
| `ebit_margin` | EBIT / revenue | 0.05 … 0.30 |
| `ebit_margin_change_1y` | `ebit_margin` minus the previous year's (`0.03` = +3 percentage points) | −0.05 … 0.05 |
| `ebit_margin_avg_5y` | mean `ebit_margin` of the latest 5 consecutive fiscal years | |
| `ebit_margin_trend_3y` | (`ebit_margin` − its value 3 fiscal years earlier) / 3: percentage points per year | −0.02 … 0.02 |
| `revenue_cagr_3y` | annual revenue growth rate over 3 fiscal years | −0.1 … 0.3 |
| `revenue_cagr_5y` | annual revenue growth rate over 5 fiscal years | |
| `eps_cagr_5y` | annual growth rate of EPS (net income / year-end shares) over 5 fiscal years; NULL if either EPS ≤ 0 | |
| `shares_outstanding_cagr_5y` | annual growth rate of year-end shares over 5 fiscal years; < 0 = buybacks, > 0 = dilution | −0.03 … 0.05 |
| `fcf_margin` | free cash flow / revenue | |
| `fcf_margin_avg_5y` | mean `fcf_margin` of the latest 5 consecutive fiscal years | |
| `fcf_positive_years_10y` | how many of the latest 10 consecutive fiscal years had FCF > 0 (integer 0 … 10) | |
| `net_debt_ebitda` | (total debt − cash and short-term investments) / EBITDA; negative = net cash | < 0 … 4 |
| `net_debt_ebitda_max_5y` | highest `net_debt_ebitda` of the latest 5 consecutive fiscal years; NULL if EBITDA ≤ 0 in any of them | |
| `interest_coverage` | EBIT / interest expense. **999** = interest expense reported as 0; NULL if not reported | 3 … 50 |
| `interest_coverage_min_5y` | lowest `interest_coverage` of the latest 5 consecutive fiscal years (999 for a year without interest) | |
| `pe_forward` | price / consensus EPS estimate for the next fiscal year (EODHD earnings trend `+1y`). NULL if EPS ≤ 0 or the as-of date is more than 100 days from when the estimate was fetched (it is a snapshot, refreshed with the fundamentals) | 8 … 40 |
| `ev_ebit` | (market cap + net debt) / EBIT. Minority interests are ignored | 5 … 30 |
| `fcf_yield` | free cash flow / market cap | 0 … 0.10 |
| `fcf_payout_ratio` | dividends paid / free cash flow; NULL if FCF ≤ 0 or no dividend line reported | 0 … 1 |

Multi-year rules: `*_5y` / `*_10y` averages, minimums, maximums and counts need that many **consecutive**
fiscal years (year ends 9 … 15 months apart), every one of them computable; one missing year or one missing
figure gives NULL, never a 0. Growth rates (`*_cagr_*`, `ebit_margin_trend_3y`) need only the two endpoint
years, the earlier one ending 5 (or 3) years before the latest (± ~3 months); no CAGR from a value ≤ 0.

Share counts are EODHD's year-end `commonStockSharesOutstanding`, which EODHD reports adjusted for the
splits known when the fundamentals were fetched (checked on 20 companies with splits). A split stored by
`update prices` after that fetch is applied by us. EODHD's history is not always consistent (Sampo has
adjusted, unadjusted and early post-split years): when a year's count jumps from the year before by roughly
the ratio of any later split, up or down, that year and all earlier ones are treated as unknown, so metrics
that need them (`eps_cagr_5y`, `shares_outstanding_cagr_5y`, valuation history) are NULL. A count covering all
share classes (Volvo A + B) is combined with the price of the listing screened.

Tips: `pe_forward` is computed by us with both sides in EUR, so it also works when quote and reporting
currency differ (e.g. GBX price / USD EPS). Companies that report interest expense as 0 get
`interest_coverage` 999; when it is not reported at all, EBIT and everything built on it is NULL. Banks and insurers have no meaningful EBIT/EBITDA or net debt; exclude `Financial Services` when
screening on these.

### Fundamentals (trailing twelve months)

From EODHD's quarterly statements (fetched in the same `screener update fundamentals` call). TTM = the latest
4 consecutive quarters, so these are fresher than the annual metrics above, which can be up to ~15 months old.
The annual metrics and the valuation history are not affected. Rules:
- Flow figures (revenue, EBIT, EBITDA, net income, FCF, interest, tax, dividends) are **summed** over the 4
  quarters; debt, cash, equity and shares come from the **latest** quarter. A figure missing in any of the 4
  quarters gives NULL, never a partial sum.
- A quarter counts from 60 days after its end; if the latest such quarter is more than ~8 months old, all
  `*_ttm` metrics are NULL. Quarters must be consecutive (ends ~3 months apart).
- Companies that report only half-yearly (e.g. Nestlé, LVMH, Unilever) have no 4 consecutive quarters: their
  `*_ttm` metrics are NULL, the annual ones still work.
- EODHD's quarters often miss a line the annual statement has, most often interest expense (Nokia, ASML).
  Then EBIT, and everything built on it, is NULL in TTM while the annual metric has a value.
- Ratios and EUR conversion as for the annual metrics.

| metric | definition | typical range |
|---|---|---|
| `roic_ttm` | `roic` on TTM figures (equity and net debt of the latest quarter) | 0.05 … 0.30 |
| `ebit_margin_ttm` | TTM EBIT / TTM revenue | 0.05 … 0.30 |
| `fcf_margin_ttm` | TTM free cash flow / TTM revenue | |
| `net_debt_ebitda_ttm` | net debt of the latest quarter / TTM EBITDA; NULL if EBITDA ≤ 0 | < 0 … 4 |
| `interest_coverage_ttm` | TTM EBIT / TTM interest expense (999 = reported as 0) | 3 … 50 |
| `ev_ebit_ttm` | (market cap + net debt of the latest quarter) / TTM EBIT; NULL if EBIT ≤ 0 | 5 … 30 |
| `pe_ttm` | market cap / TTM net income; NULL if net income ≤ 0 | 8 … 40 |
| `fcf_yield_ttm` | TTM free cash flow / market cap | 0 … 0.10 |
| `revenue_growth_ttm` | TTM revenue / TTM revenue a year earlier − 1 (needs 8 consecutive quarters) | −0.1 … 0.3 |
| `eps_growth_ttm` | TTM EPS / TTM EPS a year earlier − 1; EPS = net income / quarter-end shares (split-adjusted as above); NULL if either EPS ≤ 0 | |
| `ttm_quarter_end` | end of the latest quarter in the TTM figures (ISO text, see Data quality) | |

### Valuation vs. own history

| metric | definition | range |
|---|---|---|
| `ev_ebit_median_5y` | median EV/EBIT of the monthly samples of the last 5 years | |
| `ev_ebit_percentile_5y` | share of those samples ≤ the current EV/EBIT | 0 … 1; **high = expensive** |
| `fcf_yield_median_5y` | median FCF yield (FCF / market cap) of the samples | |
| `fcf_yield_percentile_5y` | share of the samples ≤ the current FCF yield | 0 … 1; **high = cheap** |
| `pe_percentile_5y` | share of the P/E samples ≤ the current P/E (trailing annual net income) | 0 … 1; high = expensive |
| `ev_sales_percentile_5y` | share of the EV/Sales samples ≤ the current EV/Sales | 0 … 1; high = expensive |

Percentile = `count(samples <= current) / count(samples)`: `ev_ebit_percentile_5y = 0.92` means today's
EV/EBIT is at least as high as in 92 % of the past 5 years' samples. FCF yield runs the other way: a high
yield (and percentile) is the cheap end. Filter cheap stocks with `ev_ebit_percentile_5y <= 0.3` or
`fcf_yield_percentile_5y >= 0.7`.

How the samples are made:
- One sample per month: as-of minus 1, 2, … 60 months (60 samples). The current value is the same
  calculation on the as-of date. Sampling monthly keeps single trading days from dominating.
- Every sample uses only what was known on its date: the close of that day × the year-end share count of the
  latest fiscal year public then (90 days after year end, at most ~18 months old), that year's net debt,
  EBIT, revenue, net income and FCF, and the FX rates of that day. Today's statements are never combined with
  old prices.
- Meaningless values are left out: EV/EBIT and EV/Sales need EBIT / revenue > 0 and EV > 0, P/E needs net
  income > 0. A negative FCF yield is kept (cash burn is information). Medians and percentiles need at least
  36 valid samples and a valid current value, else NULL.
- This is **approximately** point-in-time, not exact: EODHD has no filing dates (the 90-day lag is
  assumed) and gives the statements as reported today, so restatements are mixed in; year-end share counts
  miss buybacks during the year. The current value is computed the same way (statement share count × price),
  so it can differ slightly from `ev_ebit` / `fcf_yield`, which use EODHD's market cap.

### Data quality

| metric | definition |
|---|---|
| `last_price_date` | date of the last price on or before as-of (ISO text, e.g. `"2026-09-30"`) |
| `n_obs` | number of stored daily prices up to as-of |
| `fiscal_year_end` | end of the latest fiscal year used by the fundamental metrics (ISO text); NULL = no usable statements |
| `ttm_quarter_end` | end of the latest quarter used by the `*_ttm` metrics (ISO text); NULL = no 4 usable consecutive quarters |

`n_obs >= 750` is roughly "at least 3 years of history".

## Writing good profiles

- **Cheap, broad filters first** (type, exchange, size, liquidity), specific signal conditions last.
  The funnel in `show-run` is then easy to read, and `--from-step N` lets you re-run only the tail after
  tweaking a late threshold without recomputing the earlier steps.
- **One idea per step**, with a `label` saying the intent in plain words ("deep fall from the 3-year high").
  Labels appear in `explain` and `show-run`.
- Use `any_of` when either of two measurements expresses the same idea, and to give items with short
  history an alternative path past long-window metrics.
- Start with loose thresholds, look at the funnel, then tighten. A step that eliminates almost nothing or
  almost everything is usually a wrong unit (percent vs. fraction) or a wrong sign.

## Examples

### Falling knives — big drop, recently stabilized

```yaml
name: falling_knives
description: big drop from the 3-year high, recent stabilization
steps:
  - label: equities only
    attr: type
    op: in
    value: [Common Stock]
  - label: not too small
    metric: market_cap_eur
    op: ">="
    value: 1000000000
  - label: tradable
    metric: avg_value_traded_eur_3m
    op: ">="
    value: 1000000
  - label: deep fall from the 3-year high
    metric: drawdown_3y
    op: "<="
    value: -0.40
  - label: big loss over 1 or 3 years
    any_of:
      - {metric: perf_1y, op: "<=", value: -0.25}
      - {metric: perf_3y, op: "<=", value: -0.40}
  - label: no new low in the last month
    metric: days_since_low_52w
    op: ">="
    value: 30
  - label: last 3 months roughly flat
    metric: perf_3m
    op: between
    value: [-0.10, 0.15]
```

### Steady dividend — large, calm, high-yield

```yaml
name: steady_dividend
steps:
  - {attr: type, op: ==, value: Common Stock}
  - {attr: sector, op: not_in, value: [Energy, Basic Materials]}
  - {metric: market_cap_eur, op: ">=", value: 10000000000}
  - {metric: dividend_yield, op: ">=", value: 0.05}
  - {metric: volatility_1y, op: "<=", value: 0.25}
  - {metric: max_drawdown_3y, op: ">=", value: -0.35}
```

### Nordic momentum — strong trend near the high

```yaml
name: nordic_momentum
description: Nordic equities in a steady uptrend, close to their 52-week high
steps:
  - label: Nordic main exchanges
    attr: exchange
    op: in
    value: [HE, ST, CO]
  - {attr: type, op: ==, value: Common Stock}
  - label: tradable
    metric: avg_value_traded_eur_3m
    op: ">="
    value: 500000
  - label: strong year
    metric: perf_1y
    op: ">="
    value: 0.30
  - label: still rising
    all_of:
      - {metric: perf_6m, op: ">", value: 0}
      - {metric: perf_3m, op: ">", value: 0}
  - label: within 10 % of the 52-week high
    metric: drawdown_52w
    op: ">="
    value: -0.10
  - label: not a lottery ticket
    metric: volatility_1y
    op: "<="
    value: 0.45
```

### Helsinki dividend top 10

```yaml
name: he_dividend_top10
show: [sector, dividend_yield, fcf_payout_ratio]
steps:
  - {attr: exchange, op: ==, value: HE}
  - {attr: type, op: ==, value: Common Stock}
  - label: 10 highest dividend yields
    metric: dividend_yield
    top: 10
```

### 15 best performers of the last year, tradable only

```yaml
name: perf_top15
steps:
  - {attr: type, op: ==, value: Common Stock}
  - {metric: market_cap_eur, op: ">=", value: 1000000000}
  - label: tradable
    metric: avg_value_traded_eur_3m
    op: ">="
    value: 1000000
  - label: 15 best 1-year returns
    metric: perf_1y
    top: 15
```

### Low-risk large caps, US and Europe, with a short-history escape

```yaml
name: low_risk_large
steps:
  - label: US and Xetra listings
    attr: exchange
    op: in
    value: [US, XETRA]
  - {metric: market_cap_eur, op: ">=", value: 20000000000}
  - label: recent price data
    metric: last_price_date
    op: ">="
    value: "2026-09-01"
  - label: low tail risk, or too new to tell but calm
    any_of:
      - {metric: cvar_95, op: "<=", value: 0.035}
      - all_of:
          - {metric: n_obs, op: "<", value: 500}
          - {metric: volatility_1y, op: "<=", value: 0.20}
```
