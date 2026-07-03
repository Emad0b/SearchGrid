from __future__ import annotations

import re

from .cache import JsonCache
from .config import AppSettings
from .llm import extract_json_with_llm
from .models import CityCostData, Source
from .search import result_to_source, search_web

PRICE_RE = re.compile(r"£\s?([0-9][0-9,]*(?:\.\d{1,2})?)")


def collect_cost_data(
    city: str,
    grocery_monthly: float,
    settings: AppSettings,
    search_cache: JsonCache,
    llm_cache: JsonCache,
) -> CityCostData:
    queries = [
        f"{city} UK cost of living monthly utilities groceries rent 2026",
        f"{city} average utilities monthly cost renters UK",
        f"{city} student graduate monthly living costs UK",
    ]
    snippets: list[str] = []
    sources: list[Source] = []
    for query in queries:
        for result in search_web(query, settings, search_cache, max_results=5):
            snippets.append(f"{result.title}. {result.snippet}")
            sources.append(result_to_source(result))

    text = "\n".join(snippets)
    fallback = _heuristic_cost(city, grocery_monthly, text, sources)
    extracted = extract_json_with_llm(
        task=f"Estimate non-housing monthly living costs and utilities for {city}, UK.",
        text=text,
        schema_hint=(
            '{"non_housing_monthly_cost": number|null, "utilities_monthly": number|null, '
            '"note": string, "confidence": number}'
        ),
        settings=settings,
        cache=llm_cache,
        fallback={
            "non_housing_monthly_cost": fallback.non_housing_monthly_cost,
            "utilities_monthly": fallback.utilities_monthly,
            "note": fallback.note,
            "confidence": fallback.confidence,
        },
    )
    if not isinstance(extracted, dict):
        return fallback

    return CityCostData(
        city=city,
        non_housing_monthly_cost=_money(extracted.get("non_housing_monthly_cost"))
        or fallback.non_housing_monthly_cost,
        utilities_monthly=_money(extracted.get("utilities_monthly")) or fallback.utilities_monthly,
        grocery_monthly=grocery_monthly,
        confidence=max(0.05, min(float(extracted.get("confidence") or fallback.confidence), 0.95)),
        note=str(extracted.get("note") or fallback.note),
        sources=sources[:8],
    )


def _heuristic_cost(city: str, grocery_monthly: float, text: str, sources: list[Source]) -> CityCostData:
    prices = [_money(match.group(1)) for match in PRICE_RE.finditer(text)]
    prices = [price for price in prices if price is not None and 20 <= price <= 1200]
    utilities = _default_utilities(city)
    non_housing = _default_non_housing(city, grocery_monthly)
    if prices:
        low_monthly = [price for price in prices if 80 <= price <= 700]
        if low_monthly:
            non_housing = max(grocery_monthly, min(low_monthly, key=lambda value: abs(value - non_housing)))

    return CityCostData(
        city=city,
        non_housing_monthly_cost=non_housing,
        utilities_monthly=utilities,
        grocery_monthly=grocery_monthly,
        confidence=0.42 if sources else 0.25,
        note="Estimated from live search snippets plus city-level fallback values.",
        sources=sources[:8],
    )


def _default_utilities(city: str) -> float:
    if city in {"London", "Ilford"}:
        return 180.0 if city == "London" else 170.0
    return 145.0


def _default_non_housing(city: str, grocery_monthly: float) -> float:
    base = {
        "London": 520,
        "Ilford": 480,
        "Watford": 420,
        "Slough": 415,
        "Luton": 390,
    }.get(city, 385)
    return max(grocery_monthly, float(base))


def _money(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("£", "").replace(",", "").strip())
    except ValueError:
        return None
