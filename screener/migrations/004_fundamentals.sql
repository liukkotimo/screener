-- Annual financial statements (Yahoo) and the first fundamental metrics.

ALTER TABLE item ADD COLUMN financial_currency TEXT;       -- Yahoo financialCurrency: unit of statements and forward_eps
ALTER TABLE item ADD COLUMN forward_eps REAL;              -- Yahoo forwardEps (snapshot at info_updated_at), financial currency
ALTER TABLE item ADD COLUMN fundamentals_updated_at TEXT;

-- Annual statement figures in the item's financial currency (major unit). NULL = not reported.
-- Rows are never deleted, so history grows beyond Yahoo's ~4 years over time; a restatement overwrites.
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
    diluted_eps             REAL,
    operating_cash_flow     REAL,
    capital_expenditure     REAL,
    free_cash_flow          REAL,
    dividends_paid          REAL,
    total_debt              REAL,
    cash_and_st_investments REAL,
    stockholders_equity     REAL,
    PRIMARY KEY (item_id, fiscal_year_end)
) WITHOUT ROWID;

ALTER TABLE item_metrics ADD COLUMN roic REAL;
ALTER TABLE item_metrics ADD COLUMN roic_avg_4y REAL;
ALTER TABLE item_metrics ADD COLUMN ebit_margin REAL;
ALTER TABLE item_metrics ADD COLUMN ebit_margin_change_1y REAL;
ALTER TABLE item_metrics ADD COLUMN revenue_cagr_3y REAL;
ALTER TABLE item_metrics ADD COLUMN fcf_margin REAL;
ALTER TABLE item_metrics ADD COLUMN net_debt_ebitda REAL;
ALTER TABLE item_metrics ADD COLUMN interest_coverage REAL;
ALTER TABLE item_metrics ADD COLUMN pe_forward REAL;
ALTER TABLE item_metrics ADD COLUMN ev_ebit REAL;
ALTER TABLE item_metrics ADD COLUMN fcf_yield REAL;
ALTER TABLE item_metrics ADD COLUMN fcf_payout_ratio REAL;
ALTER TABLE item_metrics ADD COLUMN fiscal_year_end TEXT;
