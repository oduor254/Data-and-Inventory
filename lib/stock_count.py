"""Stock counts from the QR Trace Manager, balanced against stock movement.

A count session (SC/26/0301) scans every bag at one location. On submit,
Odoo writes one line per product: system qty (what the books said) and
counted qty (what was scanned). A new session opens the moment the last
one is submitted, so a session's scans — not its start time — show when
the count really happened.

For each location the last count is balanced against the period since the
count before it, using lib/stock_flow:

    opening + received − sold − transferred out ± adjustments = closing

so a short or over can be traced to the movement it came from. Bags only,
like stock_flow. Times are Nairobi time.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import streamlit as st

from lib import db, queries
from lib import stock_flow as sf

_NBI = "AT TIME ZONE 'UTC' AT TIME ZONE 'Africa/Nairobi'"

_BAGS = """
JOIN product_product  pp ON pp.id = l.product_id
JOIN product_template pt ON pt.id = pp.product_tmpl_id
JOIN product_category pc ON pc.id = pt.categ_id
    AND pc.complete_name ILIKE 'Bags /%' AND pc.complete_name NOT ILIKE '%Clothing%'
"""

SESSIONS = f"""
WITH lines AS (
    SELECT l.session_id, SUM(l.system_qty) AS sys, SUM(l.counted_qty) AS cnt,
           SUM(LEAST(l.difference, 0)) AS short, SUM(GREATEST(l.difference, 0)) AS over,
           COUNT(*) FILTER (WHERE l.difference <> 0) AS diff_lines
    FROM denri_scan_count_line l {_BAGS}
    GROUP BY 1
), scans AS (
    SELECT session_id, COUNT(*) AS scans,
           COUNT(*) FILTER (WHERE anomaly IS NOT NULL) AS anomalies,
           MIN(scan_time {_NBI}) AS first_scan, MAX(scan_time {_NBI}) AS last_scan
    FROM denri_scan_count_scan GROUP BY 1
)
SELECT s.id AS "Session Id", s.name AS "Count", split_part(sl.complete_name, '/', 1) AS "Code",
       s.state AS "State", s.end_time {_NBI} AS "Submitted",
       sc.first_scan AS "First Scan", sc.last_scan AS "Last Scan",
       COALESCE(sc.scans, 0) AS "Scans", COALESCE(sc.anomalies, 0) AS "Anomalies",
       li.sys AS "System Qty", li.cnt AS "Counted", li.short AS "Short", li.over AS "Over",
       COALESCE(li.diff_lines, 0) AS "Products Off"
FROM denri_scan_count_session s
JOIN stock_location sl ON sl.id = s.location_id
LEFT JOIN lines li ON li.session_id = s.id
LEFT JOIN scans sc ON sc.session_id = s.id
WHERE s.state <> 'cancel'
"""

# Each voided barcode is put on the location that last counted it.
VOIDS = f"""
SELECT l.name AS "Barcode", INITCAP(REPLACE(l.void_reason, '_', ' ')) AS "Reason",
       l.void_note AS "Note", l.void_date {_NBI} AS "Voided", rp.name AS "Voided By",
       x.code AS "Code"
FROM denri_qr_label l
LEFT JOIN res_users ru ON ru.id = l.void_user_id
LEFT JOIN res_partner rp ON rp.id = ru.partner_id
LEFT JOIN LATERAL (
    SELECT split_part(sl.complete_name, '/', 1) AS code
    FROM denri_scan_count_scan c
    JOIN denri_scan_count_session s ON s.id = c.session_id
    JOIN stock_location sl ON sl.id = s.location_id
    WHERE c.code = l.name OR c.unit_id = l.voided_unit_id
    ORDER BY c.scan_time DESC LIMIT 1
) x ON TRUE
WHERE l.state = 'void'
ORDER BY l.void_date DESC
"""

LINES = f"""
SELECT pt.name AS "Product", l.system_qty AS "System Qty", l.counted_qty AS "Counted",
       l.difference AS "Difference"
FROM denri_scan_count_line l {_BAGS}
WHERE l.session_id = :session AND l.difference <> 0
ORDER BY l.difference, pt.name
"""

SCANS = f"""
SELECT c.code AS "Barcode", c.product_id AS "Product Id", pt.name AS "Product",
       c.scan_time {_NBI} AS "Scanned",
       rp.name AS "Scanned By", CASE WHEN c.auto THEN 'Yes' ELSE 'No' END AS "Auto Added",
       COALESCE(c.anomaly, '') AS "Anomaly"
FROM denri_scan_count_scan c
LEFT JOIN product_product pp ON pp.id = c.product_id
LEFT JOIN product_template pt ON pt.id = pp.product_tmpl_id
LEFT JOIN res_users ru ON ru.id = c.user_id
LEFT JOIN res_partner rp ON rp.id = ru.partner_id
WHERE c.session_id = :session
ORDER BY c.scan_time
"""

def sessions() -> pd.DataFrame:
    return db.run_query(SESSIONS)


def counting_now(s: pd.DataFrame) -> pd.DataFrame:
    """Open sessions that have started scanning. An open session with no
    scans is just the next count waiting, not a count in progress."""
    return s[(s["State"] == "open") & (s["Scans"] > 0)].sort_values("Last Scan", ascending=False)


# stock_flow's moves at the exact time instead of the day: counts are
# submitted mid-day and shops often receive their restock the same day.
MOVES = queries.STOCK_FLOW_MOVES.replace("::DATE AS day", " AS day")


@st.cache_data(ttl=sf.CLOSED_TTL, show_spinner=False)
def _moves_closed(start, end) -> pd.DataFrame:
    """Finished days no longer change and take ~40s to read, so they are
    kept for CLOSED_TTL; "Refresh now" only re-reads today."""
    return db.run_query(MOVES, {"start_date": start, "end_date": end})


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(sessions, finished counts, voids), the slow reads side by side."""
    s = sessions()
    done = s[s["State"].isin(["submitted", "applied"]) & s["Submitted"].notna()]
    today = pd.Timestamp.now(tz="Africa/Nairobi").date()
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(_moves_closed, done["Submitted"].min().date(), today - pd.Timedelta(days=1)),
                pool.submit(db.run_query, MOVES, {"start_date": today, "end_date": today}),
                pool.submit(db.run_query, queries.STOCK_FLOW_NOW),
                pool.submit(voids)]
        closed, fresh, now, v = [j.result() for j in jobs]
    moves = pd.concat([closed, fresh], ignore_index=True)
    done = counts(s, moves, now)  # also gives moves its Date and Net columns
    # Counts in progress: the books now, and what they were at the first scan.
    current = now[now["Kind"] == "Current Stock"].groupby("Code")["Qty"].sum()
    s["Stock Now"] = s["Code"].map(current)
    s["Stock At Start"] = [
        current[c] - moves.loc[(moves["Code"] == c) & (moves["Date"] > t), "Net"].sum()
        if c in current.index and pd.notna(t) else float("nan")
        for c, t in zip(s["Code"], s["First Scan"].where(s["State"] == "open"))]
    return s, done, v


def counts(s: pd.DataFrame, moves: pd.DataFrame, now: pd.DataFrame) -> pd.DataFrame:
    """One row per finished count, newest first, balanced against the stock
    movement between the previous count's submit and this one's."""
    done = s[s["State"].isin(["submitted", "applied"]) & s["Submitted"].notna()]
    done = done.sort_values(["Code", "Submitted"]).copy()
    now = now[now["Kind"] == "Current Stock"]
    current = now.groupby("Code")["Qty"].sum()
    names = {c: sf.outlet_name(c, w) for c, w in zip(now["Code"], now["Warehouse"])}
    moves["Date"] = pd.to_datetime(moves["Date"])
    moves["Net"] = moves["Qty"] * moves["Kind"].isin(sf.IN_KINDS).map({True: 1, False: -1})

    g = done.groupby("Code")
    done["Previous Count"] = g["Submitted"].shift()
    done["Previous Counted"] = g["Counted"].shift()
    rows = []
    for _, c in done.iterrows():
        row = {}
        if pd.notna(c["Previous Count"]) and c["Code"] in current.index:
            m = moves[moves["Code"] == c["Code"]]
            after = lambda t: m.loc[m["Date"] > t, "Net"].sum()
            k = m[(m["Date"] > c["Previous Count"]) & (m["Date"] <= c["Submitted"])]
            k = k.groupby("Kind")["Qty"].sum().reindex(sf.KINDS, fill_value=0)
            row = {"Opening": current[c["Code"]] - after(c["Previous Count"]),
                   "Received": k["Received"], "Sold": k["Sold"] - k["Returns"],
                   "Transferred Out": k["Transferred Out"],
                   "Adjustments": k["Adj Added"] - k["Adj Removed"],
                   "Closing": current[c["Code"]] - after(c["Submitted"])}
        rows.append(row)
    t = pd.concat([done.reset_index(drop=True),
                   pd.DataFrame(rows, columns=["Opening", "Received", "Sold", "Transferred Out",
                                               "Adjustments", "Closing"])], axis=1)
    t.insert(0, "Location", t["Code"].map(names).fillna(t["Code"]))
    t["State"] = t["State"].str.title()
    t = t.rename(columns={"Submitted": "Last Count"})
    flow = t["Opening"] + t["Received"] - t["Sold"] - t["Transferred Out"] + t["Adjustments"]
    assert ((flow - t["Closing"]).abs().fillna(0) < 1e-6).all(), "movement does not add up"
    t["Short / Over"] = t["Counted"] - t["System Qty"]
    # books at the count that the movement since the last count doesn't explain
    t["Unexplained"] = t["System Qty"] - t["Closing"]
    return t.sort_values("Last Count", ascending=False).reset_index(drop=True)


def balance(all_counts: pd.DataFrame) -> pd.DataFrame:
    """Each location's last finished count."""
    return all_counts.drop_duplicates("Code").reset_index(drop=True)


def voids() -> pd.DataFrame:
    return db.run_query(VOIDS)


def lines(session_id: int) -> pd.DataFrame:
    return db.run_query(LINES, {"session": int(session_id)})


def scans(session_id: int) -> pd.DataFrame:
    return db.run_query(SCANS, {"session": int(session_id)})


# ------------------------------------------------------ count by bag type ---

SKU_LINES = f"""
SELECT l.product_id AS "Product Id", pt.name AS "Product",
       l.system_qty AS "System Qty", l.counted_qty AS "Counted"
FROM denri_scan_count_line l {_BAGS}
WHERE l.session_id = :session
"""

# what each outlet holds now and has in transit, per product
_SEL = 'SELECT o.code AS "Code", o.name AS "Warehouse", p.kind AS "Kind", SUM(p.quantity) AS "Qty"'
assert _SEL in queries.STOCK_FLOW_NOW and "GROUP BY 1, 2, 3" in queries.STOCK_FLOW_NOW
SKU_NOW = (queries.STOCK_FLOW_NOW
           .replace(_SEL, 'SELECT o.code AS "Code", p.kind AS "Kind", p.product_id AS "Product Id", '
                          'pt.name AS "Product", SUM(p.quantity) AS "Qty"')
           .replace("GROUP BY 1, 2, 3", "GROUP BY 1, 2, 3, 4"))

# Each bag move in or out of one outlet, classified the way stock_flow does.
SKU_MOVES = "WITH" + queries._FLOW_OUTLETS + f""",
moves AS (
    SELECT m.product_id, pt.name AS product, m.product_qty AS qty, m.date {_NBI} AS at,
           COALESCE(m.reference, m.origin, '') AS ref,
           so.code AS src, dst.code AS dst, ls.usage AS src_usage, ld.usage AS dst_usage
    FROM stock_move m
    JOIN stock_location ls ON ls.id = m.location_id
    JOIN stock_location ld ON ld.id = m.location_dest_id
    LEFT JOIN outlets so  ON ls.usage = 'internal' AND so.code  = split_part(ls.complete_name, '/', 1)
    LEFT JOIN outlets dst ON ld.usage = 'internal' AND dst.code = split_part(ld.complete_name, '/', 1)
""" + queries._FLOW_BAGS.format(alias="m") + """
    WHERE m.state = 'done' AND m.date >= CAST(:since AS TIMESTAMP) - INTERVAL '3 hours'
      AND (so.code = :code OR dst.code = :code)
      AND COALESCE(so.code, '') <> COALESCE(dst.code, '')
      AND NOT COALESCE(pt.is_combo, FALSE)
)
SELECT at AS "Date", product_id AS "Product Id", product AS "Product", ref AS "Reference",
       CASE WHEN src = :code THEN
                CASE dst_usage WHEN 'customer' THEN 'Sold' WHEN 'inventory' THEN 'Adj Removed'
                               ELSE 'Transferred Out' END
            ELSE CASE src_usage WHEN 'customer' THEN 'Returns' WHEN 'inventory' THEN 'Adj Added'
                                ELSE 'Received' END
       END AS "Kind", qty AS "Qty"
FROM moves ORDER BY at
"""

SKU_HISTORY = f"""
SELECT s.name AS "Count", s.end_time {_NBI} AS "Counted On", l.system_qty AS "Odoo Stock",
       l.counted_qty AS "Stock Count", l.difference AS "Diff"
FROM denri_scan_count_line l
JOIN denri_scan_count_session s ON s.id = l.session_id AND s.state IN ('submitted', 'applied')
JOIN stock_location sl ON sl.id = s.location_id
WHERE split_part(sl.complete_name, '/', 1) = :code AND l.product_id = :product
ORDER BY s.end_time DESC
"""

_KEY = ["Product Id", "Product"]


def sku_history(code: str, product_id: int) -> pd.DataFrame:
    return db.run_query(SKU_HISTORY, {"code": code, "product": int(product_id)})


def by_bag_type(s: pd.DataFrame, session_id: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one row per bag type in a count, the bag moves behind it).

    Last Count is what the previous finished count scanned of that bag; the
    moves run from that count's submit to this one's (to now for a count
    still open). Odoo Stock is the books at this count — now, for an open
    one. Expected is Last Count plus the moves, so Odoo Stock − Expected is
    what the previous count left unadjusted."""
    me = s.set_index("Session Id").loc[session_id]
    code, is_open = me["Code"], me["State"] == "open"
    end = pd.Timestamp.now(tz="Africa/Nairobi").tz_localize(None) if is_open else me["Submitted"]
    done = s[(s["Code"] == code) & s["State"].isin(["submitted", "applied"])
             & (s["Submitted"] < end) & (s["Session Id"] != session_id)]
    prev = done.sort_values("Submitted").iloc[-1] if not done.empty else None

    session = {"session": int(session_id)}
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs = {"now": pool.submit(db.run_query, SKU_NOW),
                "scans": pool.submit(db.run_query, SCANS, session),  # the page shows these too
                "lines": pool.submit(db.run_query, SKU_LINES, session)}
        if prev is not None:
            jobs["last"] = pool.submit(db.run_query, SKU_LINES, {"session": int(prev["Session Id"])})
            jobs["moves"] = pool.submit(db.run_query, SKU_MOVES,
                                        {"code": code, "since": prev["Submitted"].date()})
        got = {k: j.result() for k, j in jobs.items()}

    now = got["now"]
    now = (now[now["Code"] == code]
           .pivot_table(index=_KEY, columns="Kind", values="Qty", aggfunc="sum")
           .reindex(columns=["Current Stock", "In Transit"]).reset_index())
    parts = [now[_KEY + ["In Transit"]]]
    if is_open:
        scans_ = got["scans"].dropna(subset=["Product Id"])
        parts.append(scans_.groupby(_KEY).size().rename("Stock Count").reset_index())
        parts.append(now[_KEY + ["Current Stock"]].rename(columns={"Current Stock": "Odoo Stock"}))
    else:
        parts.append(got["lines"].rename(columns={"Counted": "Stock Count", "System Qty": "Odoo Stock"}))

    moves = pd.DataFrame(columns=["Date", "Product Id", "Product", "Reference", "Kind", "Qty"])
    if prev is not None:
        parts.append(got["last"].rename(columns={"Counted": "Last Count", "System Qty": "Odoo At Last Count"}))
        moves = got["moves"]
        moves = moves[(moves["Date"] > prev["Submitted"]) & (moves["Date"] <= end)]
        parts.append(moves.pivot_table(index=_KEY, columns="Kind", values="Qty", aggfunc="sum")
                     .reset_index())

    t = parts[0]
    for part in parts[1:]:
        t = t.merge(part, on=_KEY, how="outer")
    for c in ["Last Count", "Odoo At Last Count", *sf.KINDS, "Odoo Stock", "In Transit", "Stock Count"]:
        t[c] = t[c].fillna(0) if c in t else 0
    t["Sold"] = t["Sold"] - t["Returns"]
    t["Adjustments"] = t["Adj Added"] - t["Adj Removed"]
    net = t["Received"] - t["Sold"] - t["Transferred Out"] + t["Adjustments"]
    t["Expected"] = t["Last Count"] + net
    t["Diff"] = t["Stock Count"] - t["Odoo Stock"]
    t["Short"] = t["Diff"].clip(upper=0)
    t["Over"] = t["Diff"].clip(lower=0)
    # books that the moves don't account for, product by product (finished counts only)
    t["Unexplained"] = (t["Odoo Stock"] - t["Odoo At Last Count"] - net
                        if not is_open and prev is not None else 0)
    cols = ["Product Id", "Product", "Last Count", "Received", "Sold", "Transferred Out",
            "Adjustments", "Expected", "Odoo Stock", "In Transit", "Stock Count", "Diff",
            "Short", "Over", "Unexplained"]
    t = t[cols]
    t = t[t.drop(columns=_KEY).abs().sum(axis=1) > 0].rename(columns={"Product": "Bag"})
    return t.sort_values("Bag").reset_index(drop=True), moves
