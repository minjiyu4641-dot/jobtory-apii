"""Compatibility entry point for Streamlit Cloud and existing app.py commands."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name('jobtory_v4.py')), run_name='__main__')
