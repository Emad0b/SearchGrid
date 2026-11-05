from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

from src.banking import BankTransaction, StatementSummary, default_banking_dir, load_transactions, list_statement_pdfs


EXCLUDEABLE_CATEGORIES = [
    "Utilities",
    "Subscriptions",
    "Groceries",
    "Shopping",
    "Food & Drink",
    "Transport",
    "Entertainment",
    "Education & Fees",
    "Cash & ATM",
    "Transfers",
    "Fees",
    "Other",
]


def render_spending_analysis_page() -> None:
    st.title("Spending Analysis")
    st.caption(
        "Local-only analysis of HSBC PDF statements in the Banking folder. "
        "Statements stay on your machine and are excluded from git."
    )
    st.warning(
        "This page reads confidential bank statements from `Banking/`. "
        "Do not commit that folder or share exports that contain account details."
    )

    banking_dir = default_banking_dir()
    pdfs = list_statement_pdfs(banking_dir)
    if not pdfs:
        st.error(
            f"No PDF statements found in `{banking_dir}`. "
            "Add monthly statement PDFs there, then refresh this page."
        )
        return

    with st.spinner("Reading statement PDFs..."):
        transactions, summaries = load_transactions(banking_dir)

    if not transactions:
        st.error("No transactions could be parsed from the PDFs.")
        return

    df = _transactions_dataframe(transactions)
    summary_df = _summaries_dataframe(summaries)

    st.sidebar.header("Spending filters")
    min_date = df["Date"].min().date()
    max_date = df["Date"].max().date()
    date_range = st.sidebar.date_input(
        "Date range",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_date, end_date = date_range
    else:
        start_date, end_date = min_date, max_date

    all_categories = sorted(df["Category"].unique().tolist())
    category_options = [c for c in EXCLUDEABLE_CATEGORIES if c in all_categories] + [
        c for c in all_categories if c not in EXCLUDEABLE_CATEGORIES
    ]
    _ensure_excluded_state(category_options)
    _apply_pending_pie_exclusions()

    excluded = st.sidebar.multiselect(
        "Exclude categories (e.g. utilities, shopping)",
        options=category_options,
        key="spend_excluded_categories",
        help="Totals and charts recalculate without the selected categories. "
        "You can also click a pie slice to disable that category.",
    )
    include_transfers = st.sidebar.checkbox("Include transfers in spend totals", value=False)
    include_fees = st.sidebar.checkbox("Include bank/FX fees in spend totals", value=True)
    show_income = st.sidebar.checkbox("Show income/credits", value=True)

    filtered = df[(df["Date"].dt.date >= start_date) & (df["Date"].dt.date <= end_date)].copy()
    spend = filtered[filtered["Direction"] == "Out"].copy()
    income = filtered[filtered["Direction"] == "In"].copy()

    if not include_transfers:
        spend = spend[spend["Category"] != "Transfers"]
    if not include_fees:
        spend = spend[spend["Category"] != "Fees"]
    if excluded:
        spend = spend[~spend["Category"].isin(excluded)]

    total_spend = float(spend["Amount"].sum()) if not spend.empty else 0.0
    total_income = float(income["Amount"].sum()) if not income.empty else 0.0
    day_count = max(1, (end_date - start_date).days + 1)
    week_count = max(day_count / 7.0, 1.0)
    month_count = max(day_count / 30.4375, 1.0)

    st.markdown("### Snapshot")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total spent", f"£{total_spend:,.0f}")
    c2.metric("Per day", f"£{total_spend / day_count:,.2f}")
    c3.metric("Per week", f"£{total_spend / week_count:,.2f}")
    c4.metric("Per month", f"£{total_spend / month_count:,.2f}")
    c5.metric("Net (in − out)", f"£{total_income - total_spend:,.0f}")

    if excluded:
        st.caption(f"Excluded from spend totals: {', '.join(excluded)}")
        _render_excluded_category_controls(excluded)

    overview_tab, trends_tab, categories_tab, statements_tab, detail_tab = st.tabs(
        ["Overview", "Trends", "Categories", "Statements", "Transactions"]
    )

    with overview_tab:
        left, right = st.columns([1.2, 1])
        with left:
            if spend.empty:
                st.info("No spending in the current filter.")
            else:
                by_cat = (
                    spend.groupby("Category", as_index=False)["Amount"]
                    .sum()
                    .sort_values("Amount", ascending=False)
                )
                fig = px.pie(
                    by_cat,
                    names="Category",
                    values="Amount",
                    hole=0.45,
                    title="Spend by category",
                )
                fig.update_traces(
                    hovertemplate=(
                        "<b>%{label}</b><br>"
                        "Amount: £%{value:,.2f}<br>"
                        "Share: %{percent}<br>"
                        "<extra>Click slice to disable</extra>"
                    ),
                    textposition="inside",
                )
                fig.update_layout(
                    margin=dict(l=10, r=10, t=45, b=10),
                    clickmode="event+select",
                    legend=dict(title="Click legend or slice to hide"),
                )
                selection = st.plotly_chart(
                    fig,
                    use_container_width=True,
                    on_select="rerun",
                    selection_mode="points",
                    key="spend_category_pie",
                )
                st.caption("Hover a slice for details, then **click** it to disable that category from totals.")
                _ = selection
        with right:
            rate_rows = []
            for category, amount in (
                spend.groupby("Category")["Amount"].sum().sort_values(ascending=False).items()
            ):
                rate_rows.append(
                    {
                        "Category": category,
                        "Total": round(float(amount), 2),
                        "Per day": round(float(amount) / day_count, 2),
                        "Per week": round(float(amount) / week_count, 2),
                        "Per month": round(float(amount) / month_count, 2),
                        "Share %": round(100 * float(amount) / total_spend, 1) if total_spend else 0,
                    }
                )
            rates_df = pd.DataFrame(rate_rows)
            st.dataframe(rates_df, use_container_width=True, hide_index=True)
            st.download_button(
                "Download category rates (CSV)",
                data=rates_df.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"spend_category_rates_{_stamp()}.csv",
                mime="text/csv",
            )

        st.subheader("With vs without selected categories")
        baseline = filtered[filtered["Direction"] == "Out"].copy()
        if not include_transfers:
            baseline = baseline[baseline["Category"] != "Transfers"]
        if not include_fees:
            baseline = baseline[baseline["Category"] != "Fees"]
        compare_cats = st.multiselect(
            "Compare excluding these categories",
            options=sorted(baseline["Category"].unique()),
            default=[c for c in ["Utilities", "Shopping", "Subscriptions"] if c in set(baseline["Category"])],
        )
        with_all = float(baseline["Amount"].sum()) if not baseline.empty else 0.0
        without = baseline[~baseline["Category"].isin(compare_cats)] if compare_cats else baseline
        without_total = float(without["Amount"].sum()) if not without.empty else 0.0
        cmp1, cmp2, cmp3 = st.columns(3)
        cmp1.metric("All included", f"£{with_all:,.0f}")
        cmp2.metric("Without selected", f"£{without_total:,.0f}")
        cmp3.metric("Difference", f"£{with_all - without_total:,.0f}")

    with trends_tab:
        if spend.empty:
            st.info("No spending in the current filter.")
        else:
            daily = spend.groupby(spend["Date"].dt.date, as_index=False)["Amount"].sum()
            daily.columns = ["Date", "Amount"]
            weekly = spend.copy()
            weekly["Week"] = weekly["Date"].dt.to_period("W-SUN").apply(lambda p: p.start_time.date())
            weekly = weekly.groupby("Week", as_index=False)["Amount"].sum()
            monthly = spend.copy()
            monthly["Month"] = monthly["Date"].dt.to_period("M").astype(str)
            monthly = monthly.groupby("Month", as_index=False)["Amount"].sum()

            st.plotly_chart(
                px.bar(daily, x="Date", y="Amount", title="Spend per day"),
                use_container_width=True,
            )
            st.plotly_chart(
                px.bar(weekly, x="Week", y="Amount", title="Spend per week"),
                use_container_width=True,
            )
            st.plotly_chart(
                px.bar(monthly, x="Month", y="Amount", title="Spend per month"),
                use_container_width=True,
            )

            stacked = spend.copy()
            stacked["Month"] = stacked["Date"].dt.to_period("M").astype(str)
            month_cat = stacked.groupby(["Month", "Category"], as_index=False)["Amount"].sum()
            st.plotly_chart(
                px.bar(
                    month_cat,
                    x="Month",
                    y="Amount",
                    color="Category",
                    title="Monthly spend by category",
                ),
                use_container_width=True,
            )

            export_left, export_right = st.columns(2)
            export_left.download_button(
                "Download daily spend (CSV)",
                data=daily.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"spend_daily_{_stamp()}.csv",
                mime="text/csv",
            )
            export_right.download_button(
                "Download monthly spend (CSV)",
                data=monthly.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"spend_monthly_{_stamp()}.csv",
                mime="text/csv",
            )

    with categories_tab:
        if spend.empty:
            st.info("No spending in the current filter.")
        else:
            top_merchants = (
                spend.groupby(["Category", "Description"], as_index=False)["Amount"]
                .agg(Total="sum", Count="count")
                .sort_values("Total", ascending=False)
                .head(40)
            )
            st.dataframe(top_merchants, use_container_width=True, hide_index=True)
            st.plotly_chart(
                px.bar(
                    top_merchants.head(20),
                    x="Total",
                    y="Description",
                    color="Category",
                    orientation="h",
                    title="Top merchants",
                ).update_layout(yaxis={"categoryorder": "total ascending"}, height=560),
                use_container_width=True,
            )

    with statements_tab:
        st.caption("Statement header totals from each PDF (not re-derived from every line).")
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download statement summaries (CSV)",
            data=summary_df.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"statement_summaries_{_stamp()}.csv",
            mime="text/csv",
        )
        st.caption(f"Loaded {len(pdfs)} PDF file(s) from `{banking_dir.name}/`.")

    with detail_tab:
        detail = spend.copy()
        if show_income and not income.empty:
            detail = pd.concat([spend, income], ignore_index=True)
        detail = detail.sort_values(["Date", "Direction", "Amount"], ascending=[False, True, False])
        display = detail[
            ["Date", "Direction", "Category", "Payment type", "Description", "Amount", "Source"]
        ].copy()
        display["Date"] = display["Date"].dt.date
        st.dataframe(display, use_container_width=True, hide_index=True)
        st.download_button(
            "Download filtered transactions (CSV)",
            data=display.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"transactions_filtered_{_stamp()}.csv",
            mime="text/csv",
        )


def _transactions_dataframe(transactions: list[BankTransaction]) -> pd.DataFrame:
    rows = []
    for txn in transactions:
        if txn.amount_out and txn.amount_out > 0:
            rows.append(
                {
                    "Date": pd.Timestamp(txn.txn_date),
                    "Direction": "Out",
                    "Amount": float(txn.amount_out),
                    "Category": txn.category,
                    "Payment type": txn.payment_type,
                    "Description": txn.description,
                    "Source": txn.source_file,
                }
            )
        if txn.amount_in and txn.amount_in > 0:
            rows.append(
                {
                    "Date": pd.Timestamp(txn.txn_date),
                    "Direction": "In",
                    "Amount": float(txn.amount_in),
                    "Category": txn.category,
                    "Payment type": txn.payment_type,
                    "Description": txn.description,
                    "Source": txn.source_file,
                }
            )
    return pd.DataFrame(rows)


def _summaries_dataframe(summaries: list[StatementSummary]) -> pd.DataFrame:
    rows = []
    for item in summaries:
        rows.append(
            {
                "Statement": item.source_file,
                "Period start": item.period_start,
                "Period end": item.period_end,
                "Opening": item.opening_balance,
                "Payments in": item.payments_in,
                "Payments out": item.payments_out,
                "Closing": item.closing_balance,
            }
        )
    return pd.DataFrame(rows)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ensure_excluded_state(category_options: list[str]) -> None:
    if "spend_excluded_categories" not in st.session_state:
        st.session_state.spend_excluded_categories = [
            category for category in ["Education & Fees"] if category in category_options
        ]
        return
    current = list(st.session_state.spend_excluded_categories)
    st.session_state.spend_excluded_categories = [
        category for category in current if category in category_options
    ]


def _apply_pending_pie_exclusions() -> None:
    selection = st.session_state.get("spend_category_pie")
    if selection is None:
        return
    points = _selection_points(selection)
    if not points:
        return

    categories = list(st.session_state.get("spend_excluded_categories", []))
    changed = False
    for point in points:
        label = _point_category_label(point)
        if label and label not in categories:
            categories.append(label)
            changed = True
    if changed:
        st.session_state.spend_excluded_categories = categories
        # Clear selection so the same click is not re-applied forever.
        st.session_state.spend_category_pie = {"selection": {"points": [], "point_indices": []}}


def _selection_points(selection: object) -> list[dict]:
    if selection is None:
        return []
    if isinstance(selection, dict):
        payload = selection.get("selection", selection)
        points = payload.get("points", []) if isinstance(payload, dict) else []
        return [point for point in points if isinstance(point, dict)]
    payload = getattr(selection, "selection", None)
    if payload is None:
        return []
    if isinstance(payload, dict):
        points = payload.get("points", [])
    else:
        points = getattr(payload, "points", [])
    return [point for point in points if isinstance(point, dict)]


def _point_category_label(point: dict) -> str | None:
    for key in ("label", "name", "legendgroup"):
        value = point.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    customdata = point.get("customdata")
    if isinstance(customdata, str) and customdata.strip():
        return customdata.strip()
    if isinstance(customdata, (list, tuple)) and customdata:
        first = customdata[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    return None


def _render_excluded_category_controls(excluded: list[str]) -> None:
    st.markdown("**Disabled categories** — click a button to re-enable:")
    columns = st.columns(min(4, max(1, len(excluded))))
    for index, category in enumerate(excluded):
        with columns[index % len(columns)]:
            if st.button(f"Enable {category}", key=f"reenable_{category}", use_container_width=True):
                current = list(st.session_state.get("spend_excluded_categories", []))
                st.session_state.spend_excluded_categories = [item for item in current if item != category]
                st.rerun()
