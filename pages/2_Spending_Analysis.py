from __future__ import annotations

import streamlit as st

from src.spending import render_spending_analysis_page
from src.ui import inject_styles

st.set_page_config(page_title="Spending Analysis", layout="wide", page_icon="📊")
inject_styles()
render_spending_analysis_page()
