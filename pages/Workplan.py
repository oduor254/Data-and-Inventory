"""Workplan — Odoo's production planning app: each plan's demand, what stock
already covered, how far the draw queue has got, which tailors are allocated
and what is waiting to be issued to them (queries.WORKPLAN_*)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from lib import auth, db, filters, grid, queries, theme

st.set_page_config(page_title=auth.page_title("Workplan"), page_icon="🗓️", layout="wide")

auth.require_login()

st.title("🗓️ Workplan")
st.caption("The production plan from demand to the tailor's table: what was asked for, what stock "
           "already covered, what is still to be drawn and cut, and who is stitching it.")

connected, detail = db.check_connection()
if not connected:
    st.error(f"Could not connect to Postgres: {detail}", icon="🚫")
    st.stop()

PRIORITIES = ["Very high", "High", "Normal", "Low"]
DONE, LEFT = theme.CATEGORICAL[0], theme.CATEGORICAL[1]


def _kpis(items: list[tuple[str, str, str | None]]) -> None:
    for col, (label, value, note) in zip(st.columns(len(items)), items):
        with col.container(border=True):
            st.metric(label, value, note, delta_color="off")


def _progress_bars(df: pd.DataFrame, by: str, title: str) -> None:
    """Drawn against still-to-draw, one bar per row of df (already in display order)."""
    fig = go.Figure()
    fig.add_bar(y=df[by], x=df["Drawn"], name="Drawn", orientation="h", marker=dict(color=DONE))
    fig.add_bar(y=df[by], x=df["Remaining"], name="Still to draw", orientation="h", marker=dict(color=LEFT))
    theme.apply_layout(fig, show_legend=True)
    fig.update_layout(title=title, barmode="stack", height=max(320, 30 * len(df) + 120))
    theme.show(fig, width="stretch")


# ------------------------------------------------------------ scope ---
plans = db.run_query(queries.WORKPLAN_PLANS)
if plans.empty:
    st.info("No production plans yet.")
    st.stop()

c1, c2, c3 = st.columns([2, 2, 1])
with c1:
    countries = ["All"] + sorted(plans["Country"].unique())
    country = st.segmented_control("Country", countries, default="All", key="wp_country") or "All"
if country != "All":
    plans = plans[plans["Country"] == country]
with c2:
    labels = {f"{r['Plan']} · {r['Plan Date']:%d %b %Y}": r["Plan Id"] for _, r in plans.iterrows()}
    picked = st.selectbox("Plan", ["All plans"] + list(labels), key="wp_plan")
with c3:
    db.refresh_button(key="workplan_refresh")
if picked in labels:
    plans = plans[plans["Plan Id"] == labels[picked]]
plan_ids = set(plans["Plan Id"])

queue = db.run_query(queries.WORKPLAN_DRAW_QUEUE)
queue = queue[queue["Plan Id"].isin(plan_ids)]
open_queue = queue[queue["Status"] != "Done"]

demand, to_draw, drawn = plans["Demand"].sum(), plans["To Draw"].sum(), plans["Drawn"].sum()
covered = plans["Covered by Cut Store"].sum() + plans["Covered by WIP"].sum()
finished = plans["Finished"].sum()
# Completion: bags finished out of the bags the plan sent to be drawn.
plans["Completion %"] = (plans["Finished"] / plans["To Draw"].where(plans["To Draw"] > 0) * 100).round(0)
_kpis([
    ("Bags Demanded", f"{demand:,.0f}", f"{len(plans):,} plan{'s' if len(plans) != 1 else ''}"),
    ("Already Covered", f"{covered:,.0f}", "by the cut-bag store and WIP"),
    ("To Draw", f"{to_draw:,.0f}", f"{len(queue):,} draw requests"),
    ("Completion Rate", f"{finished / to_draw:.0%}" if to_draw else "—", "finished out of to draw"),
])
_kpis([
    ("Drawn", f"{drawn:,.0f}", f"{drawn / to_draw:.0%} of what was asked" if to_draw else None),
    ("Still to Draw", f"{open_queue['Remaining'].sum():,.0f}",
     f"{len(open_queue):,} requests open" + (f", oldest {open_queue['Days Waiting'].max():,.0f} days"
                                               if len(open_queue) else "")),
    ("Issued to Tailors", f"{plans['Issued to Tailors'].sum():,.0f}", "bags being stitched or done"),
    ("Finished", f"{finished:,.0f}", "passed quality check"),
])

SECTIONS = ["📋 Plans", "✂️ Draw Queue", "🧵 Tailors & Issue"]
section = st.segmented_control("Section", SECTIONS, default=SECTIONS[0], key="wp_section",
                               label_visibility="collapsed") or SECTIONS[0]

# ------------------------------------------------------------ Plans ---
if section == SECTIONS[0]:
    st.subheader("Progress by plan")
    st.caption("Completion is the bags finished (passed quality check) out of the bags the plan sent to be "
               "drawn. Draw progress is the first step: how many have been drawn and how many are still open.")
    recent = plans.head(15).iloc[::-1].rename(columns={"Still to Draw": "Remaining"})
    a, b = st.columns(2)
    with a.container(border=True):
        fig = go.Figure()
        fig.add_bar(y=recent["Plan"], x=recent["Completion %"].fillna(0), orientation="h",
                    marker=dict(color=DONE, cornerradius=4),
                    text=recent["Completion %"].map(lambda v: "" if pd.isna(v) else f"{v:.0f}%"),
                    textposition="outside")
        theme.apply_layout(fig, show_legend=False)
        fig.update_layout(title="Completion Rate — latest plans", height=max(320, 30 * len(recent) + 120),
                          xaxis=dict(ticksuffix="%"))
        theme.show(fig, width="stretch")
    with b.container(border=True):
        _progress_bars(recent, "Plan", "Drawn against Still to Draw — latest plans")

    st.subheader("All plans")
    with st.container(border=True):
        st.caption("Bag Types is how many different bags the plan lists. Materials to Buy is how many "
                   "raw materials the plan found short when it was approved.")
        grid.filterable_table(plans.drop(columns="Plan Id"), pinned_columns=("Plan",), height=420)

# ------------------------------------------------------- Draw Queue ---
elif section == SECTIONS[1]:
    if queue.empty:
        st.info("These plans raised no draw requests.")
        st.stop()
    by_priority = (queue.groupby("Priority")[["Requested", "Drawn", "Remaining"]].sum()
                   .reindex(PRIORITIES[::-1], fill_value=0).reset_index())
    a, b = st.columns(2)
    with a.container(border=True):
        _progress_bars(by_priority, "Priority", "Draw Progress by Priority")
    with b.container(border=True):
        top = open_queue.groupby("Bag", as_index=False)[["Drawn", "Remaining"]].sum().nlargest(12, "Remaining")
        _progress_bars(top.iloc[::-1], "Bag", "Biggest Open Requests — by Bag")

    st.subheader("The queue")
    show = st.segmented_control("Show", ["Open", "Done", "All"], default="Open", key="wp_queue_show",
                                label_visibility="collapsed") or "Open"
    view = {"Open": open_queue, "Done": queue[queue["Status"] == "Done"]}.get(show, queue)
    with st.container(border=True):
        st.caption("Highest priority first, then the longest waiting. Filter under any header.")
        grid.filterable_table(view.drop(columns="Plan Id"), pinned_columns=("Bag",), height=480)

# ------------------------------------------------- Tailors & Issue ---
else:
    alloc = db.run_query(queries.WORKPLAN_ALLOCATIONS)
    alloc = alloc[alloc["Plan Id"].isin(plan_ids)]
    start, end = filters.date_range_control("wp_issue", default="This Week")
    issues = db.run_query(queries.WORKPLAN_ISSUE_REQUESTS, {"start_date": start, "end_date": end})
    pending, issued = issues[issues["Status"] == "Pending"], issues[issues["Status"] == "Issued"]
    upcoming = alloc[alloc["Start Stitching"] >= date.today()]
    _kpis([
        ("Bags Allocated", f"{alloc['Bags'].sum():,.0f}", f"to {alloc['Tailor'].nunique():,} tailors"),
        ("Starting Today or Later", f"{upcoming['Bags'].sum():,.0f}", f"{upcoming['Tailor'].nunique():,} tailors"),
        ("Waiting to be Issued", f"{pending['Bags'].sum():,.0f}", f"{len(pending):,} requests, all dates"),
        ("Issued in Period", f"{issued['Bags'].sum():,.0f}", f"{start:%d %b} to {end:%d %b}"),
    ])

    if alloc.empty:
        st.info("No tailors have been allocated for these plans.")
    else:
        with st.container(border=True):
            daily = alloc.groupby("Start Stitching")["Bags"].sum()
            fig = go.Figure()
            fig.add_bar(x=daily.index, y=daily.values, marker=dict(color=DONE, cornerradius=4))
            theme.apply_layout(fig, show_legend=False)
            fig.update_layout(title="Bags Allocated by Stitching Start Date", height=340)
            theme.show(fig, width="stretch")

        st.subheader("Allocation by tailor")
        with st.container(border=True):
            per_tailor = (alloc.groupby(["Tailor", "Tailor Level"], as_index=False)
                          .agg(**{"Bags": ("Bags", "sum"), "Bag Types": ("Bag", "nunique"),
                                  "First Start": ("Start Stitching", "min"), "Last Start": ("Start Stitching", "max")})
                          .sort_values("Bags", ascending=False))
            per_tailor["Waiting to be Issued"] = (per_tailor["Tailor"].map(pending.groupby("Tailor")["Bags"].sum())
                                                  .fillna(0))
            grid.filterable_table(per_tailor, pinned_columns=("Tailor",), height=420)
        with st.expander(f"Every allocation ({len(alloc):,} lines)"):
            grid.filterable_table(alloc.drop(columns="Plan Id"), pinned_columns=("Tailor",), height=420)

    st.subheader("Issue requests")
    st.caption("Requests to hand cut bags to a tailor. Pending ones show whatever the period; "
               "issued ones follow the period above. These are not tied to one plan.")
    with st.container(border=True):
        grid.filterable_table(issues, pinned_columns=("Tailor",), height=420)

    with st.expander("Tailor capacity rules — bags per tailor per day"):
        st.dataframe(db.run_query(queries.WORKPLAN_CAPACITY), hide_index=True, width="stretch")
        st.caption("Set in Odoo by the production team. A tailor of a higher level also makes simpler bags, "
                   "usually faster.")
