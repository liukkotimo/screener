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
    calculated_at           TEXT NOT NULL,
    PRIMARY KEY (item_id, asof_date)
) WITHOUT ROWID;

CREATE INDEX item_metrics_asof ON item_metrics (asof_date);
