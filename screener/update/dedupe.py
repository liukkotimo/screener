"""
Keeping one listing per company.

Yahoo screener regions are listing regions: 'de' returns every Frankfurt/Stuttgart/... line of foreign
companies and 'us' the OTC lines, so one company can appear five or more times. Listings are grouped by
(type, name); per group the home-exchange listing is kept, otherwise the most liquid one. The others get
active = 0 and duplicate_of = the kept item, so the step is visible and reversible (reset_duplicates).
"""
import sqlite3

# Yahoo exchange codes of a company's home market, by Yahoo country.
HOME_EXCHANGES = {
    'United States': {'NYQ', 'NMS', 'NGM', 'NCM', 'ASE', 'PCX', 'NAS', 'BTS'},
    'Germany': {'GER'},
    'Finland': {'HEL'},
    'Sweden': {'STO'},
    'Norway': {'OSL'},
    'Denmark': {'CPH'},
    'France': {'PAR'},
    'Netherlands': {'AMS'},
    'Belgium': {'BRU'},
    'Italy': {'MIL'},
    'Spain': {'MCE'},
    'Austria': {'VIE'},
    'Switzerland': {'EBS'},
    'United Kingdom': {'LSE'},
    'Japan': {'JPX'},
    'Hong Kong': {'HKG'},
    'Australia': {'ASX'},
    'Canada': {'TOR', 'VAN'},
}


def is_home(item) -> bool:
    return item['exchange'] in HOME_EXCHANGES.get(item['country'], ())


def find_duplicates(conn: sqlite3.Connection) -> list[tuple[dict, list[dict]]]:
    """
    Groups of active items with the same type and name: (kept, [others]), ordered by name.
    Kept: a home-exchange listing first, then the highest avg_value_traded_eur_3m of the latest metrics
    (missing counts as 0), then the ticker.
    """
    rows = conn.execute(
        """SELECT i.item_id, i.ticker, i.name, i.type, i.exchange, i.country,
                  m.avg_value_traded_eur_3m AS turnover
           FROM item i
           LEFT JOIN item_metrics m ON m.item_id = i.item_id
                AND m.asof_date = (SELECT MAX(asof_date) FROM item_metrics)
           WHERE i.active = 1 AND i.name IS NOT NULL""").fetchall()
    groups = {}
    for r in rows:
        groups.setdefault((r['type'], r['name']), []).append(dict(r))
    result = []
    for key in sorted(groups, key=lambda k: (k[1], k[0] or '')):
        items = groups[key]
        if len(items) < 2:
            continue
        items.sort(key=lambda i: (not is_home(i), -(i['turnover'] or 0), i['ticker']))
        result.append((items[0], items[1:]))
    return result


def deactivate_duplicates(conn: sqlite3.Connection, groups: list[tuple[dict, list[dict]]]) -> int:
    """Set active = 0 and duplicate_of on the non-kept listings. Returns the number deactivated."""
    n = 0
    for kept, others in groups:
        for o in others:
            conn.execute('UPDATE item SET active = 0, duplicate_of = ? WHERE item_id = ?',
                         (kept['item_id'], o['item_id']))
            n += 1
    conn.commit()
    return n


def reset_duplicates(conn: sqlite3.Connection) -> int:
    """Reactivate every listing deactivated as a duplicate. Returns the number reactivated."""
    n = conn.execute('UPDATE item SET active = 1, duplicate_of = NULL WHERE duplicate_of IS NOT NULL').rowcount
    conn.commit()
    return n
