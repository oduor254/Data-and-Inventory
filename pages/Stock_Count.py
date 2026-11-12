"""Stock Count Analytics — counts from the QR Trace Manager: who is counting
now, and each location's last count balanced against the stock movement
since the count before it (lib/stock_count.py)."""
from __future__ import annotations

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from lib import auth, db, grid
from lib import stock_count as sc

st.set_page_config(page_title=auth.page_title("Stock Count"), page_icon="🧮", layout="wide")

auth.require_login()

st.title("🧮 Stock Count")
st.caption("Stock counts from the QR Trace Manager, balanced against what each location received, "
           "sold, transferred and adjusted since its previous count. Bags only, Nairobi time.")

connected, detail = db.check_connection()
if not connected:
    st.error(f"Could not connect to Postgres: {detail}", icon="🚫")
    st.stop()

_, col_refresh = st.columns([3, 1])
with col_refresh:
    db.refresh_button(key="stock_count_refresh")

with st.spinner("Reading counts and stock movement…"):
    sessions, all_counts, voids = sc.load()
balance = sc.balance(all_counts)
now = pd.Timestamp.now(tz="Africa/Nairobi").tz_localize(None)


def _ago(t: pd.Timestamp) -> str:
    mins = int((now - t).total_seconds() // 60)
    if mins < 60:
        return f"{mins} min ago"
    if mins < 48 * 60:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} days ago"


# -------------------------------------------------------- Counting now ---
st.subheader("Counting now")
started = sc.counting_now(sessions)
recent = started["Last Scan"] > now - pd.Timedelta(hours=12)
live, stalled = started[recent], started[~recent]
names = dict(zip(balance["Code"], balance["Location"]))
if live.empty:
    st.success("No location is counting right now.", icon="✅")
else:
    for col, (_, r) in zip(st.columns(min(len(live), 4)) * ((len(live) + 3) // 4), live.iterrows()):
        with col.container(border=True):
            st.metric(names.get(r["Code"], r["Code"]), f"{r['Scans']:,} bags scanned",
                      f"last scan {_ago(r['Last Scan'])}", delta_color="off")
            st.caption(f"{r['Count']} · started {r['First Scan']:%a %d %b %H:%M}")
if not stalled.empty:
    st.warning("Started but not submitted: " + "; ".join(
        f"**{names.get(r['Code'], r['Code'])}** ({r['Count']}, {r['Scans']:,} bags, "
        f"last scan {_ago(r['Last Scan'])})" for _, r in stalled.iterrows()), icon="⏸️")
st.caption("A location is counting when its open count session has a scan in the last 12 hours. "
           "Sessions with no scans are just the next count waiting to start.")

# ------------------------------------------------- Last count per shop ---
st.subheader("Last count per location")

since_prev = voids.merge(balance[["Code", "Previous Count"]], on="Code")
since_prev = since_prev[since_prev["Voided"] > since_prev["Previous Count"]]
balance["Barcodes Replaced"] = (balance["Code"].map(since_prev.groupby("Code").size())
                                .fillna(0).astype(int))

k = st.columns(4)
for col, (label, value) in zip(k, [
    ("Locations counted", f"{len(balance):,}"),
    ("Bags short", f"{-balance['Short'].sum():,.0f}"),
    ("Bags over", f"{balance['Over'].sum():,.0f}"),
    ("Barcodes replaced", f"{balance['Barcodes Replaced'].sum():,}"),
]):
    with col.container(border=True):
        st.metric(label, value)

shown = ["Location", "Count", "Last Count", "State", "Previous Count", "Previous Counted",
         "Opening", "Received", "Sold", "Transferred Out", "Adjustments", "Closing",
         "Counted", "Short / Over", "Products Off", "Barcodes Replaced", "Unexplained"]
num = st.column_config.NumberColumn
st.dataframe(
    balance[shown].style.map(lambda v: "color: #e5534b; font-weight: 600" if v else "",
                             subset=["Short / Over", "Unexplained"]),
    hide_index=True, width="stretch",
    column_config={
        "Last Count": st.column_config.DatetimeColumn(format="ddd D MMM, HH:mm"),
        "Previous Count": st.column_config.DatetimeColumn(format="ddd D MMM, HH:mm"),
        "Previous Counted": num(help="Bags scanned at the previous count", format="%d"),
        "Opening": num(help="System stock right after the previous count was submitted", format="%d"),
        "Received": num(format="%d"), "Sold": num(help="Net of customer returns", format="%d"),
        "Transferred Out": num(format="%d"),
        "Adjustments": num(help="Stock adjustments passed in Odoo between the two counts, "
                                "added minus removed", format="%d"),
        "Closing": num(help="Opening + Received − Sold − Transferred Out + Adjustments: "
                            "what the books expect at this count", format="%d"),
        "Counted": num(help="Bags scanned at this count", format="%d"),
        "Short / Over": num(help="Counted − system stock at the count. Negative is short.", format="%d"),
        "Products Off": num(help="Products whose counted qty differs from the system", format="%d"),
        "Barcodes Replaced": num(help="Barcodes voided since the previous count, put on the "
                                      "location that last scanned them", format="%d"),
        "Unexplained": num(help="System stock at the count that the movement since the last count "
                                "doesn't account for. Should be 0.", format="%d"),
    },
)
st.caption("If Counted differs from Closing, the products below show where. A gap between Previous "
           "Counted and Opening is the previous count's short or over that was never adjusted.")

# ------------------------------------------------------- One location ---
st.subheader("Location detail")
loc = st.selectbox("Location", balance["Location"], key="sc_location")
code = balance.loc[balance["Location"] == loc, "Code"].iloc[0]
hist = all_counts[all_counts["Code"] == code]

st.markdown("**Count history**")
st.caption("Each count balanced against the one before it: Previous Counted is what that count "
           "scanned, Opening the system stock right after it was submitted. Filter under any header.")
grid.filterable_table(
    hist[["Count", "State", "Previous Count", "Previous Counted", "Opening", "Received", "Sold",
          "Transferred Out", "Adjustments", "Closing", "Last Count", "System Qty", "Counted",
          "Short", "Over", "Short / Over", "Products Off", "Anomalies", "Unexplained"]]
    .rename(columns={"Last Count": "Counted On", "Previous Count": "Previous Count On",
                     "Closing": "Closing (Books)"}),
    pinned_columns=("Count",), height=320, total=False)

# ------------------------------------------------- Count by bag type ---
st.subheader("Count by bag type")
open_now = sessions[(sessions["Code"] == code) & (sessions["State"] == "open")]
choices = {f"{r['Count']} · open now ({r['Scans']:,} scanned)": r["Session Id"]
           for _, r in open_now.sort_values("Session Id", ascending=False).iterrows()}
choices.update({f"{r['Count']} · {r['Last Count']:%d %b %Y}": r["Session Id"] for _, r in hist.iterrows()})
picked = st.selectbox("Count", list(choices), key="sc_count")
session_id = choices[picked]
count = picked.split(" · ")[0]

types, moves = sc.by_bag_type(sessions, session_id)
bags = sc.scans(session_id)
touched = types["Stock Count"] > 0
k = st.columns(6)
for col, (label, value) in zip(k, [
    ("Bags scanned", len(bags)), ("Bag types touched", touched.sum()),
    ("Types matching system", (touched & (types["Diff"] == 0)).sum()),
    ("Needs attention", (touched & (types["Diff"] != 0)).sum()),
    ("Odoo stock", types["Odoo Stock"].sum()), ("Goods in transit", types["In Transit"].sum()),
]):
    with col.container(border=True):
        st.metric(label, f"{value:,.0f}")

f1, f2 = st.columns([1, 2])
text = f1.text_input("Filter bag types", key="sc_type_text", placeholder="Filter bag types...",
                     label_visibility="collapsed")
chip = f2.segmented_control(
    "Show", ["All", "Short", "Over", "Matching", "Counted", "Has transit", "Not as expected"],
    default="All", key="sc_type_chip", label_visibility="collapsed") or "All"
view = types[types["Bag"].str.contains(text, case=False, regex=False)] if text else types
view = {
    "Short": view[view["Diff"] < 0], "Over": view[view["Diff"] > 0],
    "Matching": view[(view["Diff"] == 0)], "Counted": view[view["Stock Count"] > 0],
    "Has transit": view[view["In Transit"] > 0],
    "Not as expected": view[view["Expected"] != view["Odoo Stock"]],
}.get(chip, view).reset_index(drop=True)

red = lambda v: "color: #e5534b; font-weight: 600" if v < 0 else ("color: #57ab5a; font-weight: 600" if v > 0 else "")
fmt = {c: st.column_config.NumberColumn(format="%d") for c in view.columns if c not in ("Bag", "Product Id")}
fmt.update({
    "Last Count": st.column_config.NumberColumn(format="%d", help="Scanned of this bag at the previous count"),
    "Sold": st.column_config.NumberColumn(format="%d", help="Net of customer returns"),
    "Expected": st.column_config.NumberColumn(
        format="%d", help="Last Count + Received − Sold − Transferred Out + Adjustments"),
    "Odoo Stock": st.column_config.NumberColumn(
        format="%d", help="The books at this count (now, for a count still open)"),
    "Diff": st.column_config.NumberColumn(format="%d", help="Stock Count − Odoo Stock"),
})
event = st.dataframe(
    view.style.map(red, subset=["Diff", "Short", "Over"]),
    hide_index=True, width="stretch", height=460, key="sc_types",
    column_order=[c for c in view.columns if c != "Product Id"], column_config=fmt,
    on_select="rerun", selection_mode="single-row")
st.caption(f"{len(view):,} of {len(types):,} bag types. Click a row for that bag's stats. "
           "Odoo Stock − Expected is a short or over the previous count left unadjusted.")

rows = event.selection.rows
if rows:
    sku = view.iloc[rows[0]]
    with st.container(border=True):
        st.markdown(f"#### {sku['Bag']}")
        m = st.columns(7)
        for col, label in zip(m, ["Last Count", "Received", "Sold", "Expected", "Odoo Stock",
                                  "Stock Count", "Diff"]):
            col.metric(label, f"{sku[label]:,.0f}")
        a, b = st.columns(2)
        with a:
            st.markdown("**Moves since the last count**")
            mv = moves[moves["Product Id"] == sku["Product Id"]].drop(columns=["Product Id", "Product"])
            if mv.empty:
                st.caption("No moves.")
            else:
                st.dataframe(mv, hide_index=True, width="stretch",
                             column_config={"Date": st.column_config.DatetimeColumn(format="D MMM, HH:mm")})
            st.markdown("**Scanned in this count**")
            mine_bags = bags[bags["Product Id"] == sku["Product Id"]].drop(columns=["Product Id", "Product"])
            if mine_bags.empty:
                st.caption("None scanned.")
            else:
                st.dataframe(mine_bags, hide_index=True, width="stretch",
                             column_config={"Scanned": st.column_config.DatetimeColumn(format="D MMM, HH:mm")})
        with b:
            st.markdown(f"**Every count of this bag at {loc}**")
            st.dataframe(sc.sku_history(code, sku["Product Id"]), hide_index=True, width="stretch",
                         column_config={"Counted On": st.column_config.DatetimeColumn(format="D MMM YYYY")})

with st.expander(f"All bags scanned in {count} ({len(bags):,})"):
    grid.filterable_table(bags.drop(columns="Product Id"), pinned_columns=("Barcode",),
                          height=420, total=False)
flagged = bags.loc[bags["Anomaly"] != "", ["Barcode", "Product", "Anomaly", "Scanned"]]
if not flagged.empty:
    st.markdown(f"**Scans flagged during {count}**")
    st.dataframe(flagged, hide_index=True, width="stretch")

st.markdown(f"**Barcodes replaced at {loc}**")
mine = voids[voids["Code"] == code]
if mine.empty:
    st.caption("No voided barcodes traced to this location.")
else:
    st.dataframe(mine.drop(columns="Code"), hide_index=True, width="stretch",
                 column_config={"Voided": st.column_config.DatetimeColumn(format="D MMM YYYY, HH:mm")})

with st.expander(f"All voided barcodes ({len(voids)})"):
    st.dataframe(voids.assign(Location=voids["Code"].map(dict(zip(balance["Code"], balance["Location"])))
                              .fillna("Not traced")).drop(columns="Code"),
                 hide_index=True, width="stretch",
                 column_config={"Voided": st.column_config.DatetimeColumn(format="D MMM YYYY, HH:mm")})
    st.caption("A voided barcode is put on the location whose count last scanned it; "
               "barcodes never scanned in a count show as Not traced.")
