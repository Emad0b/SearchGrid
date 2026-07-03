from __future__ import annotations

import streamlit as st

from src.budget import render_monthly_budget_page
from src.ui import inject_styles

st.set_page_config(page_title="Monthly Budget", layout="wide", page_icon="💷")
inject_styles()
render_monthly_budget_page()
