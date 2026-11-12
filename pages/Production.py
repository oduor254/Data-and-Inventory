"""Production — materials, drawing, cutting, issuing, WIP, and repairs."""
from __future__ import annotations

from datetime import date, datetime

import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from lib import auth, colors, db, filters, grid, queries, theme

st.set_page_config(page_title=auth.page_title("Production"), page_icon="🏭", layout="wide")

auth.require_login()

st.title("🏭 Production")
st.caption("Materials, drawing, cutting, issuing, WIP, and repairs by product, live from Postgres")

connected, detail = db.check_connection()
if not connected:
    st.error(f"Could not connect to Postgres: {detail}", icon="🚫")
    st.stop()

col_picker, col_refresh = st.columns([3, 1])
with col_picker:
    start_date, end_date = filters.date_range_control("production")
st.caption("\"Bags Cut in Store\" reflects current stock and ignores the date range above.")

# Fixed stage order — matches metric_order in the SQL.
METRICS = [
    "Materials Used", "Bags Drawn", "Bags Cut", "Bags Issued",
    "WIP Created", "Bags Cut in Store", "Repairs", "Samples",
]


@st.fragment()
def render_production(start_date: date, end_date: date) -> None:
    with col_refresh:
        db.refresh_button(key="production_refresh")

    df = db.run_query(
        queries.PRODUCTION_BREAKDOWN,
        {"start_date": start_date, "end_date": end_date},
    )

    if df.empty:
        st.info("No production activity for this date range yet.")
        return

    totals = df.groupby("Metric")["Quantity"].sum().reindex(METRICS).fillna(0)

    row1 = st.columns(4)
    row2 = st.columns(4)
    for col, metric_name in zip(row1 + row2, METRICS):
        with col.container(border=True):
            st.metric(metric_name, f"{totals[metric_name]:,.2f}" if metric_name == "Materials Used"
                      else f"{totals[metric_name]:,.0f}")

    st.caption(f"Last updated {datetime.now().strftime('%H:%M:%S')} · {len(df):,} product rows")

    selected_metric = st.selectbox("Metric to chart", METRICS, key="production_metric_filter")
    metric_df = df[df["Metric"] == selected_metric]

    with st.container(border=True):
        top = metric_df.nlargest(15, "Quantity").sort_values("Quantity", ascending=True)
        fig = go.Figure()
        fig.add_bar(
            y=top["Product"], x=top["Quantity"], orientation="h",
            marker=dict(color=theme.sequential_colors(len(top)), cornerradius=4),
        )
        theme.apply_layout(fig, show_legend=False)
        fig.update_layout(
            title=f"Top Products — {selected_metric}",
            height=max(360, 28 * len(top)),
        )
        theme.show(fig, width="stretch")

    with st.container(border=True):
        st.caption("Click a column header's filter icon to search or narrow that column.")
        grid.filterable_table(df[["Metric", "Product", "Quantity"]])


@st.fragment()
def render_finished(start_date: date, end_date: date) -> None:
    """Bags finished by production before they are put away (queries.FINISHED_OUTPUT)."""
    df = db.run_query(queries.FINISHED_OUTPUT, {"start_date": start_date, "end_date": end_date})
    now = db.run_query(queries.FINISHED_NOT_STORED)
    waiting = now.groupby("Stage")["Bags"].sum()

    daily = df.groupby("Date")["Finished"].sum()
    daily = daily[daily > 0]
    finished, failed = df["Finished"].sum(), df["QC Failed"].sum()
    tiles = [
        ("Bags Finished", f"{finished:,.0f}", "passed quality check"),
        ("Average per Working Day", f"{daily.mean():,.0f}" if len(daily) else "0", f"over {len(daily)} working days"),
        ("Best Day", f"{daily.max():,.0f}" if len(daily) else "0", f"{daily.idxmax():%a %d %b}" if len(daily) else None),
        ("Failed Quality Check", f"{failed:,.0f}",
         f"{failed / (finished + failed):.1%} of bags checked" if finished + failed else None),
        ("Waiting for Quality Check", f"{waiting.get('Waiting for quality check', 0):,.0f}", "right now"),
        ("Finished, Not Yet in Store", f"{waiting.get('Finished, not yet in store', 0):,.0f}", "right now"),
    ]
    for col, (label, value, note) in zip(st.columns(3) + st.columns(3), tiles):
        with col.container(border=True):
            st.metric(label, value)
            st.caption(note or " ")

    if df.empty:
        st.info("No bags were finished in this date range.")
    else:
        with st.container(border=True):
            fig = go.Figure()
            fig.add_bar(x=daily.index, y=daily.values, marker=dict(color=theme.CATEGORICAL[0], cornerradius=4))
            theme.apply_layout(fig, show_legend=False)
            fig.update_layout(title="Bags Finished per Day", height=360)
            theme.show(fig, width="stretch")

        st.subheader("Finished by product and colour")
        with st.container(border=True):
            by_product = df.groupby("Product", as_index=False)[["Finished", "QC Failed"]].sum()
            by_product.insert(0, "Bag", by_product["Product"].map(colors.strip_color))
            grid.filterable_table(by_product.sort_values("Finished", ascending=False))

    if not now.empty:
        st.subheader("Not yet in the store — right now")
        st.caption("Bags collected from the tailors that are waiting for quality check, or finished and waiting "
                   "to be put away. This ignores the date range above.")
        with st.container(border=True):
            grid.filterable_table(now)


tab_stages, tab_finished = st.tabs(["🧵 Production Stages", "✅ Finished Output"])
with tab_stages:
    render_production(start_date, end_date)
with tab_finished:
    render_finished(start_date, end_date)
