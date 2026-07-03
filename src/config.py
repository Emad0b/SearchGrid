from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

try:
    import yaml
except ImportError:  # pragma: no cover - handled in README/requirements
    yaml = None

from .models import City

ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
CACHE_DIR = ROOT_DIR / ".cache"
DEFAULT_DESTINATION = "King's Cross Station, London"
DEFAULT_DESTINATION_COORDS = (51.5308, -0.1238)


@dataclass(slots=True)
class AppSettings:
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    ors_api_key: str | None = None
    tfl_app_id: str | None = None
    tfl_app_key: str | None = None
    request_timeout: int = 20
    cache_ttl_hours: int = 24
    user_agent: str = "SearchGridLivingDashboard/1.0 (+local research app)"


def load_settings() -> AppSettings:
    load_dotenv(ROOT_DIR / ".env")
    return AppSettings(
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        ors_api_key=os.getenv("ORS_API_KEY") or None,
        tfl_app_id=os.getenv("TFL_APP_ID") or None,
        tfl_app_key=os.getenv("TFL_APP_KEY") or None,
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "20")),
        cache_ttl_hours=int(os.getenv("CACHE_TTL_HOURS", "24")),
        user_agent=os.getenv("USER_AGENT", "SearchGridLivingDashboard/1.0 (+local research app)"),
    )


def load_candidate_cities() -> list[City]:
    path = DATA_DIR / "candidate_cities.yaml"
    if not path.exists() or yaml is None:
        return [
            City("London", "Greater London", 51.5072, -0.1276),
            City("Reading", "Berkshire", 51.4551, -0.9787),
            City("Luton", "Bedfordshire", 51.8787, -0.4200),
            City("Slough", "Berkshire", 51.5105, -0.5950),
            City("Watford", "Hertfordshire", 51.6565, -0.3903),
            City("Guildford", "Surrey", 51.2362, -0.5704),
            City("Brighton", "East Sussex", 50.8225, -0.1372),
            City("Cambridge", "Cambridgeshire", 52.2053, 0.1218),
            City("Oxford", "Oxfordshire", 51.7520, -1.2577),
            City("Milton Keynes", "Buckinghamshire", 52.0406, -0.7594),
            City("Birmingham", "West Midlands", 52.4862, -1.8904),
            City("Manchester", "Greater Manchester", 53.4808, -2.2426),
            City("Bristol", "Bristol", 51.4545, -2.5879),
            City("Leeds", "West Yorkshire", 53.8008, -1.5491),
        ]

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    cities: list[City] = []
    for item in data:
        cities.append(
            City(
                name=str(item["name"]),
                region=str(item.get("region", "")),
                latitude=float(item["latitude"]) if item.get("latitude") is not None else None,
                longitude=float(item["longitude"]) if item.get("longitude") is not None else None,
                notes=str(item.get("notes", "")),
            )
        )
    return cities
