-- Longer price history, relative strength, multi-year fundamentals and valuation history.

-- Year-end common shares outstanding (EODHD Balance_Sheet commonStockSharesOutstanding). EODHD reports
-- them adjusted for the splits known at fetch time; filled by the next `update fundamentals`.
ALTER TABLE fundamental_annual ADD COLUMN shares_outstanding REAL;

-- Stock splits from the price updates: ratio = new shares per old share (4.0 for a 4:1 split).
CREATE TABLE split (
    item_id INTEGER NOT NULL REFERENCES item(item_id) ON DELETE CASCADE,
    date    TEXT NOT NULL,
    ratio   REAL NOT NULL,
    PRIMARY KEY (item_id, date)
) WITHOUT ROWID;

ALTER TABLE item_metrics ADD COLUMN perf_5y                    REAL;
ALTER TABLE item_metrics ADD COLUMN cagr_5y                    REAL;
ALTER TABLE item_metrics ADD COLUMN cagr_10y                   REAL;
ALTER TABLE item_metrics ADD COLUMN drawdown_5y                REAL;
ALTER TABLE item_metrics ADD COLUMN drawdown_available_history REAL;
ALTER TABLE item_metrics ADD COLUMN max_drawdown_5y            REAL;
ALTER TABLE item_metrics ADD COLUMN max_drawdown_10y           REAL;
ALTER TABLE item_metrics ADD COLUMN days_since_low_3y          INTEGER;
ALTER TABLE item_metrics ADD COLUMN recovery_from_low_3y       REAL;
ALTER TABLE item_metrics ADD COLUMN volatility_3y              REAL;
ALTER TABLE item_metrics ADD COLUMN downside_volatility_3y     REAL;
ALTER TABLE item_metrics ADD COLUMN ulcer_index_3y             REAL;
ALTER TABLE item_metrics ADD COLUMN ma_200_distance            REAL;
ALTER TABLE item_metrics ADD COLUMN ma_200_slope_3m            REAL;
ALTER TABLE item_metrics ADD COLUMN relative_strength_market_6m REAL;
ALTER TABLE item_metrics ADD COLUMN relative_strength_world_6m REAL;
ALTER TABLE item_metrics ADD COLUMN roic_avg_10y               REAL;
ALTER TABLE item_metrics ADD COLUMN roic_min_5y                REAL;
ALTER TABLE item_metrics ADD COLUMN roic_std_5y                REAL;
ALTER TABLE item_metrics ADD COLUMN ebit_margin_avg_5y         REAL;
ALTER TABLE item_metrics ADD COLUMN ebit_margin_trend_3y       REAL;
ALTER TABLE item_metrics ADD COLUMN fcf_positive_years_10y     INTEGER;
ALTER TABLE item_metrics ADD COLUMN fcf_margin_avg_5y          REAL;
ALTER TABLE item_metrics ADD COLUMN revenue_cagr_5y            REAL;
ALTER TABLE item_metrics ADD COLUMN eps_cagr_5y                REAL;
ALTER TABLE item_metrics ADD COLUMN shares_outstanding_cagr_5y REAL;
ALTER TABLE item_metrics ADD COLUMN net_debt_ebitda_max_5y     REAL;
ALTER TABLE item_metrics ADD COLUMN interest_coverage_min_5y   REAL;
ALTER TABLE item_metrics ADD COLUMN ev_ebit_median_5y          REAL;
ALTER TABLE item_metrics ADD COLUMN ev_ebit_percentile_5y      REAL;
ALTER TABLE item_metrics ADD COLUMN fcf_yield_median_5y        REAL;
ALTER TABLE item_metrics ADD COLUMN fcf_yield_percentile_5y    REAL;
ALTER TABLE item_metrics ADD COLUMN pe_percentile_5y           REAL;
ALTER TABLE item_metrics ADD COLUMN ev_sales_percentile_5y     REAL;
