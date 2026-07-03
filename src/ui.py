from __future__ import annotations

import streamlit as st


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 2rem;
            padding-bottom: 3rem;
        }
        .hero-card {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 1rem;
            padding: 0.2rem 0 1rem 0;
        }
        .eyebrow {
            font-size: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: #6b7280;
            font-weight: 700;
        }
        .hero-title {
            font-size: 2rem;
            font-weight: 800;
            line-height: 1.1;
            margin-top: 0.15rem;
        }
        .hero-subtitle {
            color: #6b7280;
            margin-top: 0.35rem;
            max-width: 900px;
        }
        .hero-pill {
            border: 1px solid rgba(107, 114, 128, 0.35);
            border-radius: 999px;
            padding: 0.35rem 0.7rem;
            font-size: 0.85rem;
            white-space: nowrap;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.55rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
