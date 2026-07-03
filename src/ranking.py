from __future__ import annotations

from .models import AccommodationListing, CareerCityData, CityCostData, CommuteEstimate, RankedOption

DEFAULT_WEIGHTS = {
    "affordability": 0.4,
    "budget_fit": 0.15,
    "commute": 0.2,
    "cycling": 0.05,
    "career": 0.1,
    "confidence": 0.1,
}


def rank_options(
    listings: list[AccommodationListing],
    commutes: dict[str, CommuteEstimate],
    careers: dict[str, CareerCityData],
    costs: dict[str, CityCostData],
    *,
    min_budget: float,
    max_budget: float,
    grocery_monthly: float,
    extra_monthly_expenses: float = 0.0,
    weights: dict[str, float] | None = None,
) -> list[RankedOption]:
    active_weights = _normalise_weights(weights or DEFAULT_WEIGHTS)
    ranked: list[RankedOption] = []
    for listing in listings:
        commute = commutes[listing.city]
        career = careers[listing.city]
        cost = costs[listing.city]
        ranked.append(
            score_option(
                listing,
                commute,
                career,
                cost,
                min_budget=min_budget,
                max_budget=max_budget,
                grocery_monthly=grocery_monthly,
                extra_monthly_expenses=extra_monthly_expenses,
                weights=active_weights,
            )
        )
    return sorted(ranked, key=lambda option: option.score, reverse=True)


def score_option(
    listing: AccommodationListing,
    commute: CommuteEstimate,
    career: CareerCityData,
    cost: CityCostData,
    *,
    min_budget: float,
    max_budget: float,
    grocery_monthly: float,
    extra_monthly_expenses: float,
    weights: dict[str, float],
) -> RankedOption:
    salary = career.entry_salary_mid
    monthly_net = estimate_monthly_net_salary(salary) if salary else None
    rent = listing.monthly_rent or max_budget
    utilities = 0.0 if listing.bills_included else (listing.utilities_estimate or cost.utilities_monthly or 150.0)
    commute_cost = commute.public_monthly_cost or 0.0
    non_housing = max(grocery_monthly, cost.non_housing_monthly_cost or grocery_monthly)
    other_living_costs = max(0.0, non_housing - grocery_monthly)
    monthly_outgoings = rent + utilities + commute_cost + grocery_monthly + other_living_costs + extra_monthly_expenses
    leftover = monthly_net - monthly_outgoings if monthly_net is not None else None

    affordability = _scale(leftover or -500, -500, 1500)
    budget_target = (min_budget + max_budget) / 2
    budget_fit = max(0.0, 100 - abs(rent - budget_target) / max(1, budget_target) * 100)
    commute_score = _inverse_scale(commute.public_duration_min or 180, 20, 180)
    commute_cost_score = _inverse_scale(commute.public_monthly_cost or 450, 80, 500)
    commute_combined = commute_score * 0.65 + commute_cost_score * 0.35
    cycling_score = _inverse_scale(commute.cycling_duration_min or 240, 20, 180)
    career_score = _scale((career.entry_salary_mid or 25000), 22000, 45000) * 0.55 + career.demand_score * 0.45
    confidence = (
        listing.confidence * 0.5
        + commute.public_confidence * 0.18
        + commute.cycling_confidence * 0.05
        + career.confidence * 0.17
        + cost.confidence * 0.1
    ) * 100
    if listing.verification_level == "verified_listing" and commute.public_monthly_cost is not None:
        confidence = max(confidence, 90.0)
    if _has_source_linked_rent(listing) and commute.public_monthly_cost is not None:
        confidence = max(confidence, 70.0)

    breakdown = {
        "affordability": affordability,
        "budget_fit": budget_fit,
        "commute": commute_combined,
        "cycling": cycling_score,
        "career": career_score,
        "confidence": confidence,
    }
    score = sum(breakdown[name] * weights.get(name, 0) for name in breakdown)
    warnings = _warnings(listing, commute, leftover)
    return RankedOption(
        listing=listing,
        commute=commute,
        career=career,
        costs=cost,
        score=round(score, 2),
        monthly_net_salary=monthly_net,
        monthly_required_budget=monthly_outgoings,
        estimated_leftover=leftover,
        expense_breakdown={
            "rent": round(rent, 2),
            "utilities": round(utilities, 2),
            "groceries": round(grocery_monthly, 2),
            "other_living_costs": round(other_living_costs, 2),
            "commute": round(commute_cost, 2),
            "subscriptions_and_extra": round(extra_monthly_expenses, 2),
        },
        score_breakdown={key: round(value, 1) for key, value in breakdown.items()},
        warnings=warnings,
    )


def estimate_monthly_net_salary(annual_gross: float | None) -> float | None:
    if annual_gross is None:
        return None
    personal_allowance = 12570.0
    taxable = max(0.0, annual_gross - personal_allowance)
    basic_tax = min(taxable, 37700.0) * 0.2
    higher_tax = max(0.0, taxable - 37700.0) * 0.4
    ni = max(0.0, annual_gross - 12570.0) * 0.08
    return (annual_gross - basic_tax - higher_tax - ni) / 12


def _warnings(
    listing: AccommodationListing,
    commute: CommuteEstimate,
    leftover: float | None,
) -> list[str]:
    warnings: list[str] = []
    if listing.monthly_rent is None:
        warnings.append("Rent was not extractable from the public snippet/page; budget max is used for scoring.")
    if listing.bills_included is None:
        warnings.append("Bills/utility status unclear.")
    if commute.public_confidence < 0.5:
        warnings.append("Commute cost or duration is a low-confidence estimate.")
    if commute.cycling_duration_min and commute.cycling_duration_min > 120:
        warnings.append("Cycling commute is likely impractical for daily travel.")
    if leftover is not None and leftover < 0:
        warnings.append("Estimated monthly outgoings exceed take-home salary.")
    return warnings


def _has_source_linked_rent(listing: AccommodationListing) -> bool:
    return (
        listing.monthly_rent is not None
        and listing.confidence >= 0.68
        and bool(listing.sources)
        and listing.verification_level in {"live_result", "source_page"}
        and "search-page result" not in listing.raw_summary.lower()
    )


def _normalise_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(max(value, 0) for value in weights.values()) or 1
    return {key: max(value, 0) / total for key, value in weights.items()}


def _scale(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return max(0.0, min((value - low) / (high - low) * 100, 100.0))


def _inverse_scale(value: float, low: float, high: float) -> float:
    return 100.0 - _scale(value, low, high)
