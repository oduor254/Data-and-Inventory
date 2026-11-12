@echo off
rem Local run. The port is set here, not in .streamlit/config.toml: Streamlit Cloud needs its default port.
cd /d "%~dp0"
.venv\Scripts\streamlit run app.py --server.port 8510
