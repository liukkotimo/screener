"""
Keeping one listing per company.

A company can be listed on several exchanges (NOKIA.HE and NOKIA.ST, Nordea on HE, ST and CO). Listings
are grouped when they share an ISIN or the same (type, name); per group the home-exchange listing is kept
(the exchange of the ISIN's country), otherwise the most liquid one. The others get active = 0 and
duplicate_of = the kept item, so the step is visible and reversible (reset_duplicates).
"""
import sqlite3

# EODHD exchange codes of a company's home market, by ISIN country prefix (ISO 3166 alpha-2).
HOME_EXCHANGES = {
    'US': {'US'},
    'DE': {'XETRA'},
    'FI': {'HE'},
    'SE': {'ST'},
    'NO': {'OL'},
    'DK': {'CO'},
    'FR': {'PA'},
    'NL': {'AS'},
    'BE': {'BR'},
    'ES': {'MC'},
    'AT': {'VI'},
    'CH': {'SW'},
    'GB': {'LSE'},
    'IE': {'IR'},
    'PT': {'LS'},
    'AU': {'AU'},
    'CA': {'TO', 'V'},
}


def is_home(item) -> bool:
    return bool(item['isin']) and item['exchange'] in HOME_EXCHANGES.get(item['isin'][:2], ())


def find_duplicates(conn: sqlite3.Connection) -> list[tuple[dict, list[dict]]]:
    """
    Groups of active items sharing an ISIN or (type, name): (kept, [others]), ordered by the kept name.
    Kept: a home-exchange listing first, then the highest avg_value_traded_eur_3m of the latest metrics
    (missing counts as 0), then the ticker.
    """
    rows = conn.execute(
        """SELECT i.item_id, i.ticker, i.name, i.type, i.exchange, i.isin,
                  m.avg_value_traded_eur_3m AS turnover
           FROM item i
           LEFT JOIN item_metrics m ON m.item_id = i.item_id
                AND m.asof_date = (SELECT MAX(asof_date) FROM item_metrics)
           WHERE i.active = 1 AND (i.name IS NOT NULL OR i.isin IS NOT NULL)""").fetchall()
    items = {r['item_id']: dict(r) for r in rows}
    # Union-find: an item joins the group of the first item seen with the same ISIN or the same (type, name).
    parent = {i: i for i in items}

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    first_seen = {}
    for i, it in items.items():
        for key in (('isin', it['isin']), ('name', it['type'], it['name'])):
            if key[-1] is None:
                continue
            if key in first_seen:
                parent[root(i)] = root(first_seen[key])
            else:
                first_seen[key] = i
    groups = {}
    for i in items:
        groups.setdefault(root(i), []).append(items[i])
    result = []
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda i: (not is_home(i), -(i['turnover'] or 0), i['ticker']))
        result.append((group[0], group[1:]))
    result.sort(key=lambda g: (g[0]['name'] or '', g[0]['ticker']))
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
