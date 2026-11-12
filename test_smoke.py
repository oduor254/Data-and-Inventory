"""Smoke test: every page renders against the live database without an
exception. Run: python test_smoke.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv

load_dotenv()
from streamlit.testing.v1 import AppTest

for page in sorted(Path("pages").glob("*.py")):
    at = AppTest.from_file(str(page), default_timeout=300)
    at.session_state["authenticated"] = True
    at.run()
    assert not at.exception, f"{page.name}: {at.exception}"
    # st.error is also used for stock alerts, so only a failed connection counts
    assert not any("Could not connect" in e.value for e in at.error), at.error[0].value
    print("ok", page.name)
