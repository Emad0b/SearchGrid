from __future__ import annotations

import math
import re
from urllib.parse import quote

import requests

from .cache import JsonCache
from .config import AppSettings, DEFAULT_DESTINATION_COORDS
from .models import City, CommuteEstimate, Source
from .search import result_to_source, search_web

DURATION_RE = re.compile(r"\b(?:(\d+(?:\.\d+)?)\s*h(?:ours?)?)?\s*(?:(\d{1,3})\s*m(?:in(?:ute)?s?)?)", re.I)
PRICE_RE = re.compile(r"£\s?([0-9][0-9,]*(?:\.\d{1,2})?)")

TECH_DESTINATIONS = {
    "Shoreditch / Old Street": ("Old Street Station, London", 51.5256, -0.0875),
    "King's Cross / Knowledge Quarter": ("King's Cross Station, London", 51.5308, -0.1238),
    "Liverpool Street / City": ("Liverpool Street Station, London", 51.5178, -0.0817),
    "Canary Wharf": ("Canary Wharf Station, London", 51.5054, -0.0235),
    "Paddington / West London": ("Paddington Station, London", 51.5154, -0.1755),
    "White City / Scale Space": ("White City Station, London", 51.5122, -0.2247),
    "South Bank / Waterloo": ("Waterloo Station, London", 51.5033, -0.1136),
}


def estimate_commute(
    city: City,
    destination: str,
    settings: AppSettings,
    cache: JsonCache,
) -> CommuteEstimate:
    estimate = CommuteEstimate(city=city.name, destination=destination)
    sources: list[Source] = []

    tfl_duration = _tfl_duration(city.name, destination, settings, cache)
    if tfl_duration is not None:
        estimate.public_duration_min = tfl_duration
        estimate.public_confidence = 0.7
        estimate.public_cost_note = "Duration from TfL Journey Planner; cost estimated separately."

    rail = _web_rail_fallback(city.name, destination, settings, cache)
    sources.extend(rail["sources"])
    if estimate.public_duration_min is None and rail["duration_min"] is not None:
        estimate.public_duration_min = rail["duration_min"]
        estimate.public_confidence = 0.45
    if rail["monthly_cost"] is not None:
        estimate.public_monthly_cost = rail["monthly_cost"]
        estimate.public_cost_note = rail["cost_note"]
        estimate.public_confidence = max(estimate.public_confidence, 0.45)
    else:
        estimate.public_monthly_cost = _fallback_monthly_commute_cost(city.name)
        estimate.public_cost_note = "Fallback estimate; verify with National Rail/TfL before budgeting."
        estimate.public_confidence = max(estimate.public_confidence, _fallback_commute_confidence(city.name))

    cycling = _cycling_route(city, destination, settings, cache)
    estimate.cycling_duration_min = cycling["duration_min"]
    estimate.cycling_distance_km = cycling["distance_km"]
    estimate.cycling_confidence = cycling["confidence"]
    estimate.cycling_note = cycling["note"]
    sources.extend(cycling["sources"])

    estimate.sources = sources[:8]
    return estimate


def estimate_tech_commute(
    city: City,
    settings: AppSettings,
    cache: JsonCache,
    *,
    tech_areas: list[str] | None = None,
) -> CommuteEstimate:
    selected = [area for area in (tech_areas or list(TECH_DESTINATIONS)) if area in TECH_DESTINATIONS]
    if not selected:
        selected = list(TECH_DESTINATIONS)

    best_area = _closest_tech_area_by_bike(city, selected)
    destination, _, _ = TECH_DESTINATIONS[best_area]
    estimate = estimate_commute(city, destination, settings, cache)
    estimate.destination = best_area

    bike = _cycling_estimate_to_tech_area(city, best_area)
    estimate.cycling_duration_min = bike["duration_min"]
    estimate.cycling_distance_km = bike["distance_km"]
    estimate.cycling_confidence = bike["confidence"]
    estimate.cycling_note = (
        f"Estimated bicycle commute to {best_area}, the closest selected London tech cluster by cycling distance."
    )
    return estimate


def _closest_tech_area_by_bike(city: City, tech_areas: list[str]) -> str:
    if city.latitude is None or city.longitude is None:
        return tech_areas[0]
    return min(
        tech_areas,
        key=lambda area: _cycling_estimate_to_tech_area(city, area)["duration_min"] or float("inf"),
    )


def _cycling_estimate_to_tech_area(city: City, tech_area: str) -> dict[str, float]:
    _, lat, lon = TECH_DESTINATIONS[tech_area]
    if city.latitude is None or city.longitude is None:
        return {"duration_min": None, "distance_km": None, "confidence": 0.1}
    route_multiplier = 1.2 if city.name == "London" else 1.28
    distance = _haversine_km(city.latitude, city.longitude, lat, lon) * route_multiplier
    duration = distance / 16 * 60
    return {"duration_min": duration, "distance_km": distance, "confidence": 0.45}


def _tfl_duration(city: str, destination: str, settings: AppSettings, cache: JsonCache) -> float | None:
    key = {"type": "tfl_journey", "from": city, "to": destination}
    cached = cache.get(key)
    if cached is not None:
        return cached

    params = {
        "mode": "national-rail,tube,elizabeth-line,overground,dlr,tram,bus,walking",
        "timeIs": "Arriving",
    }
    if settings.tfl_app_id and settings.tfl_app_key:
        params["app_id"] = settings.tfl_app_id
        params["app_key"] = settings.tfl_app_key
    url = f"https://api.tfl.gov.uk/Journey/JourneyResults/{quote(city)}/to/{quote(destination)}"
    try:
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout,
        )
        if response.status_code in {300, 404}:
            cache.set(key, None)
            return None
        response.raise_for_status()
        journeys = response.json().get("journeys", [])
    except Exception:
        cache.set(key, None)
        return None

    durations = [journey.get("duration") for journey in journeys if journey.get("duration")]
    value = float(min(durations)) if durations else None
    cache.set(key, value)
    return value


def _web_rail_fallback(city: str, destination: str, settings: AppSettings, cache: JsonCache) -> dict[str, object]:
    queries = [
        f"{city} to London train duration monthly season ticket cost 2026",
        f"{city} to {destination} commute train fare duration",
    ]
    snippets: list[str] = []
    sources: list[Source] = []
    for query in queries:
        results = search_web(query, settings, cache, max_results=5)
        for result in results:
            snippets.append(f"{result.title}. {result.snippet}")
            sources.append(result_to_source(result))

    text = " ".join(snippets)
    duration = _extract_duration(text)
    price = _extract_commute_price(text)
    if price is not None and price < 80:
        monthly_cost = price * 20
        note = "Approximate monthly cost from day/single fares found online multiplied by 20 commute days."
    elif price is not None:
        monthly_cost = price
        note = "Approximate monthly or season-ticket cost extracted from public web results."
    else:
        monthly_cost = None
        note = ""

    return {
        "duration_min": duration,
        "monthly_cost": monthly_cost,
        "cost_note": note,
        "sources": sources,
    }


def _cycling_route(city: City, destination: str, settings: AppSettings, cache: JsonCache) -> dict[str, object]:
    dest_coords = _geocode(destination, settings, cache) or DEFAULT_DESTINATION_COORDS
    if city.latitude is None or city.longitude is None:
        return {
            "duration_min": None,
            "distance_km": None,
            "confidence": 0.1,
            "note": "No city coordinates available for cycling estimate.",
            "sources": [],
        }

    if settings.ors_api_key:
        key = {"type": "ors_cycle", "from": city.name, "to": destination, "dest": dest_coords}
        cached = cache.get(key)
        if cached is not None:
            cached["sources"] = [Source(**source) for source in cached.get("sources", [])]
            return cached
        try:
            response = requests.post(
                "https://api.openrouteservice.org/v2/directions/cycling-regular",
                headers={
                    "Authorization": settings.ors_api_key,
                    "Content-Type": "application/json",
                    "User-Agent": settings.user_agent,
                },
                json={
                    "coordinates": [[city.longitude, city.latitude], [dest_coords[1], dest_coords[0]]],
                    "units": "km",
                    "instructions": False,
                },
                timeout=settings.request_timeout,
            )
            response.raise_for_status()
            summary = response.json()["routes"][0]["summary"]
            value = {
                "duration_min": float(summary["duration"]) / 60,
                "distance_km": float(summary["distance"]),
                "confidence": 0.8,
                "note": "Cycling route from OpenRouteService.",
                "sources": [{"title": "OpenRouteService cycling directions", "url": "https://openrouteservice.org/", "snippet": ""}],
            }
            cache.set(key, value)
            value["sources"] = [Source(**source) for source in value["sources"]]
            return value
        except Exception:
            pass

    distance = _haversine_km(city.latitude, city.longitude, dest_coords[0], dest_coords[1]) * 1.25
    duration = distance / 16 * 60
    return {
        "duration_min": duration,
        "distance_km": distance,
        "confidence": 0.35,
        "note": "Estimated from straight-line distance with a road-route multiplier at 16 km/h.",
        "sources": [],
    }


def _geocode(place: str, settings: AppSettings, cache: JsonCache) -> tuple[float, float] | None:
    if not place or place.lower() in {"central london", "london"}:
        return DEFAULT_DESTINATION_COORDS
    key = {"type": "nominatim", "place": place}
    cached = cache.get(key)
    if cached is not None:
        return tuple(cached) if cached else None
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place, "format": "json", "limit": 1, "countrycodes": "gb"},
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not data:
            cache.set(key, None)
            return None
        coords = (float(data[0]["lat"]), float(data[0]["lon"]))
        cache.set(key, coords)
        return coords
    except Exception:
        cache.set(key, None)
        return None


def _extract_duration(text: str) -> float | None:
    candidates: list[float] = []
    for match in DURATION_RE.finditer(text):
        hours = float(match.group(1) or 0)
        mins = float(match.group(2) or 0)
        total = hours * 60 + mins
        if 10 <= total <= 360:
            candidates.append(total)
    return min(candidates) if candidates else None


def _extract_commute_price(text: str) -> float | None:
    candidates = [float(match.group(1).replace(",", "")) for match in PRICE_RE.finditer(text)]
    plausible = [price for price in candidates if 2 <= price <= 900]
    if not plausible:
        return None
    return min(plausible, key=lambda value: abs(value - 250))


def _fallback_monthly_commute_cost(city: str) -> float:
    inner_london = {"London", "Ilford"}
    londonish = {"Watford", "Slough", "Luton"}
    if city in inner_london:
        return 180.0 if city == "London" else 220.0
    if city in londonish:
        return 300.0
    return 120.0


def _fallback_commute_confidence(city: str) -> float:
    if city == "London":
        return 0.55
    if city in {"Ilford", "Watford", "Slough", "Luton"}:
        return 0.45
    return 0.35


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
