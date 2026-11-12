"""Smoke test: every page renders against the live database without an
exception. Run: python test_smoke.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv

load_dotenv()
from streamlit.testing.v1 import AppTest

def check(at, name):
    assert not at.exception, f"{name}: {at.exception}"
    # st.error is also used for stock alerts, so only a failed connection counts
    assert not any("Could not connect" in e.value for e in at.error), at.error[0].value
    print("ok", name.encode("ascii", "ignore").decode())  # Windows consoles choke on emoji


for page in sorted(Path("pages").glob("*.py")):
    at = AppTest.from_file(str(page), default_timeout=300)
    at.session_state["authenticated"] = True
    at.run()
    check(at, page.name)
    # pages that draw one section at a time: walk the others too
    for i in range(len(at.button_group)):
        if at.button_group[i].proto.click_mode:  # multi-select filter, not a section switch
            continue
        # the harness's own .options drop a leading emoji, which the page's value includes
        options = [f"{o.content_icon} {o.content}".strip() for o in at.button_group[i].proto.options]
        for option in options[1:]:
            at.button_group[i].set_value(option).run()
            assert at.button_group[i].value == option, f"{page.name}: could not open {option}"
            check(at, f"{page.name} > {option}")
