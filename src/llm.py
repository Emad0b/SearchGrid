from __future__ import annotations

import json
import re
from typing import Any

import requests

from .cache import JsonCache
from .config import AppSettings


def llm_enabled(settings: AppSettings) -> bool:
    return bool(settings.openai_api_key)


def extract_json_with_llm(
    *,
    task: str,
    text: str,
    schema_hint: str,
    settings: AppSettings,
    cache: JsonCache,
    fallback: dict[str, Any] | list[Any],
) -> dict[str, Any] | list[Any]:
    if not llm_enabled(settings):
        return fallback

    key = {
        "type": "llm_extract",
        "task": task,
        "schema_hint": schema_hint,
        "text": text[:14000],
        "model": settings.openai_model,
    }
    cached = cache.get(key)
    if cached is not None:
        return cached

    prompt = (
        "Extract factual data from the provided web text. "
        "Return only valid JSON matching the schema. "
        "Use null for unknown values. Do not invent facts.\n\n"
        f"Task: {task}\n"
        f"Schema: {schema_hint}\n\n"
        f"Text:\n{text[:14000]}"
    )
    try:
        response = requests.post(
            f"{settings.openai_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.openai_model,
                "messages": [
                    {"role": "system", "content": "You are a careful data extraction assistant."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
            },
            timeout=settings.request_timeout + 20,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        parsed = _parse_json(content)
    except Exception:
        return fallback

    cache.set(key, parsed)
    return parsed


def _parse_json(content: str) -> dict[str, Any] | list[Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?", "", content).strip()
        content = re.sub(r"```$", "", content).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", content, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(1))
