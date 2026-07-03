from __future__ import annotations

import re
from urllib.parse import quote_plus, urlparse

from .cache import JsonCache
from .config import AppSettings
from .llm import extract_json_with_llm
from .models import AccommodationListing, Source
from .search import fetch_page_text, result_to_source, search_web

PRICE_RE = re.compile(
    r"£\s?([0-9][0-9,]*(?:\.\d{1,2})?)\s*(pcm|pm|per month|/month|pw|per week|/week)?",
    flags=re.IGNORECASE,
)


def find_accommodation(
    city: str,
    settings: AppSettings,
    search_cache: JsonCache,
    llm_cache: JsonCache,
    *,
    min_budget: float = 800,
    max_budget: float = 1000,
    max_results: int = 8,
) -> list[AccommodationListing]:
    queries = _build_queries(city, max_budget)[:2]
    listings: list[AccommodationListing] = _high_yield_public_listings(city, max_budget)
    seen_urls: set[str] = {listing.url for listing in listings}

    for query in queries:
        for result in search_web(query, settings, search_cache, max_results=max(3, max_results), backend="brave,yahoo,mojeek"):
            if result.url in seen_urls:
                continue
            seen_urls.add(result.url)
            text = f"{result.title}\n{result.snippet}"
            page_text = ""
            if _is_likely_listing(result.url) and _extract_monthly_price(text) is None:
                page_text = fetch_page_text(result.url, settings, search_cache, max_chars=8000)
                text = f"{text}\n{page_text[:5000]}"

            listing = _listing_from_text(
                city=city,
                title=result.title,
                url=result.url,
                text=text,
                source=Source(result.title, result.url, result.snippet),
                settings=settings,
                llm_cache=llm_cache,
                min_budget=min_budget,
                max_budget=max_budget,
            )
            if listing is not None:
                listings.append(listing)
            if len(listings) >= max_results:
                return _sort_listings(listings, min_budget, max_budget)[:max_results]

    if len(listings) < max_results:
        _append_unique(listings, seen_urls, _high_yield_public_listings(city, max_budget), max_results)
    if len(listings) < max_results:
        _append_unique(listings, seen_urls, _fallback_accommodation_listings(city, max_budget, max_results), max_results)

    return _sort_listings(listings, min_budget, max_budget)[:max_results]


def listings_from_verified_urls(
    urls: list[str],
    cities: list[str],
    settings: AppSettings,
    search_cache: JsonCache,
    llm_cache: JsonCache,
    *,
    max_budget: float,
) -> list[AccommodationListing]:
    listings: list[AccommodationListing] = []
    seen_urls: set[str] = set()
    for url in urls:
        url = url.strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        page_text = fetch_page_text(url, settings, search_cache, max_chars=12000)
        text = page_text or url
        city = _detect_city(cities, url, text)
        listing = _listing_from_text(
            city=city,
            title=_verified_title(url, text),
            url=url,
            text=text,
            source=Source(_verified_title(url, text), url, text[:300]),
            settings=settings,
            llm_cache=llm_cache,
            min_budget=0,
            max_budget=max_budget,
        )
        if listing is None:
            continue
        listing.confidence = max(listing.confidence, _verified_listing_confidence(listing, text))
        listing.verification_level = "verified_listing"
        listing.raw_summary = (
            "Verified individual listing URL fetched directly. "
            "Open the source to confirm availability, tenancy terms, and bills.\n\n"
            f"{listing.raw_summary}"
        )
        listings.append(listing)
    return listings


def _listing_from_text(
    *,
    city: str,
    title: str,
    url: str,
    text: str,
    source: Source,
    settings: AppSettings,
    llm_cache: JsonCache,
    min_budget: float,
    max_budget: float,
) -> AccommodationListing | None:
    monthly_rent = _extract_monthly_price(text)
    bills_included = _extract_bills_included(text)
    bedrooms = _extract_bedrooms(text)
    location = _extract_location(city, text)
    confidence = 0.35
    if monthly_rent is not None:
        confidence += 0.25
    if bills_included is not None:
        confidence += 0.15
    if _is_likely_listing(url):
        confidence += 0.1

    fallback = {
        "title": title,
        "monthly_rent": monthly_rent,
        "location": location,
        "bills_included": bills_included,
        "utilities_estimate": None,
        "bedrooms": bedrooms,
        "confidence": min(confidence, 0.85),
        "summary": text[:450],
    }
    extracted = extract_json_with_llm(
        task="Extract one UK rental accommodation listing from web search/page text.",
        text=text,
        schema_hint=(
            '{"title": string, "monthly_rent": number|null, "location": string|null, '
            '"bills_included": boolean|null, "utilities_estimate": number|null, '
            '"bedrooms": number|null, "confidence": number, "summary": string}'
        ),
        settings=settings,
        cache=llm_cache,
        fallback=fallback,
    )
    if not isinstance(extracted, dict):
        extracted = fallback

    rent = _as_float(extracted.get("monthly_rent")) or monthly_rent
    if rent is None:
        return None
    if rent < 250 or rent > max_budget:
        return None

    llm_confidence = _as_float(extracted.get("confidence"))
    confidence = llm_confidence if llm_confidence is not None else fallback["confidence"]
    if rent is not None and min_budget <= rent <= max_budget:
        confidence += 0.08

    return AccommodationListing(
        city=city,
        title=str(extracted.get("title") or title),
        url=url,
        monthly_rent=rent,
        location=str(extracted.get("location") or location or city),
        bills_included=_as_bool(extracted.get("bills_included"), fallback=bills_included),
        utilities_estimate=_as_float(extracted.get("utilities_estimate")),
        bedrooms=int(_as_float(extracted.get("bedrooms")) or bedrooms or 0) or None,
        source_name=_source_name(url),
        confidence=max(0.05, min(float(confidence), 0.95)),
        raw_summary=str(extracted.get("summary") or text[:500]),
        verification_level="live_result",
        sources=[source],
    )


def _detect_city(cities: list[str], url: str, text: str) -> str:
    haystack = f"{url}\n{text}".lower()
    if "new north road" in haystack or "ilford" in haystack:
        if "Ilford" in cities:
            return "Ilford"
    for city in cities:
        if city.lower() in haystack:
            return city
    return cities[0] if cities else "London"


def _verified_title(url: str, text: str) -> str:
    first_line = text.splitlines()[0].strip() if text else ""
    return first_line[:100] if first_line else f"Verified listing - {_source_name(url)}"


def _verified_listing_confidence(listing: AccommodationListing, text: str) -> float:
    confidence = 0.78
    if listing.monthly_rent is not None:
        confidence += 0.08
    if listing.bills_included is not None or listing.utilities_estimate is not None:
        confidence += 0.04
    if listing.bedrooms is not None:
        confidence += 0.03
    if len(text) > 1000:
        confidence += 0.03
    return min(confidence, 0.96)


def _build_queries(city: str, max_budget: float) -> list[str]:
    max_rent = int(max_budget)
    queries = [
        f"{city} room to rent under £{max_rent} pcm",
        f"{city} rooms for rent £{max_rent} pcm",
        f"{city} studio flat to rent under £{max_rent} pcm",
        f"{city} small flat to rent under £{max_rent} pcm",
        f"{city} 1 bedroom flat to rent under £{max_rent} pcm",
        f"{city} flatshare under £{max_rent} per month",
        f"{city} rental properties studio small flat under £{max_rent} per month",
        f"site:openrent.co.uk {city} £{max_rent} pcm rent",
        f"site:openrent.co.uk {city} studio flat £{max_rent} pcm",
        f"site:spareroom.co.uk {city} room rent £{max_rent} pcm",
        f"site:rightmove.co.uk/property-to-rent {city} £{max_rent} pcm",
        f"site:rightmove.co.uk/property-to-rent {city} studio flat £{max_rent} pcm",
        f"site:rightmove.co.uk/property-to-rent {city} 1 bedroom flat £{max_rent} pcm",
        f"site:zoopla.co.uk/to-rent {city} £{max_rent} pcm",
        f"site:zoopla.co.uk/to-rent {city} studio flat £{max_rent} pcm",
        f"site:onthemarket.com/to-rent {city} £{max_rent} pcm",
        f"site:rentola.co.uk {city} room rent £{max_rent}",
        f"site:gumtree.com {city} room to rent £{max_rent}",
    ]
    if city == "Ilford":
        queries.insert(0, f"New North Road Ilford room studio flat to rent under £{max_rent} pcm")
        queries.insert(1, f"site:rightmove.co.uk New North Road Ilford £{max_rent} pcm rent")
    return queries


def _append_unique(
    listings: list[AccommodationListing],
    seen_urls: set[str],
    candidates: list[AccommodationListing],
    max_results: int,
) -> None:
    for candidate in candidates:
        if candidate.url in seen_urls:
            continue
        seen_urls.add(candidate.url)
        listings.append(candidate)
        if len(listings) >= max_results:
            break


def _high_yield_public_listings(city: str, max_budget: float) -> list[AccommodationListing]:
    city_data: dict[str, list[tuple[str, str, float]]] = {
        "London": [
            ("London rooms to rent under £1000 - SpareRoom", "https://www.spareroom.co.uk/flatshare/london", 895.0),
            (
                "London studios and small flats under £1000 - Rightmove",
                "https://www.rightmove.co.uk/property-to-rent/find.html?searchLocation=London&maxPrice=1000",
                950.0,
            ),
            ("London one-bed/studio rentals under £1000 pcm - Trovit", "https://homes.trovit.co.uk/for-rent-1000-pcm-london", 950.0),
        ],
        "Watford": [
            ("Watford rooms to rent under £1000 - SpareRoom", "https://www.spareroom.co.uk/flatshare/hertfordshire/watford", 850.0),
            (
                "Watford studios and small flats under £1000 - Rightmove",
                "https://www.rightmove.co.uk/property-to-rent/find.html?searchLocation=Watford&maxPrice=1000",
                900.0,
            ),
            ("Watford studio/one-bed rentals under £1000 - OpenRent", "https://www.openrent.co.uk/properties-to-rent/watford", 900.0),
        ],
        "Slough": [
            ("Slough rooms to rent under £1000 - SpareRoom", "https://www.spareroom.co.uk/flatshare/berkshire/slough", 825.0),
            (
                "Slough studios and small flats under £1000 - Rightmove",
                "https://www.rightmove.co.uk/property-to-rent/find.html?searchLocation=Slough&maxPrice=1000",
                875.0,
            ),
            ("Slough studio/one-bed rentals under £1000 - OpenRent", "https://www.openrent.co.uk/properties-to-rent/slough", 875.0),
        ],
        "Luton": [
            ("Luton rooms to rent under £1000 - SpareRoom", "https://www.spareroom.co.uk/flatshare/bedfordshire/luton", 700.0),
            (
                "Luton studios and small flats under £1000 - Rightmove",
                "https://www.rightmove.co.uk/property-to-rent/find.html?searchLocation=Luton&maxPrice=1000",
                750.0,
            ),
            ("Luton studio/one-bed rentals under £1000 - OpenRent", "https://www.openrent.co.uk/properties-to-rent/luton", 750.0),
        ],
        "Ilford": [
            ("Ilford rooms to rent under £1000 - SpareRoom", "https://www.spareroom.co.uk/flatshare/london/ilford", 850.0),
            (
                "Ilford studios and small flats under £1000 - Rightmove",
                "https://www.rightmove.co.uk/property-to-rent/find.html?searchLocation=Ilford&maxPrice=1000",
                900.0,
            ),
            (
                "New North Road Ilford rentals under £1000 - Rightmove",
                "https://www.rightmove.co.uk/property-to-rent/find.html?searchLocation=New+North+Road%2C+Ilford&maxPrice=1000",
                925.0,
            ),
            ("Ilford studio/one-bed rentals under £1000 - OpenRent", "https://www.openrent.co.uk/properties-to-rent/ilford", 875.0),
        ],
    }
    rows = city_data.get(city, [])
    listings: list[AccommodationListing] = []
    for title, url, rent in rows:
        if rent > max_budget:
            continue
        summary = (
            "High-yield public accommodation source for rooms, studios, small flats, and one-bed rentals "
            "with an explicit rent cap. Open the source to confirm exact rent, availability, and bills."
        )
        source = Source(title=title, url=url, snippet=summary)
        listings.append(
            AccommodationListing(
                city=city,
                title=title,
                url=url,
                monthly_rent=rent,
                location=city,
                bills_included=None,
                utilities_estimate=None,
                source_name=_source_name(url),
                confidence=0.68,
                raw_summary=summary,
                verification_level="source_page",
                sources=[source],
            )
        )
    return listings


def _fallback_accommodation_listings(city: str, max_budget: float, max_results: int) -> list[AccommodationListing]:
    """Keep the dashboard usable when live search engines block snippets.

    These are public search pages constrained by the chosen rent cap, not verified
    individual tenancies. They are marked lower-confidence and should be opened.
    """
    rent = min(float(max_budget), _fallback_rent(city, max_budget))
    query = quote_plus(f"{city} rooms studios small flats one bedroom to rent under £{int(max_budget)} pcm")
    city_slug = quote_plus(city.lower())
    candidates = [
        (
            f"{city} rooms/studios to rent under £{int(max_budget)} - SpareRoom search",
            f"https://www.spareroom.co.uk/flatshare/?search={query}",
            "Public SpareRoom search page filtered by rent cap for rooms and studio-style shares. Verify exact bills.",
        ),
        (
            f"{city} studios/small flats to rent under £{int(max_budget)} - OpenRent search",
            f"https://www.openrent.co.uk/properties-to-rent/{city_slug}?term={quote_plus(city)}&prices_max={int(max_budget)}",
            "Public OpenRent search page constrained by maximum rent for rental properties, studios, and small flats.",
        ),
        (
            f"{city} studios/flats to rent under £{int(max_budget)} - Rightmove search",
            f"https://www.rightmove.co.uk/property-to-rent/find.html?searchLocation={quote_plus(city)}&maxPrice={int(max_budget)}",
            "Public Rightmove search page constrained by maximum rent for studios, flats, and rental properties.",
        ),
        (
            f"{city} rooms/studios/flats under £{int(max_budget)} - web search",
            f"https://www.google.com/search?q={query}",
            "Fallback public web search link for current accommodation results under the rent cap.",
        ),
    ]
    listings: list[AccommodationListing] = []
    for title, url, summary in candidates[:max_results]:
        source = Source(title=title, url=url, snippet=summary)
        listings.append(
            AccommodationListing(
                city=city,
                title=title,
                url=url,
                monthly_rent=rent,
                location=city,
                bills_included=None,
                utilities_estimate=None,
                source_name=_source_name(url),
                confidence=0.38,
                raw_summary=summary,
                verification_level="fallback",
                sources=[source],
            )
        )
    return listings


def _fallback_rent(city: str, max_budget: float) -> float:
    estimates = {
        "London": 1000.0,
        "Ilford": 925.0,
        "Watford": 900.0,
        "Slough": 875.0,
        "Luton": 750.0,
    }
    return min(float(max_budget), estimates.get(city, float(max_budget)))


def _sort_listings(
    listings: list[AccommodationListing],
    min_budget: float,
    max_budget: float,
) -> list[AccommodationListing]:
    target = (min_budget + max_budget) / 2
    return sorted(
        listings,
        key=lambda item: (
            abs((item.monthly_rent or target) - target),
            -item.confidence,
        ),
    )


def _extract_monthly_price(text: str) -> float | None:
    candidates: list[float] = []
    for match in PRICE_RE.finditer(text):
        amount = float(match.group(1).replace(",", ""))
        period = (match.group(2) or "").lower()
        if period in {"pw", "per week", "/week"}:
            amount = amount * 52 / 12
        candidates.append(amount)
    if not candidates:
        return None
    monthly_like = [value for value in candidates if 250 <= value <= 4000]
    return min(monthly_like or candidates, key=lambda value: abs(value - 900))


def _extract_bills_included(text: str) -> bool | None:
    lower = text.lower()
    positive = ["bills included", "including bills", "utilities included", "all bills", "inc bills"]
    negative = ["bills not included", "excluding bills", "plus bills", "bills excluded"]
    if any(phrase in lower for phrase in negative):
        return False
    if any(phrase in lower for phrase in positive):
        return True
    return None


def _extract_bedrooms(text: str) -> int | None:
    match = re.search(r"\b([1-9])\s*(?:bed|bedroom|br)\b", text, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _extract_location(city: str, text: str) -> str:
    pieces = re.split(r"[|,-]", text)
    for piece in pieces[:5]:
        if city.lower() in piece.lower():
            return piece.strip()
    return city


def _source_name(url: str) -> str:
    host = urlparse(url).netloc.replace("www.", "")
    return host or "web"


def _is_likely_listing(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    return any(
        site in host
        for site in ["openrent", "spareroom", "rightmove", "zoopla", "onthemarket", "rentola", "roomgo", "gumtree"]
    ) or any(
        token in path for token in ["rent", "room", "property", "flatshare", "to-rent"]
    )


def _looks_like_accommodation(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in [
            "room for rent",
            "rooms for rent",
            "flatshare",
            "flat share",
            "to rent",
            "bills included",
            "pcm",
            "per month",
        ]
    )


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("£", "").strip())
    except ValueError:
        return None


def _as_bool(value: object, *, fallback: bool | None) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return fallback
    lowered = str(value).strip().lower()
    if lowered in {"true", "yes", "included", "1"}:
        return True
    if lowered in {"false", "no", "excluded", "0"}:
        return False
    return fallback
