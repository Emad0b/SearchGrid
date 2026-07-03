from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Source:
    title: str
    url: str
    snippet: str = ""


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = "web"


@dataclass(slots=True)
class City:
    name: str
    region: str
    latitude: float | None = None
    longitude: float | None = None
    notes: str = ""


@dataclass(slots=True)
class AccommodationListing:
    city: str
    title: str
    url: str
    monthly_rent: float | None
    location: str = ""
    bills_included: bool | None = None
    utilities_estimate: float | None = None
    bedrooms: int | None = None
    source_name: str = ""
    confidence: float = 0.4
    raw_summary: str = ""
    verification_level: str = "search"
    sources: list[Source] = field(default_factory=list)


@dataclass(slots=True)
class CommuteEstimate:
    city: str
    destination: str
    public_duration_min: float | None = None
    public_monthly_cost: float | None = None
    public_cost_note: str = ""
    public_confidence: float = 0.25
    cycling_duration_min: float | None = None
    cycling_distance_km: float | None = None
    cycling_confidence: float = 0.25
    cycling_note: str = ""
    sources: list[Source] = field(default_factory=list)


@dataclass(slots=True)
class JobOpportunity:
    title: str
    url: str
    location: str = ""
    role_type: str = "general"
    snippet: str = ""
    source_name: str = ""


@dataclass(slots=True)
class CareerCityData:
    city: str
    entry_salary_low: float | None = None
    entry_salary_mid: float | None = None
    entry_salary_high: float | None = None
    demand_score: float = 50.0
    demand_note: str = ""
    confidence: float = 0.35
    job_opportunities: list[JobOpportunity] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)


@dataclass(slots=True)
class CityCostData:
    city: str
    non_housing_monthly_cost: float | None = None
    utilities_monthly: float | None = None
    grocery_monthly: float | None = None
    confidence: float = 0.35
    note: str = ""
    sources: list[Source] = field(default_factory=list)


@dataclass(slots=True)
class RankedOption:
    listing: AccommodationListing
    commute: CommuteEstimate
    career: CareerCityData
    costs: CityCostData
    score: float
    monthly_net_salary: float | None
    monthly_required_budget: float
    estimated_leftover: float | None
    expense_breakdown: dict[str, float]
    score_breakdown: dict[str, float]
    warnings: list[str] = field(default_factory=list)


JsonDict = dict[str, Any]
