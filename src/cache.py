from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .config import CACHE_DIR


class JsonCache:
    def __init__(self, namespace: str, ttl_hours: int = 24) -> None:
        self.path = CACHE_DIR / namespace
        self.ttl_seconds = ttl_hours * 60 * 60
        self.path.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str | dict[str, Any]) -> Path:
        if not isinstance(key, str):
            key = json.dumps(key, sort_keys=True, default=str)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.path / f"{digest}.json"

    def get(self, key: str | dict[str, Any]) -> Any | None:
        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - float(payload.get("created_at", 0)) > self.ttl_seconds:
            return None
        return payload.get("value")

    def set(self, key: str | dict[str, Any], value: Any) -> None:
        path = self._path_for(key)
        payload = {"created_at": time.time(), "value": value}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
