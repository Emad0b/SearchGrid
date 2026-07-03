from __future__ import annotations

from dataclasses import asdict
import re
from urllib.parse import urlparse

from .cache import JsonCache
from .config import AppSettings
from .llm import extract_json_with_llm
from .models import CareerCityData, JobOpportunity, Source
from .search import result_to_source, search_web

SALARY_RE = re.compile(r"£\s?([2-9][0-9](?:,[0-9]{3})?|\d{5,6})\s?(?:k|K|,000)?")
ROLE_TERMS = [
    "graduate software engineer",
    "junior software engineer",
    "data scientist",
    "machine learning",
    "AI engineer",
    "python",
    "computer science",
]
AWS_TERMS = [
    "aws solution architect",
    "aws solutions architect",
    "solution architect aws",
    "solutions architect aws",
    "aws certified solutions architect",
    "associate solutions architect",
    "professional solutions architect",
]


def collect_career_data(city: str, settings: AppSettings, search_cache: JsonCache, llm_cache: JsonCache) -> CareerCityData:
    queries = [
        f"{city} entry level software engineer AI graduate salary UK 2026",
        f"{city} graduate computer science jobs machine learning data scientist demand",
        f"site:itjobswatch.co.uk {city} graduate software engineer computer science salary",
    ]
    snippets: list[str] = []
    sources: list[Source] = []
    results_count = 0
    for query in queries:
        results = search_web(query, settings, search_cache, max_results=6)
        results_count += len(results)
        for result in results:
            snippets.append(f"{result.title}. {result.snippet}")
            sources.append(result_to_source(result))

    job_opportunities = _search_job_opportunities(city, settings, search_cache)
    for job in job_opportunities:
        sources.append(Source(job.title, job.url, job.snippet))

    text = "\n".join(snippets)
    fallback = _heuristic_career(city, text, results_count, sources, job_opportunities)
    extracted = extract_json_with_llm(
        task=f"Estimate entry-level CS/AI salary and job demand in {city}, UK from public web snippets.",
        text=text,
        schema_hint=(
            '{"entry_salary_low": number|null, "entry_salary_mid": number|null, '
            '"entry_salary_high": number|null, "demand_score": number, '
            '"demand_note": string, "confidence": number}'
        ),
        settings=settings,
        cache=llm_cache,
        fallback=asdict(fallback),
    )
    if not isinstance(extracted, dict):
        return fallback

    demand_note = str(extracted.get("demand_note") or fallback.demand_note)
    if job_opportunities:
        aws_count = sum(1 for job in job_opportunities if job.role_type == "aws_architect")
        cs_count = sum(1 for job in job_opportunities if job.role_type == "cs_ai")
        demand_note = (
            f"{demand_note} Found {len(job_opportunities)} live job signal(s)"
            f" ({cs_count} CS/AI, {aws_count} AWS Solution Architect)."
        )

    return CareerCityData(
        city=city,
        entry_salary_low=_salary(extracted.get("entry_salary_low")) or fallback.entry_salary_low,
        entry_salary_mid=_salary(extracted.get("entry_salary_mid")) or fallback.entry_salary_mid,
        entry_salary_high=_salary(extracted.get("entry_salary_high")) or fallback.entry_salary_high,
        demand_score=max(0, min(float(extracted.get("demand_score") or fallback.demand_score), 100)),
        demand_note=demand_note,
        confidence=max(0.05, min(float(extracted.get("confidence") or fallback.confidence), 0.95)),
        job_opportunities=job_opportunities,
        sources=sources[:12],
    )


def _search_job_opportunities(city: str, settings: AppSettings, search_cache: JsonCache) -> list[JobOpportunity]:
    queries = [
        f"{city} graduate software engineer jobs UK 2026",
        f"{city} junior AI machine learning developer jobs UK",
        f"{city} AWS solution architect jobs UK",
        f"London AWS solution architect jobs UK",
    ]
    if city == "Ilford":
        queries.extend(
            [
                "Ilford Redbridge software developer tech jobs UK",
                "New North Road Ilford jobs software engineer",
                "East London Ilford AWS solution architect jobs",
                "Redbridge Essex computer science graduate jobs",
            ]
        )
    if city == "London":
        queries.extend(
            [
                "London Shoreditch software engineer graduate jobs",
                "London AWS certified solutions architect associate jobs",
            ]
        )

    seen_urls: set[str] = set()
    opportunities: list[JobOpportunity] = []
    for query in queries:
        for result in search_web(query, settings, search_cache, max_results=5):
            if result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            haystack = f"{result.title} {result.snippet}".lower()
            if not _looks_like_job_result(haystack):
                continue
            role_type = _classify_role(haystack)
            opportunities.append(
                JobOpportunity(
                    title=result.title.strip() or "Job listing",
                    url=result.url,
                    location=city if city.lower() in haystack else _infer_location(city, haystack),
                    role_type=role_type,
                    snippet=result.snippet.strip(),
                    source_name=_job_source_name(result.url),
                )
            )
            if len(opportunities) >= 12:
                return _sort_job_opportunities(opportunities)
    return _sort_job_opportunities(opportunities)


def _heuristic_career(
    city: str,
    text: str,
    results_count: int,
    sources: list[Source],
    job_opportunities: list[JobOpportunity],
) -> CareerCityData:
    salaries = _extract_salaries(text)
    if salaries:
        low = min(salaries)
        high = max(salaries)
        mid = sum(salaries) / len(salaries)
    else:
        low, mid, high = _city_salary_default(city)

    lowered = text.lower()
    role_hits = sum(lowered.count(term.lower()) for term in ROLE_TERMS)
    city_bonus = {
        "London": 18,
        "Ilford": 14,
        "Watford": 8,
        "Slough": 9,
        "Luton": 7,
    }.get(city, 5)
    aws_jobs = sum(1 for job in job_opportunities if job.role_type == "aws_architect")
    cs_jobs = sum(1 for job in job_opportunities if job.role_type == "cs_ai")
    demand_score = min(100, 35 + city_bonus + results_count * 2 + role_hits * 1.5 + cs_jobs * 2 + aws_jobs * 3)
    note = "Demand score estimated from live search results, role mentions, and known UK tech clusters."
    if job_opportunities:
        note = (
            f"{note} Includes {len(job_opportunities)} job listing signal(s) "
            f"({cs_jobs} CS/AI, {aws_jobs} AWS Solution Architect)."
        )
    return CareerCityData(
        city=city,
        entry_salary_low=low,
        entry_salary_mid=mid,
        entry_salary_high=high,
        demand_score=demand_score,
        demand_note=note,
        confidence=0.45 if sources else 0.25,
        job_opportunities=job_opportunities,
        sources=sources[:12],
    )


def _looks_like_job_result(text: str) -> bool:
    job_terms = [
        "job",
        "jobs",
        "vacancy",
        "vacancies",
        "hiring",
        "career",
        "graduate",
        "engineer",
        "developer",
        "architect",
        "software",
        "machine learning",
        "data scientist",
    ]
    return any(term in text for term in job_terms)


def _classify_role(text: str) -> str:
    if any(term in text for term in AWS_TERMS) or ("aws" in text and "architect" in text):
        return "aws_architect"
    if any(term in text for term in ROLE_TERMS):
        return "cs_ai"
    return "general"


def _infer_location(city: str, text: str) -> str:
    for place in ("Ilford", "Redbridge", "London", "Essex", "East London"):
        if place.lower() in text:
            return place
    return city


def _job_source_name(url: str) -> str:
    host = urlparse(url).netloc.replace("www.", "")
    return host or "web"


def _sort_job_opportunities(opportunities: list[JobOpportunity]) -> list[JobOpportunity]:
    priority = {"aws_architect": 0, "cs_ai": 1, "general": 2}
    return sorted(opportunities, key=lambda item: (priority.get(item.role_type, 3), item.title.lower()))


def _extract_salaries(text: str) -> list[float]:
    values: list[float] = []
    for match in SALARY_RE.finditer(text):
        raw = match.group(1).replace(",", "")
        value = float(raw)
        if value < 100:
            value *= 1000
        if 18000 <= value <= 80000:
            values.append(value)
    return values[:12]


def _city_salary_default(city: str) -> tuple[float, float, float]:
    defaults = {
        "London": (30000, 34000, 40000),
        "Ilford": (29000, 33000, 39000),
        "Watford": (27000, 31000, 36000),
        "Slough": (27000, 31000, 36000),
        "Luton": (26000, 30000, 35000),
    }
    return defaults.get(city, (24000, 28000, 33000))


def _salary(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(str(value).replace("£", "").replace(",", "").strip())
    except ValueError:
        return None
    if parsed < 100:
        parsed *= 1000
    return parsed if 10000 <= parsed <= 120000 else None
