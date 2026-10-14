-- Schema for EODHD data: investment items, daily prices, dividends, FX rates, annual statements, metrics and
-- screening runs. Dates are ISO 'YYYY-MM-DD' text, timestamps ISO UTC text.
-- (Migrations 001-005 of the Yahoo era were squashed into this file when the data source changed.)

CREATE TABLE item (
    item_id                 INTEGER PRIMARY KEY,
    ticker                  TEXT NOT NULL UNIQUE,  -- EODHD symbol CODE.EXCHANGE, e.g. NOKIA.HE, VOLV-B.ST
    name                    TEXT,
    type                    TEXT,                  -- EODHD type: Common Stock, Preferred Stock, ETF, FUND, ...
    exchange                TEXT,                  -- EODHD exchange code (the symbol suffix): HE, ST, ...
    exchange_name           TEXT,                  -- e.g. Helsinki Exchange
    country                 TEXT,
    currency                TEXT,                  -- quote currency as EODHD gives it, may be minor unit (GBX)
    isin                    TEXT,
    sector                  TEXT,                  -- EODHD General.Sector, e.g. Industrials, Financial Services
    industry                TEXT,
    market_cap              REAL,                  -- in the MAJOR currency (GBP for GBX quotes)
    market_cap_date         TEXT,                  -- trading day market_cap refers to
    total_assets            REAL,                  -- ETFs: EODHD ETF_Data.TotalAssets
    shares_outstanding      REAL,
    financial_currency      TEXT,                  -- unit of the statements and forward_eps
    forward_eps             REAL,                  -- consensus EPS estimate for the next fiscal year (Earnings::Trend '+1y')
    forward_eps_period      TEXT,                  -- fiscal year end that forward_eps is for
    forward_eps_date        TEXT,                  -- date forward_eps was fetched (it is a snapshot)
    source                  TEXT,                  -- where the ticker came from, e.g. csv:test20, eodhd:HE
    active                  INTEGER NOT NULL DEFAULT 1,
    duplicate_of            INTEGER REFERENCES item(item_id) ON DELETE SET NULL,  -- listing kept instead (dedupe)
    added_at                TEXT NOT NULL,
    info_updated_at         TEXT,                  -- name/market cap refreshed from the symbol list / bulk call
    fundamentals_updated_at TEXT
);

-- General, Highlights and Valuation of the latest fundamentals response, for debugging and adding attributes later.
CREATE TABLE item_info_raw (
    item_id    INTEGER PRIMARY KEY REFERENCES item(item_id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    info_json  TEXT NOT NULL
);

-- Prices in the item's quote currency/unit. close is split-adjusted (computed from EODHD's raw close and the
-- split history), adj_close is EODHD's adjusted_close (splits and dividends). volume is split-adjusted.
CREATE TABLE price_daily (
    item_id   INTEGER NOT NULL REFERENCES item(item_id) ON DELETE CASCADE,
    date      TEXT NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    adj_close REAL,
    volume    REAL,
    PRIMARY KEY (item_id, date)
) WITHOUT ROWID;

-- Dividend per share in the item's quote currency/unit (split-adjusted like close).
CREATE TABLE dividend (
    item_id INTEGER NOT NULL REFERENCES item(item_id) ON DELETE CASCADE,
    ex_date TEXT NOT NULL,
    amount  REAL NOT NULL,
    PRIMARY KEY (item_id, ex_date)
) WITHOUT ROWID;

-- EUR value of one unit of a MAJOR currency (USD, GBP, SEK...). EUR itself is 1 and not stored.
CREATE TABLE fx_rate_daily (
    currency     TEXT NOT NULL,
    date         TEXT NOT NULL,
    eur_per_unit REAL NOT NULL,
    PRIMARY KEY (currency, date)
) WITHOUT ROWID;

-- Outcome of the latest fetch per (kind, key). kind: price / fx / fundamentals; key: ticker or currency.
CREATE TABLE fetch_log (
    kind            TEXT NOT NULL,
    key             TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    last_error      TEXT,
    PRIMARY KEY (kind, key)
) WITHOUT ROWID;

-- Annual statement figures in the statement currency (major unit). NULL = not reported.
-- Rows are never deleted; a restatement overwrites. ebit = pretax_income + interest_expense,
-- ebitda = ebit + depreciation and amortization (derived, see market_data/eodhd.py).
-- Signs: interest_expense, capital_expenditure and dividends_paid are stored as positive amounts.
CREATE TABLE fundamental_annual (
    item_id                 INTEGER NOT NULL REFERENCES item(item_id) ON DELETE CASCADE,
    fiscal_year_end         TEXT NOT NULL,
    currency                TEXT,
    revenue                 REAL,
    ebit                    REAL,
    ebitda                  REAL,
    pretax_income           REAL,
    tax_provision           REAL,
    interest_expense        REAL,
    net_income              REAL,
    operating_cash_flow     REAL,
    capital_expenditure     REAL,
    free_cash_flow          REAL,
    dividends_paid          REAL,
    total_debt              REAL,
    cash_and_st_investments REAL,
    stockholders_equity     REAL,
    PRIMARY KEY (item_id, fiscal_year_end)
) WITHOUT ROWID;

-- One row per item and as-of date. NULL = not computable (history too short, stale or missing data),
-- never a substitute 0. Definitions: see screener/metrics.py (METRICS) and README.
CREATE TABLE item_metrics (
    item_id                 INTEGER NOT NULL REFERENCES item(item_id) ON DELETE CASCADE,
    asof_date               TEXT NOT NULL,
    last_price_date         TEXT,
    n_obs                   INTEGER,
    perf_1m                 REAL,
    perf_3m                 REAL,
    perf_6m                 REAL,
    perf_1y                 REAL,
    perf_3y                 REAL,
    drawdown_52w            REAL,
    drawdown_3y             REAL,
    days_since_low_52w      INTEGER,
    max_drawdown_3y         REAL,
    volatility_1y           REAL,
    cvar_95                 REAL,
    price_eur               REAL,
    market_cap              REAL,
    market_cap_eur          REAL,
    total_assets_eur        REAL,
    avg_value_traded_eur_3m REAL,
    dividend_yield          REAL,
    roic                    REAL,
    roic_avg_4y             REAL,
    ebit_margin             REAL,
    ebit_margin_change_1y   REAL,
    revenue_cagr_3y         REAL,
    fcf_margin              REAL,
    net_debt_ebitda         REAL,
    interest_coverage       REAL,
    pe_forward              REAL,
    ev_ebit                 REAL,
    fcf_yield               REAL,
    fcf_payout_ratio        REAL,
    fiscal_year_end         TEXT,
    calculated_at           TEXT NOT NULL,
    PRIMARY KEY (item_id, asof_date)
) WITHOUT ROWID;

CREATE INDEX item_metrics_asof ON item_metrics (asof_date);

-- Screening runs. Every candidate of every step is stored with its tested value(s) and outcome,
-- so "why was X eliminated?" is a simple lookup.
CREATE TABLE screen_run (
    run_id          INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,             -- profile name
    created_at      TEXT NOT NULL,
    asof_date       TEXT,                      -- item_metrics as-of date used
    profile_path    TEXT,
    profile_yaml    TEXT NOT NULL,             -- profile text exactly as run
    parent_run_id   INTEGER REFERENCES screen_run(run_id) ON DELETE SET NULL,
    rerun_from_step INTEGER,                   -- steps before this were copied from parent_run_id
    n_start         INTEGER,                   -- candidates entering step 1
    n_survivors     INTEGER
);

CREATE TABLE screen_step (
    run_id          INTEGER NOT NULL REFERENCES screen_run(run_id) ON DELETE CASCADE,
    step_no         INTEGER NOT NULL,          -- 1-based
    definition_json TEXT NOT NULL,             -- normalized step definition (used to validate re-runs)
    description     TEXT NOT NULL,             -- human readable, e.g. "perf_3m between [-0.05, 0.05]"
    n_in            INTEGER NOT NULL,
    n_passed        INTEGER NOT NULL,
    PRIMARY KEY (run_id, step_no)
) WITHOUT ROWID;

CREATE TABLE screen_step_item (
    run_id     INTEGER NOT NULL,
    step_no    INTEGER NOT NULL,
    item_id    INTEGER NOT NULL REFERENCES item(item_id) ON DELETE CASCADE,
    value_json TEXT,                           -- tested value(s), e.g. {"perf_1y": -0.41, "perf_3y": null}
    passed     INTEGER NOT NULL,               -- 1 / 0
    reason     TEXT,                           -- NULL when passed; "missing data: ..." or what failed
    PRIMARY KEY (run_id, step_no, item_id),
    FOREIGN KEY (run_id, step_no) REFERENCES screen_step(run_id, step_no) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX screen_step_item_item ON screen_step_item (run_id, item_id);
