-- Quarterly statements (EODHD Financials::*::quarterly) and trailing-twelve-month (TTM) metrics.

-- Same columns as fundamental_annual, one row per fiscal quarter; filled by the next `update fundamentals`.
CREATE TABLE fundamental_quarterly (
    item_id                 INTEGER NOT NULL REFERENCES item(item_id) ON DELETE CASCADE,
    quarter_end             TEXT NOT NULL,
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
    shares_outstanding      REAL,
    PRIMARY KEY (item_id, quarter_end)
) WITHOUT ROWID;

ALTER TABLE item_metrics ADD COLUMN roic_ttm              REAL;
ALTER TABLE item_metrics ADD COLUMN ebit_margin_ttm       REAL;
ALTER TABLE item_metrics ADD COLUMN fcf_margin_ttm        REAL;
ALTER TABLE item_metrics ADD COLUMN net_debt_ebitda_ttm   REAL;
ALTER TABLE item_metrics ADD COLUMN interest_coverage_ttm REAL;
ALTER TABLE item_metrics ADD COLUMN ev_ebit_ttm           REAL;
ALTER TABLE item_metrics ADD COLUMN pe_ttm                REAL;
ALTER TABLE item_metrics ADD COLUMN fcf_yield_ttm         REAL;
ALTER TABLE item_metrics ADD COLUMN revenue_growth_ttm    REAL;
ALTER TABLE item_metrics ADD COLUMN eps_growth_ttm        REAL;
ALTER TABLE item_metrics ADD COLUMN ttm_quarter_end       TEXT;
