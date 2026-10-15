"""
Calculating item_metrics rows from stored prices, dividends and FX rates.

compute_metrics() is pure (DataFrames in, dict out); calc_metrics() does the database work around it.
"""
import logging
import sqlite3

import pandas as pd

from screener import benchmarks as bm
from screener.analytics import fundamentals as fa
from screener.analytics import valuation as va
from screener.analytics.dividends import trailing_yield
from screener.analytics.relative import relative_strength
from screener.analytics.returns import (cagr, days_since_low, drawdown_from_high, drawdown_from_peak, max_drawdown,
                                        performance, recovery_from_low, value_at, window)
from screener.analytics.risk import cvar, downside_volatility, ulcer_index, volatility
from screener.analytics.trend import ma_distance, ma_slope
from screener.db import now_utc
from screener.market_data.eodhd import major_currency

logger = logging.getLogger(__name__)

# Metric column -> definition. Shown by `screener metrics --list` and used in explanations.
METRICS = {
    'perf_1m': 'total return over 1 month (adj_close, not annualized)',
    'perf_3m': 'total return over 3 months',
    'perf_6m': 'total return over 6 months',
    'perf_1y': 'total return over 1 year',
    'perf_3y': 'total return over 3 years (not annualized)',
    'perf_5y': 'total return over 5 years (not annualized)',
    'cagr_5y': 'annual growth rate of adj_close over 5 years: (1 + perf_5y) ** (1/5) - 1',
    'cagr_10y': 'annual growth rate of adj_close over 10 years',
    'drawdown_52w': 'adj_close vs. its 52-week high: last / max - 1 (<= 0)',
    'drawdown_3y': 'adj_close vs. its 3-year high: last / max - 1 (<= 0)',
    'drawdown_5y': 'adj_close vs. its 5-year high: last / max - 1 (<= 0)',
    'drawdown_available_history': 'adj_close vs. the high of all stored history (<= 0; min 1 year of history)',
    'days_since_low_52w': 'calendar days since the lowest adj_close of the last 52 weeks',
    'days_since_low_3y': 'calendar days since the lowest adj_close of the last 3 years',
    'recovery_from_low_3y': 'adj_close vs. its 3-year low: last / min - 1 (>= 0)',
    'max_drawdown_3y': 'worst peak-to-trough fall of adj_close within 3 years (<= 0)',
    'max_drawdown_5y': 'worst peak-to-trough fall of adj_close within 5 years (<= 0)',
    'max_drawdown_10y': 'worst peak-to-trough fall of adj_close within 10 years (<= 0)',
    'volatility_1y': 'annualized std. dev. of daily returns over 1 year (min 200 returns)',
    'volatility_3y': 'annualized std. dev. of daily returns over 3 years (min 500 returns)',
    'downside_volatility_3y': 'sqrt(mean(min(daily return, 0) ** 2)) * sqrt(252) over 3 years (min 500 returns)',
    'ulcer_index_3y': 'sqrt(mean(drawdown ** 2)), daily drawdown from the running 3-year high (min 500 returns)',
    'cvar_95': 'avg of the worst 5% daily returns over 3 years, as positive loss (min 500 returns)',
    'ma_200_distance': 'adj_close / its 200-day moving average - 1',
    'ma_200_slope_3m': '200-day moving average now / 3 months ago - 1 (both from 200 full days)',
    'relative_strength_market_6m': 'perf_6m minus the home-market benchmark return over the same dates, in the '
                                   'item currency (benchmarks.yaml, by exchange)',
    'relative_strength_world_6m': 'perf_6m minus the world benchmark return (MSCI World ETF), in the item currency',
    'price_eur': 'last close in EUR',
    'market_cap': 'EODHD market cap (last-day bulk) scaled to as-of by the close price change, major currency',
    'market_cap_eur': 'market_cap in EUR at the as-of FX rate',
    'total_assets_eur': 'ETF total assets (EODHD, latest) in EUR at the as-of FX rate',
    'avg_value_traded_eur_3m': 'average daily close * volume over 3 months, in EUR (NULL if no volume data)',
    'dividend_yield': 'dividends (ex-date) over the last 12 months / close',
    'roic': 'EBIT * (1 - tax rate) / (equity + net debt), latest fiscal year',
    'roic_avg_4y': 'mean roic of the latest 4 consecutive fiscal years (all 4 required)',
    'roic_avg_10y': 'mean roic of the latest 10 consecutive fiscal years (all 10 required)',
    'roic_min_5y': 'lowest roic of the latest 5 consecutive fiscal years (all 5 required)',
    'roic_std_5y': 'population std. dev. of roic over the latest 5 consecutive fiscal years (all 5 required)',
    'ebit_margin': 'EBIT / revenue, latest fiscal year',
    'ebit_margin_change_1y': 'ebit_margin minus the previous year\'s (0.03 = +3 percentage points)',
    'ebit_margin_avg_5y': 'mean ebit_margin of the latest 5 consecutive fiscal years (all 5 required)',
    'ebit_margin_trend_3y': '(ebit_margin - ebit_margin 3 fiscal years earlier) / 3, per year',
    'revenue_cagr_3y': 'annual revenue growth rate over 3 fiscal years',
    'revenue_cagr_5y': 'annual revenue growth rate over 5 fiscal years',
    'eps_cagr_5y': 'annual growth rate of EPS (net income / year-end shares) over 5 fiscal years (NULL if '
                   'either EPS <= 0)',
    'shares_outstanding_cagr_5y': 'annual growth rate of year-end shares outstanding over 5 fiscal years '
                                  '(split-adjusted; < 0 = buybacks)',
    'fcf_margin': 'free cash flow / revenue, latest fiscal year',
    'fcf_margin_avg_5y': 'mean fcf_margin of the latest 5 consecutive fiscal years (all 5 required)',
    'fcf_positive_years_10y': 'number of the latest 10 consecutive fiscal years with free cash flow > 0',
    'net_debt_ebitda': '(total debt - cash and short-term investments) / EBITDA (NULL if EBITDA <= 0)',
    'net_debt_ebitda_max_5y': 'highest net_debt_ebitda of the latest 5 consecutive fiscal years (all 5 required)',
    'interest_coverage': 'EBIT / interest expense (999 = interest expense reported as 0)',
    'interest_coverage_min_5y': 'lowest interest_coverage of the latest 5 consecutive fiscal years (all 5 required)',
    'pe_forward': 'price / consensus EPS estimate for the next fiscal year (NULL if EPS <= 0 or the estimate '
                  'was fetched > 100 days from as-of)',
    'ev_ebit': '(market cap + net debt) / EBIT (NULL if EBIT <= 0)',
    'fcf_yield': 'free cash flow / market cap',
    'fcf_payout_ratio': 'dividends paid / free cash flow (NULL if FCF <= 0)',
    'ev_ebit_median_5y': 'median EV/EBIT of 60 monthly samples over 5 years, each from the data of its date '
                         '(min 36 valid; EV/EBIT <= 0 excluded)',
    'ev_ebit_percentile_5y': 'share of the 5-year monthly EV/EBIT samples <= the current EV/EBIT (0..1; high = '
                             'expensive vs. own history)',
    'fcf_yield_median_5y': 'median FCF yield of 60 monthly samples over 5 years (negative values included)',
    'fcf_yield_percentile_5y': 'share of the 5-year monthly FCF yield samples <= the current one (0..1; high = '
                               'CHEAP vs. own history)',
    'pe_percentile_5y': 'share of the 5-year monthly P/E samples (net income > 0) <= the current P/E (0..1)',
    'ev_sales_percentile_5y': 'share of the 5-year monthly EV/Sales samples <= the current EV/Sales (0..1)',
    'fiscal_year_end': 'end of the latest fiscal year used (public >= 90 days before as-of, at most ~18 months old)',
    'last_price_date': 'date of the last price on or before as-of',
    'n_obs': 'number of stored prices on or before as-of',
}

# valuation vs. own history (analytics/valuation.py): metric -> (ratio, statistic)
VALUATION_HISTORY = {
    'ev_ebit_median_5y': ('ev_ebit', va.median),
    'ev_ebit_percentile_5y': ('ev_ebit', va.percentile),
    'fcf_yield_median_5y': ('fcf_yield', va.median),
    'fcf_yield_percentile_5y': ('fcf_yield', va.percentile),
    'pe_percentile_5y': ('pe', va.percentile),
    'ev_sales_percentile_5y': ('ev_sales', va.percentile),
}
VALUATION_HISTORY_METRICS = tuple(VALUATION_HISTORY)
# metrics from annual statements (plus pe_forward), all NULL when there are no usable statements
FUNDAMENTAL_METRICS = ('roic', 'roic_avg_4y', 'roic_avg_10y', 'roic_min_5y', 'roic_std_5y', 'ebit_margin',
                       'ebit_margin_change_1y', 'ebit_margin_avg_5y', 'ebit_margin_trend_3y', 'revenue_cagr_3y',
                       'revenue_cagr_5y', 'eps_cagr_5y', 'shares_outstanding_cagr_5y', 'fcf_margin',
                       'fcf_margin_avg_5y', 'fcf_positive_years_10y', 'net_debt_ebitda', 'net_debt_ebitda_max_5y',
                       'interest_coverage', 'interest_coverage_min_5y', 'pe_forward', 'ev_ebit', 'fcf_yield',
                       'fcf_payout_ratio', 'fiscal_year_end', *VALUATION_HISTORY_METRICS)
FORWARD_EPS_MAX_AGE_DAYS = 100  # forward_eps is a snapshot (refreshed with fundamentals, every 90 days by default)
RS_MONTHS = 6


def compute_metrics(prices: pd.DataFrame, dividends: pd.Series, item: dict, fx: pd.Series | None,
                    asof, annual: pd.DataFrame | None = None, fx_fin: pd.Series | None = None,
                    benchmarks: dict | None = None, splits: pd.Series | None = None) -> dict:
    """
    prices     DataFrame indexed by date with close, adj_close, volume (quote units, e.g. pence), all history
    dividends  Series indexed by ex-date (quote units)
    item       dict with currency, market_cap, market_cap_date, market_cap_close (close on or before
               market_cap_date, from all stored prices: the snapshot may be newer than as-of), total_assets,
               financial_currency, forward_eps, forward_eps_date, fundamentals_updated_at
    fx         EUR per unit of the item's major currency, indexed by date; None for EUR items
    annual     annual statements (fundamental_annual columns) indexed by fiscal year end, in the financial
               currency; None or empty if there are none
    fx_fin     EUR per unit of the financial currency; None if EUR (or unknown)
    benchmarks {'market': ..., 'world': ...}: (adj_close, major currency, EUR per unit or None for EUR) of the
               item's benchmarks (load_benchmarks), or None where there is none
    splits     split ratios indexed by split date (empty or None if none)
    Returns {column: value} for item_metrics; None wherever a value cannot be computed.
    """
    asof = pd.Timestamp(asof)
    prices = prices[prices.index <= asof]
    close = prices['close']
    adj = prices['adj_close'] if prices['adj_close'].notna().any() else close
    major, unit_factor = major_currency(item.get('currency'))
    eur_per_unit = 1.0 if major == 'EUR' else (value_at(fx, asof) if fx is not None else None)

    def to_eur(v):  # v in major currency
        return None if v is None or eur_per_unit is None else v * eur_per_unit

    m = {
        'last_price_date': close.index[-1].strftime('%Y-%m-%d') if len(close) else None,
        'n_obs': int(close.notna().sum()),
        'perf_1m': performance(adj, asof, 1),
        'perf_3m': performance(adj, asof, 3),
        'perf_6m': performance(adj, asof, 6),
        'perf_1y': performance(adj, asof, 12),
        'perf_3y': performance(adj, asof, 36),
        'perf_5y': performance(adj, asof, 60),
        'cagr_5y': cagr(adj, asof, 60),
        'cagr_10y': cagr(adj, asof, 120),
        'drawdown_52w': drawdown_from_high(adj, asof, 12),
        'drawdown_3y': drawdown_from_high(adj, asof, 36),
        'drawdown_5y': drawdown_from_high(adj, asof, 60),
        'drawdown_available_history': drawdown_from_peak(adj, asof),
        'days_since_low_52w': days_since_low(adj, asof, 12),
        'days_since_low_3y': days_since_low(adj, asof, 36),
        'recovery_from_low_3y': recovery_from_low(adj, asof, 36),
        'max_drawdown_3y': max_drawdown(adj, asof, 36),
        'max_drawdown_5y': max_drawdown(adj, asof, 60),
        'max_drawdown_10y': max_drawdown(adj, asof, 120),
        'volatility_1y': volatility(adj, asof, 12),
        'volatility_3y': volatility(adj, asof, 36, min_obs=500),
        'downside_volatility_3y': downside_volatility(adj, asof, 36),
        'ulcer_index_3y': ulcer_index(adj, asof, 36),
        'cvar_95': cvar(adj, asof, 36),
        'ma_200_distance': ma_distance(adj, asof, 200),
        'ma_200_slope_3m': ma_slope(adj, asof, 3, 200),
        'dividend_yield': trailing_yield(dividends, close, asof, 12),
    }
    for kind in ('market', 'world'):
        m[f'relative_strength_{kind}_{RS_MONTHS}m'] = benchmark_rs(adj, (benchmarks or {}).get(kind), major, fx, asof)

    last_close = value_at(close, asof)
    m['price_eur'] = to_eur(last_close * unit_factor) if last_close is not None else None

    # The market cap is a snapshot from market_cap_date; scale it by the price change to as-of.
    market_cap = None
    if item.get('market_cap') and item.get('market_cap_close') and last_close is not None:
        market_cap = item['market_cap'] * last_close / item['market_cap_close']
    m['market_cap'] = market_cap
    m['market_cap_eur'] = to_eur(market_cap)
    m['total_assets_eur'] = to_eur(item.get('total_assets'))

    w = window(close, asof, 3)
    value_traded = None
    if w is not None:
        vol = prices['volume'].reindex(w.index.drop(w.index[0])).fillna(0)
        if vol.sum() > 0:
            value_traded = float((w.reindex(vol.index) * vol).mean()) * unit_factor
    m['avg_value_traded_eur_3m'] = to_eur(value_traded)

    years = statement_years(annual, item, splits, asof)
    m.update(fundamental_metrics(years, item, fx_fin, asof, m['market_cap_eur'], m['price_eur']))
    if years is not None:
        history = va.valuation_history(close * unit_factor, years, asof, 60, None if major == 'EUR' else fx,
                                       fx_fin_series(item, fx_fin))
        m.update({k: stat(history[ratio]) for k, (ratio, stat) in VALUATION_HISTORY.items()})
    return m


def benchmark_rs(adj: pd.Series, benchmark: tuple | None, currency: str | None, fx: pd.Series | None,
                 asof) -> float | None:
    """
    relative_strength against benchmark = (adj_close, currency, EUR per unit or None for EUR). In the same
    currency no FX is involved; otherwise both are converted via EUR, and a missing rate gives None.
    """
    if benchmark is None or currency is None:
        return None
    prices, b_currency, b_fx = benchmark
    if b_currency == currency:
        return relative_strength(adj, prices, asof, RS_MONTHS)
    if (currency != 'EUR' and fx is None) or (b_currency != 'EUR' and b_fx is None):
        return None  # FX rates missing
    return relative_strength(adj, prices, asof, RS_MONTHS, None if currency == 'EUR' else fx, b_fx)


def fx_fin_series(item: dict, fx_fin: pd.Series | None) -> pd.Series | None:
    """EUR per unit of the financial currency as a series, None for EUR. An unknown rate stays missing."""
    fin = major_currency(item.get('financial_currency'))[0]
    if fin == 'EUR':
        return None
    return fx_fin if fx_fin is not None else pd.Series(dtype=float)


def statement_years(annual: pd.DataFrame | None, item: dict, splits: pd.Series | None, asof) -> pd.DataFrame | None:
    """
    Fiscal years public at as-of (fundamentals.usable_years) with shares_outstanding on the split basis of the
    stored prices (fundamentals.shares_on_price_basis). None if there are none.
    """
    if annual is None or annual.empty:
        return None
    years = fa.usable_years(annual, asof)
    if years is None:
        return None
    splits = splits if splits is not None else pd.Series(dtype=float)
    return years.assign(shares_outstanding=fa.shares_on_price_basis(years, splits, item.get('fundamentals_updated_at')))


def fundamental_metrics(years: pd.DataFrame | None, item: dict, fx_fin: pd.Series | None, asof,
                        market_cap_eur: float | None, price_eur: float | None) -> dict:
    """
    Statement-based metrics from the years of statement_years(). Ratios within one statement need no FX;
    valuation compares statement figures with market cap / price, so both sides are converted to EUR at the
    as-of rate.
    """
    fin, fin_factor = major_currency(item.get('financial_currency'))
    eur_per_fin = None if fin is None else 1.0 if fin == 'EUR' else (
        value_at(fx_fin, asof) if fx_fin is not None else None)

    def fin_to_eur(v):
        return None if v is None or eur_per_fin is None else v * fin_factor * eur_per_fin

    def ratio(a, b):  # positive denominator required
        return None if a is None or b is None or b <= 0 else a / b

    def agg(func, values):
        return None if values is None else float(func(values))

    m = {'pe_forward': None}
    eps = item.get('forward_eps')
    snapshot = item.get('forward_eps_date')
    if eps and snapshot and abs((pd.Timestamp(asof) - pd.Timestamp(snapshot)).days) <= FORWARD_EPS_MAX_AGE_DAYS:
        m['pe_forward'] = ratio(price_eur, fin_to_eur(eps))

    if years is None:
        return {**dict.fromkeys(FUNDAMENTAL_METRICS), **m}
    last = years.iloc[-1]
    nd = fa.net_debt(last)
    ev = None if market_cap_eur is None or fin_to_eur(nd) is None else market_cap_eur + fin_to_eur(nd)
    return {
        **m,
        'fiscal_year_end': last.name.strftime('%Y-%m-%d'),
        'roic': fa.roic(last),
        'roic_avg_4y': fa.roic_avg(years, 4),
        'roic_avg_10y': fa.roic_avg(years, 10),
        'roic_min_5y': agg(min, fa.yearly_values(years, 5, fa.roic)),
        'roic_std_5y': fa.pstdev(fa.yearly_values(years, 5, fa.roic)),
        'ebit_margin': fa.ebit_margin(last),
        'ebit_margin_change_1y': fa.ebit_margin_change(years),
        'ebit_margin_avg_5y': fa.mean(fa.yearly_values(years, 5, fa.ebit_margin)),
        'ebit_margin_trend_3y': fa.ebit_margin_trend(years, 3),
        'revenue_cagr_3y': fa.revenue_cagr(years, 3),
        'revenue_cagr_5y': fa.revenue_cagr(years, 5),
        'eps_cagr_5y': fa.growth_rate(years, 5, fa.eps),
        'shares_outstanding_cagr_5y': fa.growth_rate(years, 5, lambda y: fa.num(y.get('shares_outstanding'))),
        'fcf_margin': fa.fcf_margin(last),
        'fcf_margin_avg_5y': fa.mean(fa.yearly_values(years, 5, fa.fcf_margin)),
        'fcf_positive_years_10y': fa.fcf_positive_years(years, 10),
        'net_debt_ebitda': fa.net_debt_ebitda(last),
        'net_debt_ebitda_max_5y': agg(max, fa.yearly_values(years, 5, fa.net_debt_ebitda)),
        'interest_coverage': fa.interest_coverage(last),
        'interest_coverage_min_5y': agg(min, fa.yearly_values(years, 5, fa.interest_coverage)),
        'ev_ebit': ratio(ev, fin_to_eur(fa.num(last.get('ebit')))),
        'fcf_yield': ratio(fin_to_eur(fa.num(last.get('free_cash_flow'))), market_cap_eur),
        'fcf_payout_ratio': fa.fcf_payout_ratio(last),
        **dict.fromkeys(VALUATION_HISTORY_METRICS),
    }


def default_asof(conn: sqlite3.Connection) -> str | None:
    return conn.execute('SELECT MAX(date) FROM price_daily p JOIN item i USING (item_id) WHERE i.active = 1'
                        ).fetchone()[0]


def load_fx(conn: sqlite3.Connection) -> dict[str, pd.Series]:
    df = pd.read_sql_query('SELECT currency, date, eur_per_unit FROM fx_rate_daily', conn, parse_dates=['date'])
    return {c: g.set_index('date')['eur_per_unit'].sort_index() for c, g in df.groupby('currency')}


def load_prices(conn: sqlite3.Connection, item_id: int, asof: str) -> pd.DataFrame:
    return pd.read_sql_query('SELECT date, close, adj_close, volume FROM price_daily WHERE item_id = ? AND date <= ? '
                             'ORDER BY date', conn, params=(item_id, asof), parse_dates=['date'], index_col='date')


def load_benchmarks(conn: sqlite3.Connection, config: dict, asof: str, fx: dict) -> tuple[dict, dict]:
    """
    {ticker: (adj_close, major currency, EUR per unit; None for EUR or unknown)} for every configured benchmark
    with prices, and
    {ticker: reason} for the ones without (relative strength against them is NULL).
    """
    series, missing = {}, {}
    for ticker in config['benchmarks']:
        row = conn.execute('SELECT item_id, currency FROM item WHERE ticker = ?', (ticker,)).fetchone()
        prices = load_prices(conn, row['item_id'], asof) if row else None
        if prices is None or prices.empty:
            missing[ticker] = 'no prices stored (run `screener update prices`)'
            continue
        currency = major_currency(row['currency'])[0]
        adj = prices['adj_close'] if prices['adj_close'].notna().any() else prices['close']
        series[ticker] = (adj, currency, None if currency == 'EUR' else fx.get(currency))
    for ticker, reason in missing.items():
        logger.warning(f'benchmark {ticker}: {reason}; relative strength against it is NULL')
    return series, missing


def calc_metrics(conn: sqlite3.Connection, asof: str | None = None, tickers: list[str] | None = None) -> dict:
    """Compute and store metrics for active items (or the given tickers) at asof (default: latest price date)."""
    asof = asof or default_asof(conn)
    if asof is None:
        raise SystemExit('no price data: run `screener update prices` first')
    fx = load_fx(conn)
    config = bm.load_config()
    bench, bench_missing = load_benchmarks(conn, config, asof, fx)

    sql = ('SELECT item_id, ticker, exchange, currency, market_cap, market_cap_date, total_assets, financial_currency, '
           'forward_eps, forward_eps_date, fundamentals_updated_at, (SELECT p.close FROM price_daily p '
           'WHERE p.item_id = item.item_id AND p.date <= item.market_cap_date ORDER BY p.date DESC LIMIT 1) '
           'AS market_cap_close FROM item WHERE ')
    if tickers:
        items = conn.execute(sql + f'ticker IN ({",".join("?" * len(tickers))})', [t.upper() for t in tickers])
    else:
        items = conn.execute(sql + 'active = 1')
    items = [dict(r) for r in items.fetchall()]
    logger.info(f'metrics as of {asof}: {len(items)} items')

    stored = no_prices = 0
    for n, item in enumerate(items, 1):
        prices = load_prices(conn, item['item_id'], asof)
        if prices.empty:
            no_prices += 1
            conn.execute('DELETE FROM item_metrics WHERE item_id = ? AND asof_date = ?', (item['item_id'], asof))
            continue
        divs = pd.read_sql_query('SELECT ex_date, amount FROM dividend WHERE item_id = ? AND ex_date <= ?', conn,
                                 params=(item['item_id'], asof), parse_dates=['ex_date'],
                                 index_col='ex_date')['amount']
        splits = pd.read_sql_query('SELECT date, ratio FROM split WHERE item_id = ?', conn,
                                   params=(item['item_id'],), parse_dates=['date'], index_col='date')['ratio']
        annual = pd.read_sql_query('SELECT * FROM fundamental_annual WHERE item_id = ? AND fiscal_year_end <= ? '
                                   'ORDER BY fiscal_year_end', conn, params=(item['item_id'], asof),
                                   parse_dates=['fiscal_year_end'], index_col='fiscal_year_end')
        item_bench = {'market': bench.get(bm.market_benchmark(item, config)), 'world': bench.get(config['world'])}
        m = compute_metrics(prices, divs, item, fx.get(major_currency(item['currency'])[0]), asof,
                            annual, fx.get(major_currency(item['financial_currency'])[0]), item_bench, splits)
        cols = {'item_id': item['item_id'], 'asof_date': asof, **m, 'calculated_at': now_utc()}
        conn.execute(f'INSERT OR REPLACE INTO item_metrics ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                     list(cols.values()))
        stored += 1
        if n % 200 == 0:
            conn.commit()
            logger.info(f'metrics {n}/{len(items)}')
    conn.commit()
    result = {'asof': asof, 'items': len(items), 'stored': stored, 'no_prices': no_prices}
    if bench_missing:
        result['benchmarks_missing'] = bench_missing
    return result
