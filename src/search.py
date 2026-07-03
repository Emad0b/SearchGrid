from __future__ import annotations

from dataclasses import asdict
import re
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - optional fallback below
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

from .cache import JsonCache
from .config import AppSettings
from .models import SearchResult, Source


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.replace("www.", "")


def result_to_source(result: SearchResult) -> Source:
    return Source(title=result.title, url=result.url, snippet=result.snippet)


def search_web(
    query: str,
    settings: AppSettings,
    cache: JsonCache,
    *,
    max_results: int = 8,
    region: str = "uk-en",
    backend: str = "duckduckgo,brave,yahoo,mojeek",
) -> list[SearchResult]:
    key = {
        "type": "search",
        "version": 4,
        "query": query,
        "max_results": max_results,
        "region": region,
        "backend": backend,
    }
    cached = cache.get(key)
    if cached is not None:
        return [SearchResult(**item) for item in cached]

    results: list[SearchResult] = []
    if DDGS is not None:
        try:
            with DDGS(timeout=min(settings.request_timeout, 6)) as ddgs:
                for item in ddgs.text(
                    query,
                    region=region,
                    safesearch="moderate",
                    max_results=max_results,
                    backend=backend,
                ):
                    url = item.get("href") or item.get("url") or ""
                    if not url:
                        continue
                    results.append(
                        SearchResult(
                            title=_clean_text(item.get("title", "")) or _domain(url),
                            url=url,
                            snippet=_clean_text(item.get("body", "")),
                            source="duckduckgo",
                        )
                    )
        except Exception:
            results = []

    if not results:
        results = _search_duckduckgo_html(query, settings, max_results=max_results)

    deduped: list[SearchResult] = []
    seen: set[str] = set()
    for result in results:
        if result.url in seen:
            continue
        seen.add(result.url)
        deduped.append(result)

    # Do not cache empty searches: public engines can intermittently block or time out,
    # and caching a transient miss makes the dashboard look permanently empty.
    if deduped:
        cache.set(key, [asdict(result) for result in deduped[:max_results]])
    return deduped[:max_results]


def _search_duckduckgo_html(query: str, settings: AppSettings, *, max_results: int) -> list[SearchResult]:
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        response = requests.get(
            url,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout,
        )
        response.raise_for_status()
    except requests.RequestException:
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    results: list[SearchResult] = []
    for item in soup.select(".result"):
        link = item.select_one(".result__a")
        if link is None:
            continue
        href = link.get("href", "")
        snippet = item.select_one(".result__snippet")
        results.append(
            SearchResult(
                title=_clean_text(link.get_text(" ")),
                url=href,
                snippet=_clean_text(snippet.get_text(" ") if snippet else ""),
                source="duckduckgo-html",
            )
        )
        if len(results) >= max_results:
            break
    return results


def fetch_page_text(url: str, settings: AppSettings, cache: JsonCache, *, max_chars: int = 12000) -> str:
    key = {"type": "page", "url": url, "max_chars": max_chars}
    cached = cache.get(key)
    if cached is not None:
        return str(cached)

    try:
        response = requests.get(
            url,
            headers={"User-Agent": settings.user_agent},
            timeout=settings.request_timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException:
        cache.set(key, "")
        return ""

    content_type = response.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        cache.set(key, "")
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta_description = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        meta_description = str(meta["content"])
    body = _clean_text(soup.get_text(" "))
    text = _clean_text(" ".join([title, meta_description, body]))[:max_chars]
    cache.set(key, text)
    return text


def source_markdown(sources: list[Source], *, limit: int = 5) -> str:
    chunks = []
    for source in sources[:limit]:
        title = source.title or _domain(source.url) or source.url
        chunks.append(f"[{title}]({source.url})")
    return ", ".join(chunks)
