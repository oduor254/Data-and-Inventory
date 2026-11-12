"""Shared date-range filter: a row of PERIOD buttons with a custom range as
the fallback — reused by every domain page."""
from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

PRESETS = ["Today", "Yesterday", "This Week", "Last Week", "Last 7 Days", "Last 30 Days",
           "Month to Date", "Custom"]

def _preset_range(preset: str, today: date) -> tuple[date, date]:
    if preset == "Yesterday":
        y = today - timedelta(days=1)
        return y, y
    if preset == "Last 7 Days":
        return today - timedelta(days=6), today
    # The business week runs Sunday to Saturday.
    this_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    if preset == "This Week":
        return this_sunday, today
    if preset == "Last Week":
        return this_sunday - timedelta(days=7), this_sunday - timedelta(days=1)
    if preset == "Last 30 Days":
        return today - timedelta(days=29), today
    if preset == "Month to Date":
        return today.replace(day=1), today
    return today, today  # "Today" and the "Custom" pre-selection default


def resolve_range(key_prefix: str, default: str = "Today") -> tuple[date, date]:
    """The selected range, read from session state WITHOUT rendering the widget.

    Lets a page fetch its data before drawing any chrome, so the whole page —
    header and controls included — can sit behind a loading skeleton. The
    widget is rendered afterwards with the same keys, so its state carries.
    """
    today = date.today()
    preset = st.session_state.get(f"{key_prefix}_preset", default) or default

    if preset != "Custom":
        return _preset_range(preset, today)

    picked = st.session_state.get(f"{key_prefix}_custom", (today, today))
    if isinstance(picked, (tuple, list)):
        if len(picked) == 2:
            return picked[0], picked[1]
        if len(picked) == 1:
            return picked[0], picked[0]
        return today, today
    return picked, picked


def date_range_control(key_prefix: str, default: str = "Today") -> tuple[date, date]:
    """Render the PERIOD buttons, every option visible (+ a custom picker when 'Custom' is chosen).

    Returns (start_date, end_date) for the caller to bind into a query.
    """
    today = date.today()

    preset = st.segmented_control("Period", PRESETS, default=default if default in PRESETS else PRESETS[0],
                                  key=f"{key_prefix}_preset")

    preset = preset or default

    if preset != "Custom":
        return _preset_range(preset, today)

    picked = st.date_input(
        "Custom range",
        value=(today, today),
        max_value=today,
        key=f"{key_prefix}_custom",
        label_visibility="collapsed",
    )
    if isinstance(picked, tuple) and len(picked) == 2:
        return picked
    if isinstance(picked, tuple) and len(picked) == 1:
        return picked[0], picked[0]
    return picked, picked
