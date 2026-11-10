"""Native squirrel cursor for this page. Uses only a bundled PNG and CSS."""
from base64 import b64encode
from pathlib import Path

import streamlit as st


def render_cursor_squirrel(enabled: bool = True) -> None:
    if not enabled:
        # Replaces the previous style element so normal hand/text cursors return.
        st.html('<style id="jobtory-native-cursor">/* Native squirrel cursor disabled. */</style>')
        return
    icon = Path(__file__).with_name('assets') / 'squirrel_cursor.png'
    encoded = b64encode(icon.read_bytes()).decode('ascii')
    st.html(f'''<style id="jobtory-native-cursor">
    @media (any-pointer: fine) {{
      html, body, body *, body *::before, body *::after {{
        cursor: url("data:image/png;base64,{encoded}") 2 2, auto !important;
      }}
      body button:disabled, body input:disabled, body [aria-disabled="true"] {{
        cursor: not-allowed !important;
      }}
    }}
    </style>''')
