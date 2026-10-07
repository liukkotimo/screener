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
steps:                          # required, non-empty list
  - {attr: type, op: ==, value: EQUITY}
  - label: shown in output      # optional on any step
    metric: market_cap_eur
    op: ">="
    value: 1000000000
  - label: either condition
    any_of:                     # OR; all_of = AND. Groups can be nested.
      - {metric: perf_1y, op: "<=", value: -0.25}
      - {metric: perf_3y, op: "<=", value: -0.40}
```

No other top-level or step keys are allowed; unknown keys, attributes, metrics or operators are rejected
with an error listing the valid choices.

A step is either a **condition** (exactly one of `attr` / `metric`, plus `op` and `value`) or a **group**
(exactly one of `any_of` / `all_of` with a non-empty list of conditions or groups). `label` is only
meaningful at step level.

## Operators

| op | value | notes |
|---|---|---|
| `<` `<=` `>` `>=` | number | metrics need a number (except `last_price_date` and `fiscal_year_end`, which take an ISO date string) |
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

## Attributes (`attr:`)

Text columns of the `item` table, from Yahoo's info. Comparison is **case-sensitive** and must match Yahoo's
spelling exactly.

| attr | meaning | example values |
|---|---|---|
| `ticker` | Yahoo symbol | `NOKIA.HE`, `SHEL.L`, `AAPL` |
| `name` | company / fund name | |
| `type` | Yahoo quoteType | `EQUITY`, `ETF`, `MUTUALFUND` |
| `exchange` | Yahoo exchange code | `HEL`, `STO`, `CPH`, `NYQ`, `NMS`, `GER` (XETRA), `FRA`, `LSE`, `PNK` (OTC) |
| `exchange_name` | readable exchange | `Helsinki`, `NYSE`, `NasdaqGS`, `XETRA` |
| `country` | company domicile | `Finland`, `United States`, `United Kingdom` |
| `currency` | quote currency (may be a minor unit) | `EUR`, `USD`, `SEK`, `GBp` |
| `sector` | Yahoo sector | `Basic Materials`, `Communication Services`, `Consumer Cyclical`, `Consumer Defensive`, `Energy`, `Financial Services`, `Healthcare`, `Industrials`, `Real Estate`, `Technology`, `Utilities` |
| `industry` | Yahoo industry (finer than sector) | `Banks - Regional`, `Software - Application` |
| `source` | how the ticker entered the universe | `csv:test20`, `yahoo_screener:fi` |

Tip: the same company is often listed on several exchanges (e.g. German regional exchanges `FRA`, `STU`,
`MUN`, `DUS` plus OTC `PNK` in the US). `screener dedupe --apply` keeps one listing per company (see README);
otherwise filter on `exchange` early to avoid duplicates and illiquid secondary listings. Funds/ETFs usually have no `sector`.

To check actual values in your database: `screener status TICKER` shows one item's attributes.

## Metrics (`metric:`)

Columns of `item_metrics`, calculated per item for an as-of date (default: the latest calculated date).
Any column added to that table is automatically usable.

Rules for all metrics:
- **Fractions, not percent**: `-0.25` = −25 %, `0.05` = 5 %.
- Returns, drawdowns and risk use `adj_close` (dividends and splits included). Returns are plain total
  return over the window, **not annualized**.
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

### Drawdown and timing

| metric | definition | range |
|---|---|---|
| `drawdown_52w` | last price vs. 52-week high: `last / max − 1` | ≤ 0 (0 = at the high) |
| `drawdown_3y` | last price vs. 3-year high | ≤ 0 |
| `max_drawdown_3y` | worst peak-to-trough fall within 3 years | ≤ 0 |
| `days_since_low_52w` | calendar days since the lowest price of the last 52 weeks | 0 (at the low now) … ~365 |

`drawdown_*` is *where the price is now* relative to the high; `max_drawdown_3y` is the *worst fall that
happened* in the window, even if fully recovered since.

### Risk

| metric | definition | typical range |
|---|---|---|
| `volatility_1y` | annualized std. dev. of daily returns over 1 year (needs ≥ 200 returns) | 0.15 (calm) … 0.6+ (wild) |
| `cvar_95` | average of the worst 5 % daily returns over 3 years, as a **positive** loss (needs ≥ 500 returns) | 0.03 … 0.10 |

Note the sign: lower `cvar_95` = less tail risk, so filter with `<=`.

### Size, liquidity, price

| metric | definition |
|---|---|
| `market_cap` | Yahoo market cap scaled to as-of by the price change, in the major quote currency (GBP, not GBp) |
| `market_cap_eur` | `market_cap` in EUR — use this for cross-market comparison |
| `total_assets_eur` | fund/ETF total assets in EUR (funds usually have no market cap) |
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

From Yahoo's annual income statement, balance sheet and cash flow (`screener update fundamentals`), equities
only. Rules:
- Only **annual** figures (no trailing twelve months). The latest fiscal year used is the newest one that ended
  at least 90 days before as-of (assumed published by then); it is shown in `fiscal_year_end`. If that year is
  more than ~18 months old, all fundamental metrics are NULL.
- Ratios within one statement need no currency conversion. Valuation metrics compare statement figures
  (in Yahoo's `financialCurrency`, e.g. USD for Shell) with market cap / price in EUR at the as-of FX rate.
- Values that make a ratio meaningless (EBIT or EBITDA ≤ 0, revenue ≤ 0, invested capital ≤ 0) give NULL.
- Yahoo has about 4 fiscal years; stored years are kept, so history grows over time.

| metric | definition | typical range |
|---|---|---|
| `roic` | EBIT × (1 − tax rate) / (equity + total debt − cash). Tax rate = tax / pretax income, limited to 0 … 0.4; 0 for a loss | 0.05 … 0.30 |
| `roic_avg_4y` | mean `roic` of the latest 4 fiscal years; NULL unless all 4 are computable | |
| `ebit_margin` | EBIT / revenue | 0.05 … 0.30 |
| `ebit_margin_change_1y` | `ebit_margin` minus the previous year's (`0.03` = +3 percentage points) | −0.05 … 0.05 |
| `revenue_cagr_3y` | annual revenue growth rate over 3 fiscal years | −0.1 … 0.3 |
| `fcf_margin` | free cash flow / revenue | |
| `net_debt_ebitda` | (total debt − cash and short-term investments) / EBITDA; negative = net cash | < 0 … 4 |
| `interest_coverage` | EBIT / interest expense. **999** = interest expense reported as 0; NULL if not reported | 3 … 50 |
| `pe_forward` | price / Yahoo's forward EPS (analyst estimate). NULL if EPS ≤ 0 or the as-of date is more than 31 days from the item info fetch (the estimate is a snapshot) | 8 … 40 |
| `ev_ebit` | (market cap + net debt) / EBIT. Minority interests are ignored | 5 … 30 |
| `fcf_yield` | free cash flow / market cap | 0 … 0.10 |
| `fcf_payout_ratio` | dividends paid / free cash flow; NULL if FCF ≤ 0 or no dividend line reported | 0 … 1 |

Tips: `pe_forward` is computed by us with both sides in EUR; Yahoo's own `forwardPE` mixes currencies when
quote and reporting currency differ (e.g. GBp price / USD EPS). Many companies with little or no debt do not
report interest expense, so `interest_coverage` is NULL for them: combine it with `net_debt_ebitda` in an
`any_of`. Banks and insurers have no meaningful EBIT/EBITDA or net debt; exclude `Financial Services` when
screening on these.

### Data quality

| metric | definition |
|---|---|
| `last_price_date` | date of the last price on or before as-of (ISO text, e.g. `"2026-09-30"`) |
| `n_obs` | number of stored daily prices up to as-of |
| `fiscal_year_end` | end of the latest fiscal year used by the fundamental metrics (ISO text); NULL = no usable statements |

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
    value: [EQUITY]
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
  - {attr: type, op: ==, value: EQUITY}
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
    value: [HEL, STO, CPH]
  - {attr: type, op: ==, value: EQUITY}
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

### Low-risk large caps, US and Europe, with a short-history escape

```yaml
name: low_risk_large
steps:
  - label: primary US and German listings
    attr: exchange
    op: in
    value: [NYQ, NMS, GER]
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
