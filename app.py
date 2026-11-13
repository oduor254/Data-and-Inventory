"""Denri Data and Inventory Dashboard — Production, Dispatch, Stock Levels
and Stock Count.

Live from the Odoo Postgres database; credentials and the login password
are in .env.

Run locally:  run.bat   (opens on port 8510)
"""
from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

import streamlit as st

from lib import auth

nav = st.navigation([
    st.Page("pages/Production.py", title="Production", icon="🏭", default=True),
    st.Page("pages/Workplan.py", title="Workplan", icon="🗓️"),
    st.Page("pages/Dispatch.py", title="Dispatch", icon="🚛"),
    st.Page("pages/Stock_Levels.py", title="Stock Levels", icon="📦"),
    st.Page("pages/Stock_Count.py", title="Stock Count", icon="🧮"),
])

with st.sidebar:
    st.caption(auth.DEFAULT_APP_NAME)

nav.run()
