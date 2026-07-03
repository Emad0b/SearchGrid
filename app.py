from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

from src.accommodation import find_accommodation, listings_from_verified_urls
from src.budget import category_amount, get_budget_items, monthly_budget_total
from src.cache import JsonCache
from src.commute import TECH_DESTINATIONS, estimate_tech_commute
from src.config import load_candidate_cities, load_settings
from src.costs import collect_cost_data
from src.jobs import collect_career_data
from src.models import CareerCityData, City, RankedOption
from src.ranking import DEFAULT_WEIGHTS, rank_options
from src.search import source_markdown
from src.ui import inject_styles


def _running_in_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


def main() -> None:
    st.set_page_config(page_title="Local Living And Commute Dashboard", layout="wide", page_icon="🏠")
    inject_styles()
    settings = load_settings()
    cities = load_candidate_cities()
    city_by_name = {city.name: city for city in cities}

    st.title("Local Living And Commute Dashboard")
    st.caption(
        "London-focused research for accommodation in London and nearby commuter towns, with commuting, "
        "cycling, salary, job demand, and city living costs."
    )
    st.info(
        "The rent cap applies to accommodation rent only. Utilities, groceries, commuting, subscriptions, "
        "and other living costs are calculated separately in the monthly total."
    )

    with st.sidebar:
        st.header("Inputs")
        budget_items = get_budget_items()
        st.caption(
            f"Monthly Budget page total: **£{monthly_budget_total(budget_items):,.0f}/mo**. "
            "Groceries, subscriptions, and other living sync from there."
        )
        current_cash = st.number_input(
            "Current cash available (£)",
            min_value=0.0,
            value=3000.0,
            step=50.0,
            help="Money you have available right now for deposit, first month rent, moving costs, and runway.",
        )
        rent_amount = category_amount(budget_items, "Rent", 900.0)
        if rent_amount > 0:
            rent_slider_default = (
                max(400, int(rent_amount) - 100),
                min(2000, max(int(rent_amount) + 100, int(rent_amount))),
            )
        else:
            rent_slider_default = (800, 1000)
        budget = st.slider(
            "Accommodation rent cap, excluding utilities (£/month)",
            400,
            2000,
            rent_slider_default,
            25,
        )
        grocery_monthly = st.number_input(
            "Grocery budget (£/month)",
            min_value=0.0,
            value=category_amount(budget_items, "Groceries", 50.0),
            step=10.0,
        )
        subscriptions_monthly = st.number_input(
            "Subscriptions (£/month)",
            min_value=0.0,
            value=category_amount(budget_items, "Subscriptions", 35.0),
            step=5.0,
            help="Streaming, phone, gym, cloud storage, software, and similar recurring expenses.",
        )
        other_monthly = st.number_input(
            "Other monthly expenses (£/month)",
            min_value=0.0,
            value=category_amount(budget_items, "Other living", 75.0),
            step=10.0,
            help="Clothes, toiletries, social spending, home items, and small unexpected costs.",
        )
        st.caption("Commute is measured against London tech-industry clusters, not only generic central London.")
        selected_tech_areas = st.multiselect(
            "London tech areas / company clusters",
            options=list(TECH_DESTINATIONS),
            default=[
                "Shoreditch / Old Street",
                "King's Cross / Knowledge Quarter",
                "Liverpool Street / City",
                "Canary Wharf",
            ],
            help="The app chooses the closest selected tech cluster for each city and estimates public/bicycle commute.",
        )
        focus_cities = [
            "London",
            "Ilford",
            "Watford",
            "Slough",
            "Luton",
        ]
        comparison_cities = [city for city in focus_cities if city in city_by_name]
        default_cities = [city for city in ["London", "Ilford", "Watford", "Slough", "Luton"] if city in city_by_name]
        selected_cities = st.multiselect(
            "Cities to compare",
            options=comparison_cities,
            default=default_cities,
        )
        max_listings = st.slider("Accommodation listings per city", 1, 10, 3)
        verified_listing_urls_raw = st.text_area(
            "Paste individual listing URLs for 90% accuracy path",
            value="",
            help=(
                "Paste one Rightmove/OpenRent/SpareRoom listing URL per line. "
                "Individual listing pages can be verified more strongly than search-result pages."
            ),
        )

        st.header("Ranking weights")
        weights = {
            "affordability": st.slider("Affordability", 0, 10, int(DEFAULT_WEIGHTS["affordability"] * 20)),
            "budget_fit": st.slider("Budget fit", 0, 10, int(DEFAULT_WEIGHTS["budget_fit"] * 20)),
            "commute": st.slider("Public commute", 0, 10, int(DEFAULT_WEIGHTS["commute"] * 20)),
            "cycling": st.slider("Cycling practicality", 0, 10, int(DEFAULT_WEIGHTS["cycling"] * 20)),
            "career": st.slider("Career opportunity", 0, 10, int(DEFAULT_WEIGHTS["career"] * 20)),
            "confidence": st.slider("Source confidence", 0, 10, int(DEFAULT_WEIGHTS["confidence"] * 20)),
        }

        st.header("Internet/LLM status")
        st.write("LLM extraction:", "enabled" if settings.openai_api_key else "heuristic fallback")
        st.write("Cycling API:", "OpenRouteService" if settings.ors_api_key else "distance estimate fallback")
        force_fresh = st.checkbox(
            "Force fresh internet data",
            value=True,
            help="Bypasses cached web/LLM results so accommodation and cost data are as current as possible.",
        )
        run = st.button("Run live search", type="primary", use_container_width=True)

    if not selected_cities:
        st.info("Choose at least one city to compare.")
        return

    if not run:
        st.info("Set your inputs, then click **Run live search**. Results use public internet sources and local caching.")
        return

    cache_ttl = 0 if force_fresh else settings.cache_ttl_hours
    cache_suffix = f"fresh_{int(time.time())}" if force_fresh else "cached"
    search_cache = JsonCache(f"web_{cache_suffix}", ttl_hours=cache_ttl)
    llm_cache = JsonCache(f"llm_{cache_suffix}", ttl_hours=cache_ttl)
    extra_monthly_expenses = subscriptions_monthly + other_monthly

    with st.status("Researching live sources...", expanded=True) as status:
        all_listings = []
        commutes = {}
        careers = {}
        costs = {}
        total_steps = max(1, len(selected_cities) * 4)
        completed_steps = 0
        progress_bar = st.progress(0, text="Starting live research...")

        def advance_progress(message: str) -> None:
            nonlocal completed_steps
            completed_steps += 1
            progress_bar.progress(min(completed_steps / total_steps, 1.0), text=message)

        for city_name in selected_cities:
            city = city_by_name[city_name]
            st.write(f"Searching {city.name} accommodation, commute to London, salary, demand, and living costs...")
            careers[city.name] = collect_career_data(city.name, settings, search_cache, llm_cache)
            advance_progress(f"{city.name}: salary and CS/AI demand checked")
            costs[city.name] = collect_cost_data(city.name, grocery_monthly, settings, search_cache, llm_cache)
            advance_progress(f"{city.name}: living costs checked")
            commutes[city.name] = estimate_tech_commute(
                city,
                settings,
                search_cache,
                tech_areas=selected_tech_areas,
            )
            advance_progress(f"{city.name}: commute and cycling checked")
            city_listings = find_accommodation(
                city.name,
                settings,
                search_cache,
                llm_cache,
                min_budget=float(budget[0]),
                max_budget=float(budget[1]),
                max_results=max_listings,
            )
            all_listings.extend(city_listings)
            fallback_count = sum(1 for listing in city_listings if listing.confidence < 0.35)
            st.write(
                f"{city.name}: found {len(city_listings)} priced accommodation result(s) under £{int(budget[1])}"
                f"{f' ({fallback_count} fallback search-page result(s))' if fallback_count else ''}."
            )
            advance_progress(f"{city.name}: accommodation listings checked")

        verified_urls = [
            line.strip()
            for line in verified_listing_urls_raw.replace(",", "\n").splitlines()
            if line.strip().startswith(("http://", "https://"))
        ]
        if verified_urls:
            verified_listings = listings_from_verified_urls(
                verified_urls,
                selected_cities,
                settings,
                search_cache,
                llm_cache,
                max_budget=float(budget[1]),
            )
            all_listings.extend(verified_listings)
            st.write(f"Verified URL check: added {len(verified_listings)} individual listing result(s).")

        progress_bar.progress(1.0, text="Research complete")
        status.update(label="Research complete", state="complete")

    if not all_listings:
        st.error(
            f"No accommodation listings with usable prices under £{int(budget[1])} were found. "
            "Search providers may be blocking public result pages; try running again or increasing the max rent."
        )
        return

    ranked = rank_options(
        all_listings,
        commutes,
        careers,
        costs,
        min_budget=float(budget[0]),
        max_budget=float(budget[1]),
        grocery_monthly=grocery_monthly,
        extra_monthly_expenses=extra_monthly_expenses,
        weights=weights,
    )
    render_results(ranked, current_cash=current_cash, city_by_name=city_by_name, careers=careers)


def render_results(
    ranked: list[RankedOption],
    *,
    current_cash: float,
    city_by_name: dict[str, City],
    careers: dict[str, CareerCityData],
) -> None:
    df = _ranked_dataframe(ranked)
    best = ranked[0]
    best_accuracy = _accuracy(best)
    runway_months = current_cash / best.monthly_required_budget if best.monthly_required_budget else 0.0
    move_in_cost = _estimated_move_in_cost(best)

    st.markdown("### Decision Snapshot")
    with st.container(border=True):
        st.markdown(
            f"""
            <div class="hero-card">
              <div>
                <div class="eyebrow">Top ranked option</div>
                <div class="hero-title">{best.listing.city}</div>
                <div class="hero-subtitle">{best.listing.title}</div>
              </div>
              <div class="hero-pill">{_verification_label(best.listing.verification_level)}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        col1.metric("Score", f"{best.score:.1f}/100")
        col2.metric("Accuracy", f"{best_accuracy:.0f}/100")
        col3.metric("Rent", _money(best.listing.monthly_rent))
        col4.metric("Required / mo", _money(best.monthly_required_budget))
        col5.metric("Leftover / mo", _money(best.estimated_leftover))
        col6.metric("Cash runway", f"{runway_months:.1f} mo")

        st.markdown(
            f"""
            **Read this first:** rent is capped separately at **{_money(best.listing.monthly_rent)}**.  
            Total monthly living cost is **{_money(best.monthly_required_budget)}**, including utilities, commute,
            groceries, subscriptions, and other expenses. Estimated take-home salary is
            **{_money(best.monthly_net_salary)}**, leaving **{_money(best.estimated_leftover)}** before savings
            or one-off costs.
            """
        )
        st.markdown(
            f"""
            **Cash check:** with **{_money(current_cash)}** available now, estimated move-in cash needed is
            **{_money(move_in_cost)}**. Estimated cash left after move-in: **{_money(current_cash - move_in_cost)}**.
            """
        )

    city_summary_df = _city_summary_dataframe(ranked, current_cash=current_cash)
    map_df = _home_location_map_dataframe(ranked, city_by_name)
    jobs_df = _job_opportunities_dataframe(careers)
    details_df = _details_dataframe(ranked)
    sources_df = _sources_dataframe(ranked)
    snapshot_df = _snapshot_dataframe(best, current_cash=current_cash, best_accuracy=best_accuracy)
    export_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    _render_download_section(
        export_stamp=export_stamp,
        ranked_df=df,
        city_summary_df=city_summary_df,
        map_df=map_df,
        jobs_df=jobs_df,
        details_df=details_df,
        sources_df=sources_df,
        snapshot_df=snapshot_df,
        ranked=ranked,
        careers=careers,
    )

    summary_tab, jobs_tab, ranking_tab, evidence_tab = st.tabs(
        ["Summary", "Job Opportunities", "Ranked Grid", "Details & Sources"]
    )

    with summary_tab:
        st.caption("Goal: increase score, accuracy, and leftover. Required/month is a cost, so lower is better.")
        st.caption(
            "Accuracy reflects listing price clarity, source quality, commute confidence, salary/cost confidence, "
            "and whether values came from individual listings or broader search pages."
        )
        chart_left, chart_right = st.columns([1, 2])
        with chart_left:
            accuracy_fig = px.pie(
                names=["Accuracy", "Uncertainty"],
                values=[best_accuracy, max(0, 100 - best_accuracy)],
                hole=0.58,
                title=f"Top Option Accuracy: {best_accuracy:.0f}/100",
            )
            accuracy_fig.update_layout(margin=dict(l=10, r=10, t=45, b=10), showlegend=True)
            st.plotly_chart(accuracy_fig, use_container_width=True)
        with chart_right:
            st.dataframe(
                city_summary_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Best score": st.column_config.ProgressColumn("Best score", min_value=0, max_value=100, format="%.1f"),
                    "Accuracy": st.column_config.ProgressColumn("Accuracy", min_value=0, max_value=100, format="%d"),
                },
            )
            st.download_button(
                "Download city summary (CSV)",
                data=_csv_bytes(city_summary_df),
                file_name=f"city_summary_{export_stamp}.csv",
                mime="text/csv",
                key="download_city_summary",
            )

        st.subheader("Home Location Map")
        st.caption("Pins use city-centre coordinates for the current home search locations unless a verified listing provides exact coordinates later.")
        if map_df.empty:
            st.info("No mapped home locations available for the current results.")
        else:
            st.map(map_df, latitude="latitude", longitude="longitude", size="pin_size")
            map_display_df = map_df[
                [
                    "City",
                    "Best listing",
                    "Rent",
                    "Required/month",
                    "Accuracy",
                    "Tech area",
                    "Cycle distance",
                ]
            ]
            st.dataframe(
                map_display_df,
                use_container_width=True,
                hide_index=True,
            )
            st.download_button(
                "Download map locations (CSV)",
                data=_csv_bytes(map_df),
                file_name=f"home_locations_{export_stamp}.csv",
                mime="text/csv",
                key="download_map_locations",
            )

    with jobs_tab:
        st.subheader("Job Opportunities Near Selected Cities")
        st.caption(
            "Live web signals for CS/AI graduate roles and AWS Solution Architect listings near Ilford, London, "
            "and other selected commuter locations. Open links to verify current availability."
        )
        if jobs_df.empty:
            st.info("No job listing signals found in this run. Try again with fresh internet data enabled.")
        else:
            aws_df = jobs_df[jobs_df["Role type"] == "AWS Solution Architect"]
            if not aws_df.empty:
                st.markdown("#### AWS Solution Architect roles")
                st.dataframe(
                    aws_df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={"URL": st.column_config.LinkColumn("URL")},
                )
                st.download_button(
                    "Download AWS roles (CSV)",
                    data=_csv_bytes(aws_df),
                    file_name=f"aws_solution_architect_jobs_{export_stamp}.csv",
                    mime="text/csv",
                    key="download_aws_jobs",
                )
            st.markdown("#### All job signals")
            st.dataframe(
                jobs_df,
                use_container_width=True,
                hide_index=True,
                column_config={"URL": st.column_config.LinkColumn("URL")},
            )
            st.download_button(
                "Download all job signals (CSV)",
                data=_csv_bytes(jobs_df),
                file_name=f"job_opportunities_{export_stamp}.csv",
                mime="text/csv",
                key="download_all_jobs",
            )

    with ranking_tab:
        st.subheader("Ranked Accommodation Options")
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "URL": st.column_config.LinkColumn("URL"),
                "Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.1f"),
                "Accuracy": st.column_config.ProgressColumn("Accuracy", min_value=0, max_value=100, format="%d"),
            },
        )
        st.download_button(
            "Download ranked options (CSV)",
            data=_csv_bytes(df),
            file_name=f"ranked_accommodation_{export_stamp}.csv",
            mime="text/csv",
            key="download_ranked_options",
        )

        chart_df = df.head(15).copy()
        chart_df["Label"] = chart_df["City"] + " | " + chart_df["Listing"].str.slice(0, 34)
        fig = px.bar(chart_df, x="Score", y="Label", orientation="h", color="Estimated leftover (£/mo)")
        fig.update_layout(yaxis={"categoryorder": "total ascending"}, height=max(420, len(chart_df) * 34))
        st.plotly_chart(fig, use_container_width=True)

    with evidence_tab:
        st.subheader("Details And Source Links")
        st.download_button(
            "Download full details (CSV)",
            data=_csv_bytes(details_df),
            file_name=f"option_details_{export_stamp}.csv",
            mime="text/csv",
            key="download_option_details",
        )
        st.download_button(
            "Download source links (CSV)",
            data=_csv_bytes(sources_df),
            file_name=f"sources_{export_stamp}.csv",
            mime="text/csv",
            key="download_sources",
        )
        for rank, option in enumerate(ranked[:20], start=1):
            _render_option_detail(rank, option)


def _render_option_detail(rank: int, option: RankedOption) -> None:
    listing = option.listing
    title = (
        f"#{rank} {listing.city} | {_money(listing.monthly_rent)} rent | "
        f"{_money(option.monthly_required_budget)}/mo total | {_accuracy(option):.0f}% accuracy"
    )
    with st.expander(title):
        st.markdown(f"**Listing:** {listing.title}")
        st.caption(listing.raw_summary)
        left, middle, right = st.columns(3)
        with left:
            st.markdown("#### Housing")
            st.markdown(
                f"""
                **Rent:** {_money(listing.monthly_rent)} / month  
                **Verification:** {_verification_label(listing.verification_level)}  
                **Bills included:** {_bool_label(listing.bills_included)}  
                **Utilities estimate:** {_money(listing.utilities_estimate or option.costs.utilities_monthly)}
                """
            )
        with middle:
            st.markdown("#### Monthly Budget")
            st.markdown(
                f"""
                **Required to live:** {_money(option.monthly_required_budget)}  
                **Groceries:** {_money(option.expense_breakdown.get("groceries"))}  
                **Other living:** {_money(option.expense_breakdown.get("other_living_costs"))}  
                **Subscriptions/extra:** {_money(option.expense_breakdown.get("subscriptions_and_extra"))}
                """
            )
        with right:
            st.markdown("#### Commute & Career")
            st.markdown(
                f"""
                **Tech area:** {option.commute.destination}  
                **Public commute:** {_minutes(option.commute.public_duration_min)}  
                **Cycle:** {_minutes(option.commute.cycling_duration_min)} ({_km(option.commute.cycling_distance_km)})  
                **Salary range:** {_money(option.career.entry_salary_low)} - {_money(option.career.entry_salary_high)}  
                **Job signals:** {len(option.career.job_opportunities)}
                """
            )
        if option.career.job_opportunities:
            st.markdown("#### Nearby job opportunities")
            for job in option.career.job_opportunities[:6]:
                role_label = _role_type_label(job.role_type)
                st.markdown(f"- **{role_label}:** [{job.title}]({job.url}) — {job.location}")
                if job.snippet:
                    st.caption(job.snippet[:220])
        if option.warnings:
            st.warning(" ".join(option.warnings))
        st.write("Score breakdown:", option.score_breakdown)
        sources = listing.sources + option.commute.sources + option.career.sources + option.costs.sources
        st.markdown(f"Sources: {source_markdown(sources, limit=10)}")


def _verification_label(value: str) -> str:
    labels = {
        "verified_listing": "Verified listing",
        "live_result": "Live result",
        "source_page": "Source page",
        "search": "Search result",
        "fallback": "Fallback",
    }
    return labels.get(value, value.replace("_", " ").title())


def _ranked_dataframe(ranked: list[RankedOption]) -> pd.DataFrame:
    rows = []
    for idx, option in enumerate(ranked, start=1):
        rows.append(
            {
                "Rank": idx,
                "City": option.listing.city,
                "Listing": option.listing.title,
                "Tech area": option.commute.destination,
                "Verification": option.listing.verification_level,
                "Rent (£/mo)": round(option.listing.monthly_rent) if option.listing.monthly_rent else "Unknown",
                "Bills included": _bool_label(option.listing.bills_included),
                "Public commute": _minutes(option.commute.public_duration_min),
                "Commute cost (£/mo)": round(option.commute.public_monthly_cost or 0),
                "Cycling": _minutes(option.commute.cycling_duration_min),
                "Cycle distance (km)": round(option.commute.cycling_distance_km or 0, 1),
                "Required (£/mo)": round(option.monthly_required_budget),
                "Salary mid (£)": round(option.career.entry_salary_mid or 0),
                "Demand": round(option.career.demand_score),
                "Estimated leftover (£/mo)": round(option.estimated_leftover or 0),
                "Score": option.score,
                "Accuracy": round(_accuracy(option)),
                "URL": option.listing.url,
            }
        )
    return pd.DataFrame(rows)


def _city_summary_dataframe(ranked: list[RankedOption], *, current_cash: float) -> pd.DataFrame:
    best_by_city: dict[str, RankedOption] = {}
    for option in ranked:
        if option.listing.city not in best_by_city:
            best_by_city[option.listing.city] = option

    rows = []
    for option in best_by_city.values():
        breakdown = option.expense_breakdown
        rows.append(
            {
                "City": option.listing.city,
                "Tech area": option.commute.destination,
                "Best score": option.score,
                "Accuracy": round(_accuracy(option)),
                "Required/month": round(option.monthly_required_budget),
                "Move-in cash est.": round(_estimated_move_in_cost(option)),
                "Cash runway (mo)": round(current_cash / option.monthly_required_budget, 1)
                if option.monthly_required_budget
                else 0,
                "Rent": round(breakdown.get("rent", 0)),
                "Utilities": round(breakdown.get("utilities", 0)),
                "Groceries": round(breakdown.get("groceries", 0)),
                "Other living": round(breakdown.get("other_living_costs", 0)),
                "Commute": round(breakdown.get("commute", 0)),
                "Subscriptions/extra": round(breakdown.get("subscriptions_and_extra", 0)),
                "Leftover/month": round(option.estimated_leftover or 0),
                "Job signals": len(option.career.job_opportunities),
            }
        )
    return pd.DataFrame(rows).sort_values("Best score", ascending=False)


def _home_location_map_dataframe(ranked: list[RankedOption], city_by_name: dict[str, City]) -> pd.DataFrame:
    best_by_city: dict[str, RankedOption] = {}
    for option in ranked:
        if option.listing.city not in best_by_city:
            best_by_city[option.listing.city] = option

    rows = []
    for option in best_by_city.values():
        city = city_by_name.get(option.listing.city)
        if city is None or city.latitude is None or city.longitude is None:
            continue
        rows.append(
            {
                "City": option.listing.city,
                "latitude": city.latitude,
                "longitude": city.longitude,
                "pin_size": max(2500, min(9000, option.score * 90)),
                "Best listing": option.listing.title,
                "Rent": _money(option.listing.monthly_rent),
                "Required/month": _money(option.monthly_required_budget),
                "Accuracy": f"{_accuracy(option):.0f}%",
                "Tech area": option.commute.destination,
                "Cycle distance": _km(option.commute.cycling_distance_km),
            }
        )
    return pd.DataFrame(rows)


def _accuracy(option: RankedOption) -> float:
    return max(0.0, min(option.score_breakdown.get("confidence", 0.0), 100.0))


def _estimated_move_in_cost(option: RankedOption) -> float:
    rent = option.expense_breakdown.get("rent", option.listing.monthly_rent or 0.0)
    deposit = rent * 5 * 12 / 52
    moving_buffer = 150.0
    return rent + deposit + moving_buffer


def _money(value: float | None) -> str:
    if value is None:
        return "Unknown"
    return f"£{value:,.0f}"


def _minutes(value: float | None) -> str:
    if value is None:
        return "Unknown"
    hours = int(value // 60)
    minutes = int(round(value % 60))
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _km(value: float | None) -> str:
    if value is None:
        return "unknown distance"
    return f"{value:.1f} km"


def _bool_label(value: bool | None) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "Unknown"


def _render_download_section(
    *,
    export_stamp: str,
    ranked_df: pd.DataFrame,
    city_summary_df: pd.DataFrame,
    map_df: pd.DataFrame,
    jobs_df: pd.DataFrame,
    details_df: pd.DataFrame,
    sources_df: pd.DataFrame,
    snapshot_df: pd.DataFrame,
    ranked: list[RankedOption],
    careers: dict[str, CareerCityData],
) -> None:
    bundle = _zip_export_bundle(
        export_stamp=export_stamp,
        ranked_df=ranked_df,
        city_summary_df=city_summary_df,
        map_df=map_df,
        jobs_df=jobs_df,
        details_df=details_df,
        sources_df=sources_df,
        snapshot_df=snapshot_df,
        ranked=ranked,
        careers=careers,
    )

    with st.expander("Download results", expanded=False):
        st.caption("Export the current research run as CSV, JSON, or a ZIP bundle.")
        top_left, top_right = st.columns([1, 1])
        with top_left:
            st.download_button(
                "Download all results (ZIP)",
                data=bundle,
                file_name=f"living_commute_research_{export_stamp}.zip",
                mime="application/zip",
                type="primary",
                use_container_width=True,
                key="download_all_zip",
            )
        with top_right:
            st.download_button(
                "Download full dataset (JSON)",
                data=_json_export_bytes(ranked, careers),
                file_name=f"living_commute_research_{export_stamp}.json",
                mime="application/json",
                use_container_width=True,
                key="download_all_json",
            )

        csv_left, csv_mid, csv_right = st.columns(3)
        csv_left.download_button(
            "Ranked options (CSV)",
            data=_csv_bytes(ranked_df),
            file_name=f"ranked_accommodation_{export_stamp}.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_zip_ranked",
        )
        csv_mid.download_button(
            "City summary (CSV)",
            data=_csv_bytes(city_summary_df),
            file_name=f"city_summary_{export_stamp}.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_zip_city_summary",
        )
        csv_right.download_button(
            "Job opportunities (CSV)",
            data=_csv_bytes(jobs_df),
            file_name=f"job_opportunities_{export_stamp}.csv",
            mime="text/csv",
            use_container_width=True,
            key="download_zip_jobs",
        )


def _csv_bytes(df: pd.DataFrame) -> bytes:
    if df.empty:
        return b""
    return df.to_csv(index=False).encode("utf-8-sig")


def _zip_export_bundle(
    *,
    export_stamp: str,
    ranked_df: pd.DataFrame,
    city_summary_df: pd.DataFrame,
    map_df: pd.DataFrame,
    jobs_df: pd.DataFrame,
    details_df: pd.DataFrame,
    sources_df: pd.DataFrame,
    snapshot_df: pd.DataFrame,
    ranked: list[RankedOption],
    careers: dict[str, CareerCityData],
) -> bytes:
    files: dict[str, bytes] = {
        f"snapshot_{export_stamp}.csv": _csv_bytes(snapshot_df),
        f"ranked_accommodation_{export_stamp}.csv": _csv_bytes(ranked_df),
        f"city_summary_{export_stamp}.csv": _csv_bytes(city_summary_df),
        f"home_locations_{export_stamp}.csv": _csv_bytes(map_df),
        f"job_opportunities_{export_stamp}.csv": _csv_bytes(jobs_df),
        f"option_details_{export_stamp}.csv": _csv_bytes(details_df),
        f"sources_{export_stamp}.csv": _csv_bytes(sources_df),
        f"living_commute_research_{export_stamp}.json": _json_export_bytes(ranked, careers),
    }
    if not jobs_df.empty:
        aws_df = jobs_df[jobs_df["Role type"] == "AWS Solution Architect"]
        files[f"aws_solution_architect_jobs_{export_stamp}.csv"] = _csv_bytes(aws_df)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            if payload:
                archive.writestr(name, payload)
    return buffer.getvalue()


def _snapshot_dataframe(
    best: RankedOption,
    *,
    current_cash: float,
    best_accuracy: float,
) -> pd.DataFrame:
    move_in_cost = _estimated_move_in_cost(best)
    runway_months = current_cash / best.monthly_required_budget if best.monthly_required_budget else 0.0
    return pd.DataFrame(
        [
            {
                "Top city": best.listing.city,
                "Top listing": best.listing.title,
                "Listing URL": best.listing.url,
                "Verification": best.listing.verification_level,
                "Score": round(best.score, 1),
                "Accuracy": round(best_accuracy),
                "Rent (£/mo)": best.listing.monthly_rent,
                "Required (£/mo)": round(best.monthly_required_budget),
                "Leftover (£/mo)": round(best.estimated_leftover or 0),
                "Current cash (£)": round(current_cash, 2),
                "Move-in cash est. (£)": round(move_in_cost),
                "Cash after move-in (£)": round(current_cash - move_in_cost, 2),
                "Cash runway (mo)": round(runway_months, 1),
                "Tech area": best.commute.destination,
                "Public commute": _minutes(best.commute.public_duration_min),
                "Cycle commute": _minutes(best.commute.cycling_duration_min),
            }
        ]
    )


def _details_dataframe(ranked: list[RankedOption]) -> pd.DataFrame:
    rows = []
    for rank, option in enumerate(ranked, start=1):
        listing = option.listing
        breakdown = option.expense_breakdown
        rows.append(
            {
                "Rank": rank,
                "City": listing.city,
                "Listing": listing.title,
                "Listing URL": listing.url,
                "Location": listing.location,
                "Verification": listing.verification_level,
                "Rent (£/mo)": listing.monthly_rent,
                "Bills included": _bool_label(listing.bills_included),
                "Utilities (£/mo)": breakdown.get("utilities"),
                "Groceries (£/mo)": breakdown.get("groceries"),
                "Other living (£/mo)": breakdown.get("other_living_costs"),
                "Commute (£/mo)": breakdown.get("commute"),
                "Subscriptions/extra (£/mo)": breakdown.get("subscriptions_and_extra"),
                "Required (£/mo)": round(option.monthly_required_budget),
                "Salary low (£)": option.career.entry_salary_low,
                "Salary mid (£)": option.career.entry_salary_mid,
                "Salary high (£)": option.career.entry_salary_high,
                "Demand score": round(option.career.demand_score),
                "Job signals": len(option.career.job_opportunities),
                "Tech area": option.commute.destination,
                "Public commute": _minutes(option.commute.public_duration_min),
                "Commute cost (£/mo)": option.commute.public_monthly_cost,
                "Cycle commute": _minutes(option.commute.cycling_duration_min),
                "Cycle distance (km)": option.commute.cycling_distance_km,
                "Estimated leftover (£/mo)": round(option.estimated_leftover or 0),
                "Score": round(option.score, 1),
                "Accuracy": round(_accuracy(option)),
                "Warnings": " | ".join(option.warnings),
                "Summary": listing.raw_summary,
            }
        )
    return pd.DataFrame(rows)


def _sources_dataframe(ranked: list[RankedOption]) -> pd.DataFrame:
    rows = []
    seen: set[tuple[str, str, str]] = set()
    for rank, option in enumerate(ranked, start=1):
        listing = option.listing
        grouped_sources = [
            ("Listing", listing.sources),
            ("Commute", option.commute.sources),
            ("Career", option.career.sources),
            ("Living costs", option.costs.sources),
        ]
        for category, sources in grouped_sources:
            for source in sources:
                key = (category, source.url, source.title)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "Rank": rank,
                        "City": listing.city,
                        "Category": category,
                        "Title": source.title,
                        "URL": source.url,
                        "Snippet": source.snippet[:300] if source.snippet else "",
                    }
                )
        for job in option.career.job_opportunities:
            key = ("Job opportunity", job.url, job.title)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "Rank": rank,
                    "City": listing.city,
                    "Category": "Job opportunity",
                    "Title": job.title,
                    "URL": job.url,
                    "Snippet": job.snippet[:300] if job.snippet else "",
                }
            )
    return pd.DataFrame(rows)


def _json_export_bytes(ranked: list[RankedOption], careers: dict[str, CareerCityData]) -> bytes:
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "ranked_options": [
            {
                "rank": rank,
                "city": option.listing.city,
                "listing": {
                    "title": option.listing.title,
                    "url": option.listing.url,
                    "monthly_rent": option.listing.monthly_rent,
                    "location": option.listing.location,
                    "verification_level": option.listing.verification_level,
                    "bills_included": option.listing.bills_included,
                    "raw_summary": option.listing.raw_summary,
                },
                "monthly_required_budget": option.monthly_required_budget,
                "estimated_leftover": option.estimated_leftover,
                "expense_breakdown": option.expense_breakdown,
                "score": option.score,
                "accuracy": _accuracy(option),
                "score_breakdown": option.score_breakdown,
                "warnings": option.warnings,
                "commute": {
                    "destination": option.commute.destination,
                    "public_duration_min": option.commute.public_duration_min,
                    "public_monthly_cost": option.commute.public_monthly_cost,
                    "cycling_duration_min": option.commute.cycling_duration_min,
                    "cycling_distance_km": option.commute.cycling_distance_km,
                },
                "career": {
                    "entry_salary_low": option.career.entry_salary_low,
                    "entry_salary_mid": option.career.entry_salary_mid,
                    "entry_salary_high": option.career.entry_salary_high,
                    "demand_score": option.career.demand_score,
                    "demand_note": option.career.demand_note,
                    "job_opportunities": [
                        {
                            "title": job.title,
                            "url": job.url,
                            "location": job.location,
                            "role_type": job.role_type,
                            "snippet": job.snippet,
                            "source_name": job.source_name,
                        }
                        for job in option.career.job_opportunities
                    ],
                },
            }
            for rank, option in enumerate(ranked, start=1)
        ],
        "careers_by_city": {
            city: {
                "demand_score": career.demand_score,
                "demand_note": career.demand_note,
                "entry_salary_low": career.entry_salary_low,
                "entry_salary_mid": career.entry_salary_mid,
                "entry_salary_high": career.entry_salary_high,
                "job_opportunities": [
                    {
                        "title": job.title,
                        "url": job.url,
                        "location": job.location,
                        "role_type": job.role_type,
                        "snippet": job.snippet,
                        "source_name": job.source_name,
                    }
                    for job in career.job_opportunities
                ],
            }
            for city, career in careers.items()
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


def _role_type_label(role_type: str) -> str:
    labels = {
        "aws_architect": "AWS Solution Architect",
        "cs_ai": "CS/AI",
        "general": "General",
    }
    return labels.get(role_type, role_type.replace("_", " ").title())


def _job_opportunities_dataframe(careers: dict[str, CareerCityData]) -> pd.DataFrame:
    rows = []
    seen_urls: set[str] = set()
    for city in sorted(careers):
        career = careers[city]
        for job in career.job_opportunities:
            if job.url in seen_urls:
                continue
            seen_urls.add(job.url)
            rows.append(
                {
                    "City searched": city,
                    "Role type": _role_type_label(job.role_type),
                    "Title": job.title,
                    "Location": job.location,
                    "Source": job.source_name,
                    "Snippet": job.snippet[:180] if job.snippet else "",
                    "URL": job.url,
                }
            )
    if not rows:
        return pd.DataFrame()
    priority = {"AWS Solution Architect": 0, "CS/AI": 1, "General": 2}
    rows.sort(key=lambda row: (priority.get(row["Role type"], 3), row["City searched"], row["Title"].lower()))
    return pd.DataFrame(rows)


if __name__ == "__main__" and not _running_in_streamlit():
    import sys

    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", __file__, *sys.argv[1:]]
    raise SystemExit(stcli.main())

if _running_in_streamlit():
    main()
