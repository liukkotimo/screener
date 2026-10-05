-- Base data: investment items, daily prices, dividends, FX rates and a fetch log.
-- Dates are ISO 'YYYY-MM-DD' text, timestamps ISO UTC text.

CREATE TABLE item (
    item_id            INTEGER PRIMARY KEY,
    ticker             TEXT NOT NULL UNIQUE,      -- Yahoo symbol, e.g. NOKIA.HE
    name               TEXT,
    type               TEXT,                      -- Yahoo quoteType: EQUITY, ETF, MUTUALFUND, ...
    exchange           TEXT,                      -- Yahoo exchange code: HEL, STO, NYQ, LSE, ...
    exchange_name      TEXT,                      -- e.g. Helsinki
    country            TEXT,
    currency           TEXT,                      -- quote currency as Yahoo gives it, may be minor unit (GBp)
    sector             TEXT,
    industry           TEXT,
    market_cap         REAL,                      -- Yahoo marketCap, in the MAJOR currency (GBP for GBp quotes)
    total_assets       REAL,                      -- funds/ETFs: Yahoo totalAssets (major currency)
    shares_outstanding REAL,
    market_cap_date    TEXT,                      -- date market_cap was fetched
    source             TEXT,                      -- where the ticker came from, e.g. csv:test20, yahoo_screener:fi
    active             INTEGER NOT NULL DEFAULT 1,
    added_at           TEXT NOT NULL,
    info_updated_at    TEXT
);

-- Full Yahoo info dict of the latest successful fetch, for debugging and adding attributes later.
CREATE TABLE item_info_raw (
    item_id    INTEGER PRIMARY KEY REFERENCES item(item_id) ON DELETE CASCADE,
    fetched_at TEXT NOT NULL,
    info_json  TEXT NOT NULL
);

-- Prices in the item's quote currency/unit. close is split-adjusted, adj_close also dividend-adjusted.
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

-- Dividend per share in the item's quote currency/unit (split-adjusted by Yahoo).
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

-- Outcome of the latest fetch per (kind, key). kind: info / price / fx; key: ticker or currency.
CREATE TABLE fetch_log (
    kind            TEXT NOT NULL,
    key             TEXT NOT NULL,
    last_attempt_at TEXT NOT NULL,
    last_success_at TEXT,
    last_error      TEXT,
    PRIMARY KEY (kind, key)
) WITHOUT ROWID;
