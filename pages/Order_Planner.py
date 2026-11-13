"""Order Planner — from a month's units and revenue target to what each shop
should be sent on each dispatch, and what production has to make
(lib/order_plan.py explains the model)."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from lib import auth, db, grid, queries, theme
from lib import order_plan as op
from lib import stock_planning as sp

st.set_page_config(page_title=auth.page_title("Order Planner"), page_icon="🎯", layout="wide")

auth.require_login()

st.title("🎯 Order Planner")
st.caption("Set the month's target and the planner suggests what to send each shop, colour by colour, "
           "on each dispatch, and what production must make. Bags only; sales are the last 90 days. "
           "Website and KTDA take their stock from the CBD/Stock warehouse; every other shop is sent from the warehouse.")

connected, detail = db.check_connection()
if not connected:
    st.error(f"Could not connect to Postgres: {detail}", icon="🚫")
    st.stop()

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
STANDING_ORDER = [op.BEST, op.STRONG, op.STEADY, op.SLOW, op.DORMANT]


def _kpis(items: list[tuple[str, str, str | None]]) -> None:
    for col, (label, value, note) in zip(st.columns(len(items)), items):
        with col.container(border=True):
            st.metric(label, value, note, delta_color="off")


def _download(df: pd.DataFrame, name: str, key: str) -> None:
    st.download_button("Download as CSV", df.to_csv(index=False).encode("utf-8"), file_name=name,
                       mime="text/csv", key=key)


with st.spinner("Reading stock, sales and targets…"):
    position = db.run_query(queries.STOCK_POSITION)
    demand = db.run_query(queries.STOCK_DEMAND)
    targets = db.run_query(queries.STOCK_TARGETS)
shops = sorted(position.loc[position["Bucket"] == "Shop", "Location"].unique())
demand = demand[demand["Location"].isin(shops)]
base = sp.shop_sku(position, demand, shops)
cal = sp.Calendar.for_today()

# ------------------------------------------------------------ inputs ---
months = {f"{cal.month_start:%B %Y} (this month)": (cal.month_start, cal.month_end),
          f"{cal.next_month:%B %Y} (next month)":
              (cal.next_month, cal.next_month + timedelta(days=cal.days_next - 1))}
c1, c2, c3, c4 = st.columns([1.3, 1, 1, 1.6])
with c1:
    month = st.selectbox("Month", list(months), key="op_month")
start, end = months[month]
odoo = targets[pd.to_datetime(targets["Month"]).dt.date == start]
trend_units = float(((base["Daily Rate"] + base["Rate 30d"]) / 2).sum() * ((end - start).days + 1))
with c2:
    units_target = st.number_input(
        "Units target", min_value=0, step=100, key=f"op_units_{start}",
        value=int(odoo["Target Bags"].sum() or round(trend_units, -2)),
        help="Bags to sell in the month, all shops together. Starts at the Odoo target, or at what "
             "the sales trend points to when Odoo has none.")
with c3:
    revenue_target = st.number_input(
        "Revenue target (KSh)", min_value=0, step=100000, key=f"op_revenue_{start}",
        value=int(odoo["Target Revenue"].sum()),
        help="If this needs more bags than the units target, the planner plans for the higher number.")
with c4:
    days_picked = st.pills("Dispatch days", WEEKDAYS[:6], selection_mode="multi", default=["Mon", "Wed", "Fri"],
                           key="op_days") or ["Mon", "Wed", "Fri"]
with st.expander("Assumptions"):
    a1, a2, a3 = st.columns(3)
    split = a1.radio("Split the target across shops by", [op.SPLIT_TREND, op.SPLIT_TARGETS], key="op_split",
                     help="Sales trend: each shop's share of recent sales. Shop targets: each shop's "
                          "share of the revenue targets set in Odoo for the month.")
    cover_days = a2.number_input("Days of stock left at month end", 0, 30, 7, key="op_cover",
                                 help="So a shop does not start the next month empty.")
    min_present = a3.number_input("Minimum on the shelf per selling colour", 0, 5, 1, key="op_min",
                                  help="Keeps the range balanced: every colour that sells stays in the shop.")

shop_weights = None
if split == op.SPLIT_TARGETS:
    shop_weights = odoo.set_index("Location")["Target Revenue"]
    shop_weights = shop_weights[shop_weights > 0]
    if shop_weights.empty:
        st.warning("Odoo has no shop targets for this month, so the sales trend is used.", icon="ℹ️")
        shop_weights = None

first = max(start, cal.today)
dates = op.dispatch_dates(first, end, {WEEKDAYS.index(d) for d in days_picked})
plan = op.build(base, position, demand, units_target=units_target, revenue_target=revenue_target,
                today=cal.today, start=start, end=end, dispatches=dates, shop_weights=shop_weights,
                cover_days=int(cover_days), min_present=int(min_present))
lines, production, by_date, s = plan["lines"], plan["production"], plan["by_date"], plan["summary"]
date_cols = s["dates"]

# ----------------------------------------------------------- headline ---
if s["units_for_revenue"] > units_target:
    st.info(f"The revenue target needs about **{s['units_for_revenue']:,.0f} bags** at the average selling "
            f"price of KSh {s['avg_price']:,.0f}, more than the units target of {units_target:,.0f}. "
            "The plan uses the higher number.", icon="💡")
elif revenue_target and units_target:
    st.caption(f"At the average selling price of KSh {s['avg_price']:,.0f}, {units_target:,.0f} bags bring in "
               f"about KSh {units_target * s['avg_price']:,.0f}, which covers the revenue target.")

_kpis([
    ("Bags Required", f"{s['required']:,.0f}", f"{start:%B}, all shops"),
    ("Sold So Far", f"{s['sold']:,.0f}", f"{s['sold'] / s['required']:.0%} of required" if s["required"] else None),
    ("Still to Sell", f"{max(s['required'] - s['sold'], 0):,.0f}", f"in {s['days']} days"),
    ("In Shops and on the Way", f"{s['on_hand']:,.0f}", "expected at month start" if s["future"] else "right now"),
])
if s["future"]:
    st.caption("Next month's plan assumes the shops receive nothing more before the month starts, so it "
               "overstates what to send. Run it again near month end for the real figure.")
_kpis([
    ("To Send to Shops", f"{s['send']:,.0f}", f"over {len(dates)} dispatches"),
    ("From the Warehouse", f"{s['from_warehouse']:,.0f}", "finished and ready"),
    ("Already Cut", f"{s['already_cut']:,.0f}", "in the cut-bag store, to be stitched"),
    ("To Make", f"{s['to_make']:,.0f}", "the workplan for production"),
])

SECTIONS = ["🚚 Dispatch Plan", "🏪 Shop Orders", "🎨 Colour Performance", "🏭 Production Workplan"]
section = st.segmented_control("Section", SECTIONS, default=SECTIONS[0], key="op_section",
                               label_visibility="collapsed") or SECTIONS[0]

# ------------------------------------------------------ Dispatch Plan ---
if section == SECTIONS[0]:
    with st.container(border=True):
        x = by_date["Dispatch"].map(op.label)
        fig = go.Figure()
        fig.add_bar(x=x, y=by_date["From Warehouse"], name="From the warehouse",
                    marker=dict(color=theme.CATEGORICAL[0]))
        fig.add_bar(x=x, y=by_date["From Production"], name="Needs production first",
                    marker=dict(color=theme.CATEGORICAL[1]))
        theme.apply_layout(fig, show_legend=True)
        fig.update_layout(title="Bags to Send on Each Dispatch", barmode="stack", height=360)
        theme.show(fig, width="stretch")
    st.caption("Each dispatch carries what a shop needs to last until the next one, so the first is the "
               "largest: it also fills what the shops are short of today.")

    short = s["cbd_send"] - s["cbd_has"]
    st.info(f"**CBD/Stock** holds {s['cbd_stock']:,.0f} bags. Website and KTDA need **{s['cbd_send']:,.0f}** from it "
            f"this period; it already has {s['cbd_has']:,.0f} of those in the right bags and colours"
            + (f", so **{short:,.0f} must be restocked into CBD/Stock** first." if short > 0 else "."), icon="🏬")

    st.subheader("By shop")
    by_shop = lines.groupby("Location").agg(
        **{"Takes Stock From": ("Source", "first"), "Month Target": ("Shop Target", "first"), "Sold So Far": ("Sold MTD", "sum"),
           "On Hand": ("Stock Counted In", "sum"), "To Send": ("Send", "sum"),
           **{c: (c, "sum") for c in date_cols}}).reset_index().rename(columns={"Location": "Shop"})
    by_shop["Month Target"] = by_shop["Month Target"].round(0)
    if s["future"]:
        by_shop["Sold So Far"] = 0
    with st.container(border=True):
        grid.filterable_table(by_shop, pinned_columns=("Shop",), height=520)
    _download(by_shop, f"dispatch_plan_{start:%Y_%m}.csv", "op_dl_shop")

    st.subheader("By product, colour and location")
    st.caption("The same plan one level down: each colour of each bag for each shop, and how many go on each "
               "dispatch. Filter under Shop, Product or Colour.")
    detail = (lines.loc[lines["Send"] > 0, ["Location", "Source", "Bag", "Colour", "Stock Counted In", "Send", *date_cols]]
              .rename(columns={"Location": "Shop", "Source": "Takes Stock From", "Bag": "Product",
                               "Stock Counted In": "On Hand", "Send": "To Send"})
              .sort_values(["Shop", "To Send"], ascending=[True, False]))
    with st.container(border=True):
        grid.filterable_table(detail, pinned_columns=("Shop", "Product", "Colour"), height=560)
    _download(detail, f"dispatch_plan_by_colour_{start:%Y_%m}.csv", "op_dl_detail")

# -------------------------------------------------------- Shop Orders ---
elif section == SECTIONS[1]:
    f1, f2, f3 = st.columns([1, 1, 2])
    shop = f1.selectbox("Shop", ["All shops"] + shops, key="op_shop")
    when = f2.selectbox("Dispatch", ["Whole month"] + date_cols, key="op_when")
    view = lines if shop == "All shops" else lines[lines["Location"] == shop]
    qty = "Send" if when == "Whole month" else when
    view = view[view[qty] > 0].sort_values(["Location", qty], ascending=[True, False])
    f3.metric(f"Bags to send · {shop} · {when}", f"{view[qty].sum():,.0f}",
              f"{len(view):,} bag colours", delta_color="off")
    cols = ["Location", "Source", "Bag", "Colour", "Standing", "Colour Share %", "Sold 30d", "Trend %",
            "Stock Counted In", "Need", "Send", "From Warehouse", "Awaiting Production"]
    order = view[cols + ([when] if when != "Whole month" else date_cols)].rename(
        columns={"Location": "Shop", "Source": "Takes Stock From", "Stock Counted In": "On Hand",
                 "Send": "Send (Month)"})
    with st.container(border=True):
        st.caption("Standing is the colour's place among that bag's colours at the shop. Colour Share is its "
                   "part of the bag's sales there. Need is the bags expected to sell in the rest of the month.")
        grid.filterable_table(order, pinned_columns=("Shop", "Bag", "Colour"), height=560)
    _download(order, f"shop_orders_{start:%Y_%m}.csv", "op_dl_orders")

# ------------------------------------------------- Colour Performance ---
elif section == SECTIONS[2]:
    mix = lines.groupby("Standing").agg(Colours=("SKU", "count"), **{"Sold 90d": ("Sold 90d", "sum"),
                                         "On Hand": ("On Hand", "sum"), "To Send": ("Send", "sum")})
    mix = mix.reindex(STANDING_ORDER, fill_value=0)
    _kpis([(name, f"{r['To Send']:,.0f} to send", f"{r['On Hand']:,.0f} on hand · {r['Sold 90d']:,.0f} sold in 90d")
           for name, r in mix.iterrows()])
    st.caption("Best mover is the top colour of a bag at a shop; Strong sells above that bag's average; "
               f"Steady below it; Slow sold fewer than {sp.ACTIVE_MIN_SOLD} in 90 days; Dormant sold none.")

    net = lines.groupby(["Bag", "Colour"], as_index=False).agg(
        **{"Sold 90d": ("Sold 90d", "sum"), "Sold 30d": ("Sold 30d", "sum"), "On Hand": ("On Hand", "sum"),
           "To Send": ("Send", "sum"), "Shops Selling": ("Sold 90d", lambda v: int((v > 0).sum()))})
    bag_total = net.groupby("Bag")["Sold 90d"].transform("sum")
    net["Colour Share %"] = np.where(bag_total > 0, net["Sold 90d"] / bag_total * 100, 0).round(0)
    net["Trend %"] = np.where(net["Sold 90d"] > 0, (net["Sold 30d"] / 30) / (net["Sold 90d"] / 90) * 100 - 100,
                              np.nan).round(0)
    net["Days of Cover"] = np.where(net["Sold 90d"] > 0, net["On Hand"] / (net["Sold 90d"] / 90), np.nan).round(0)
    bags = net.groupby("Bag")["Sold 90d"].sum().sort_values(ascending=False)
    bag = st.selectbox("Bag", list(bags.index), key="op_bag")
    one = net[net["Bag"] == bag].sort_values("Sold 90d")
    a, b = st.columns(2)
    with a.container(border=True):
        fig = go.Figure()
        fig.add_bar(y=one["Colour"], x=one["Sold 90d"], name="Sold in 90 days", orientation="h",
                    marker=dict(color=theme.CATEGORICAL[0]))
        fig.add_bar(y=one["Colour"], x=one["On Hand"], name="In shops now", orientation="h",
                    marker=dict(color=theme.CATEGORICAL[1]))
        theme.apply_layout(fig, show_legend=True)
        fig.update_layout(title=f"{bag} — sales against stock, by colour", barmode="group",
                          height=max(340, 44 * len(one) + 120))
        theme.show(fig, width="stretch")
    with b.container(border=True):
        st.dataframe(one.iloc[::-1].drop(columns="Bag"), hide_index=True, width="stretch")
        st.caption("A colour with much stock and few sales is lying dormant; one with many sales and "
                   "little stock is being starved.")

    st.subheader("Dormant stock in the shops")
    dormant = lines[(lines["Standing"] == op.DORMANT) & (lines["On Hand"] > 0)]
    st.caption(f"{dormant['On Hand'].sum():,.0f} bags sit in shops in colours that have not sold there in "
               "90 days. The planner sends no more of these; consider moving them to a shop where they sell.")
    with st.container(border=True):
        grid.filterable_table(dormant[["Location", "Bag", "Colour", "On Hand"]]
                              .rename(columns={"Location": "Shop"}).sort_values("On Hand", ascending=False),
                              pinned_columns=("Shop",), height=380)

# ---------------------------------------------- Production Workplan ---
else:
    output = db.run_query(queries.FINISHED_OUTPUT, {"start_date": cal.today - timedelta(days=14),
                                                    "end_date": cal.today - timedelta(days=1)})
    per_day = output.groupby("Date")["Finished"].sum()
    per_day = per_day[per_day > 0]
    daily = float(per_day.mean()) if len(per_day) else 0.0
    work_days = sum(1 for i in range((end - first).days + 1) if (first + timedelta(days=i)).weekday() < 6)
    capacity = daily * work_days
    stitch = s["already_cut"] + s["to_make"]
    _kpis([
        ("To Make", f"{s['to_make']:,.0f}", f"{production['SKU'][production['To Make'] > 0].nunique():,} bag colours"),
        ("Already Cut, to Stitch", f"{s['already_cut']:,.0f}", "from the cut-bag store"),
        ("Production Pace", f"{daily:,.0f} a day", "bags finished, last 14 days"),
        ("Can Finish by Month End", f"{capacity:,.0f}", f"{work_days} working days at that pace"),
    ])
    if capacity and stitch > capacity:
        st.warning(f"The plan needs **{stitch:,.0f} bags** stitched and production finishes about "
                   f"{capacity:,.0f} by month end at its current pace: **{stitch - capacity:,.0f} short**. "
                   "Start with the bags needed soonest, below.", icon="⚠️")
    elif capacity:
        st.success(f"At the current pace production can finish the {stitch:,.0f} bags needed "
                   f"({stitch / capacity:.0%} of what it turns out by month end).", icon="✅")

    with st.container(border=True):
        fig = go.Figure()
        fig.add_bar(x=by_date["Dispatch"].map(op.label), y=by_date["From Production"],
                    marker=dict(color=theme.CATEGORICAL[1], cornerradius=4))
        theme.apply_layout(fig, show_legend=False)
        fig.update_layout(title="Bags Production Must Have Ready for Each Dispatch", height=340)
        theme.show(fig, width="stretch")

    st.subheader("What to make")
    st.caption("Needed By is the first dispatch the warehouse cannot cover on its own. Send is the whole "
               "month for all shops; To Make is what is left after the warehouse and the bags already cut.")
    by = st.segmented_control("View", ["By colour", "By bag"], default="By colour", key="op_make_by",
                              label_visibility="collapsed") or "By colour"
    table = production if by == "By colour" else production.groupby("Bag", as_index=False).agg(
        **{"Send": ("Send", "sum"), "In Warehouse": ("In Warehouse", "sum"),
           "From Warehouse": ("From Warehouse", "sum"), "Already Cut": ("Already Cut", "sum"),
           "To Make": ("To Make", "sum"), "Needed By": ("Needed By", "min")}).sort_values(
        ["Needed By", "To Make"], ascending=[True, False])
    with st.container(border=True):
        grid.filterable_table(table, pinned_columns=("Bag",), height=560)
    _download(table, f"production_workplan_{start:%Y_%m}.csv", "op_dl_make")
