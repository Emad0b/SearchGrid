from __future__ import annotations

from datetime import datetime, timezone
import json

import pandas as pd
import plotly.express as px
import streamlit as st

BUDGET_SESSION_KEY = "monthly_budget_items"
BUDGET_INCOME_KEY = "monthly_budget_income"

DEFAULT_BUDGET_ITEMS: list[dict[str, object]] = [
    {"category": "Rent", "amount": 900.0, "notes": "Accommodation rent only (exclude utilities if billed separately)"},
    {"category": "Utilities", "amount": 170.0, "notes": "Electricity, gas, water, council tax share, broadband"},
    {"category": "Groceries", "amount": 50.0, "notes": "Food and household basics"},
    {"category": "Subscriptions", "amount": 35.0, "notes": "Phone, streaming, gym, cloud, software"},
    {"category": "Commute", "amount": 220.0, "notes": "Travelcard, rail, bus, or fuel"},
    {"category": "Other living", "amount": 75.0, "notes": "Clothes, toiletries, social, home items"},
]


def get_budget_items() -> list[dict[str, object]]:
    if BUDGET_SESSION_KEY not in st.session_state:
        st.session_state[BUDGET_SESSION_KEY] = [item.copy() for item in DEFAULT_BUDGET_ITEMS]
    return st.session_state[BUDGET_SESSION_KEY]


def set_budget_items(items: list[dict[str, object]]) -> None:
    st.session_state[BUDGET_SESSION_KEY] = items


def reset_budget_items() -> None:
    st.session_state[BUDGET_SESSION_KEY] = [item.copy() for item in DEFAULT_BUDGET_ITEMS]


def get_monthly_income() -> float:
    return float(st.session_state.get(BUDGET_INCOME_KEY, 0.0))


def set_monthly_income(value: float) -> None:
    st.session_state[BUDGET_INCOME_KEY] = max(0.0, float(value))


def category_amount(items: list[dict[str, object]], category: str, default: float = 0.0) -> float:
    target = category.strip().lower()
    for item in items:
        if str(item.get("category", "")).strip().lower() == target:
            return float(item.get("amount") or 0.0)
    return default


def monthly_budget_total(items: list[dict[str, object]] | None = None) -> float:
    rows = items if items is not None else get_budget_items()
    return sum(float(row.get("amount") or 0.0) for row in rows if str(row.get("category", "")).strip())


def budget_dataframe(items: list[dict[str, object]]) -> pd.DataFrame:
    rows = []
    for item in items:
        category = str(item.get("category", "")).strip()
        if not category:
            continue
        rows.append(
            {
                "Category": category,
                "Amount (£/month)": float(item.get("amount") or 0.0),
                "Notes": str(item.get("notes", "")),
            }
        )
    if not rows:
        return pd.DataFrame(columns=["Category", "Amount (£/month)", "Notes"])
    return pd.DataFrame(rows)


def items_from_dataframe(df: pd.DataFrame) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for _, row in df.iterrows():
        category = str(row.get("Category", "")).strip()
        if not category:
            continue
        items.append(
            {
                "category": category,
                "amount": float(row.get("Amount (£/month)") or 0.0),
                "notes": str(row.get("Notes", "")),
            }
        )
    return items


def render_monthly_budget_page() -> None:
    st.title("Monthly Budget")
    st.caption("Build your monthly living budget with rent, utilities, groceries, subscriptions, and custom categories.")
    st.info(
        "Changes here are saved for this session and sync into the **Living Research** page sidebar "
        "for groceries, subscriptions, and other living costs."
    )

    export_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    items = get_budget_items()
    editor_df = budget_dataframe(items)
    if editor_df.empty:
        editor_df = pd.DataFrame(
            [{"Category": "Rent", "Amount (£/month)": 900.0, "Notes": "Accommodation rent only"}]
        )

    with st.form("monthly_budget_form", clear_on_submit=False):
        st.subheader("Budget line items")
        st.caption("Edit amounts, rename categories, add rows, or delete rows. All values are monthly.")
        edited_df = st.data_editor(
            editor_df,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            column_config={
                "Category": st.column_config.TextColumn("Category", required=True, width="medium"),
                "Amount (£/month)": st.column_config.NumberColumn(
                    "Amount (£/month)",
                    min_value=0.0,
                    step=5.0,
                    format="%.2f",
                ),
                "Notes": st.column_config.TextColumn("Notes", width="large"),
            },
        )

        income_col, action_col = st.columns([1, 1])
        with income_col:
            monthly_income = st.number_input(
                "Estimated monthly take-home income (£)",
                min_value=0.0,
                value=get_monthly_income(),
                step=50.0,
                help="Optional. Used to estimate how much you would have left after this budget.",
            )
        with action_col:
            st.write("")
            st.write("")
            save_budget = st.form_submit_button("Save budget", type="primary", use_container_width=True)

    action_left, action_right = st.columns(2)
    with action_left:
        if st.button("Reset to defaults", use_container_width=True):
            reset_budget_items()
            set_monthly_income(0.0)
            st.rerun()
    with action_right:
        st.download_button(
            "Download budget (CSV)",
            data=budget_dataframe(get_budget_items()).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"monthly_budget_{export_stamp}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    if save_budget:
        saved_items = items_from_dataframe(edited_df)
        if not saved_items:
            st.error("Add at least one budget category before saving.")
        else:
            set_budget_items(saved_items)
            set_monthly_income(monthly_income)
            st.success("Monthly budget saved.")
            st.rerun()

    current_items = get_budget_items()
    total = monthly_budget_total(current_items)
    income = get_monthly_income()
    leftover = income - total if income > 0 else None

    st.markdown("### Budget summary")
    metric_left, metric_mid, metric_right = st.columns(3)
    metric_left.metric("Total monthly budget", f"£{total:,.0f}")
    metric_mid.metric("Categories", str(len(current_items)))
    if leftover is not None:
        metric_right.metric("Estimated leftover", f"£{leftover:,.0f}")
    else:
        metric_right.metric("Estimated leftover", "Add income")

    chart_left, chart_right = st.columns([1, 1])
    summary_df = budget_dataframe(current_items)
    with chart_left:
        if summary_df.empty:
            st.info("No budget categories yet.")
        else:
            fig = px.pie(
                summary_df,
                names="Category",
                values="Amount (£/month)",
                hole=0.45,
                title="Monthly spend breakdown",
            )
            fig.update_layout(margin=dict(l=10, r=10, t=45, b=10))
            st.plotly_chart(fig, use_container_width=True)
    with chart_right:
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download budget (JSON)",
            data=json.dumps(
                {
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "monthly_income": income,
                    "total_monthly_budget": total,
                    "estimated_leftover": leftover,
                    "items": current_items,
                },
                indent=2,
                ensure_ascii=False,
            ).encode("utf-8"),
            file_name=f"monthly_budget_{export_stamp}.json",
            mime="application/json",
            key="download_budget_json",
        )

    if income > 0 and leftover is not None and leftover < 0:
        st.warning(
            f"This budget exceeds your estimated take-home income by **£{abs(leftover):,.0f}** per month."
        )
    elif income > 0 and leftover is not None:
        st.success(f"Based on your income, you would have about **£{leftover:,.0f}** left each month.")
