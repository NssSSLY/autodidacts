from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from autodidact.config import RuntimeSettings, runtime_settings


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""


class SearchProvider(ABC):
    provider_name: str

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        raise NotImplementedError


class SearchProviderError(RuntimeError):
    pass


class DuckDuckGoHtmlSearch(SearchProvider):
    """Simple no-key fallback. Production should prefer a stable search API."""

    provider_name = "duckduckgo_html"

    def __init__(self, settings: RuntimeSettings | None = None):
        self.settings = settings or runtime_settings()

    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        headers = {"User-Agent": self.settings.http_user_agent}
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        async with httpx.AsyncClient(
            timeout=self.settings.search_timeout_seconds,
            headers=headers,
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        hits: list[SearchHit] = []
        for result in soup.select(".result"):
            anchor = result.select_one("a.result__a")
            if not anchor:
                continue
            raw_url = anchor.get("href", "")
            parsed = urlparse(raw_url)
            if "duckduckgo.com" in parsed.netloc and "uddg" in parse_qs(parsed.query):
                raw_url = unquote(parse_qs(parsed.query)["uddg"][0])
            snippet_element = result.select_one(".result__snippet")
            snippet = snippet_element.get_text(" ", strip=True) if snippet_element else ""
            hits.append(SearchHit(anchor.get_text(" ", strip=True), raw_url, snippet))
            if len(hits) >= limit:
                break
        return hits


class BraveSearchProvider(SearchProvider):
    provider_name = "brave"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.search.brave.com/res/v1/web/search",
        timeout_seconds: float = 30.0,
        user_agent: str = "Autodidact/0.1 (+local research agent)",
        client: httpx.AsyncClient | None = None,
    ):
        if not api_key:
            raise ValueError("BRAVE_SEARCH_API_KEY is required for the Brave search provider")
        self.api_key = api_key
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.client = client

    async def _request(self, query: str, limit: int) -> httpx.Response:
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self.api_key,
            "User-Agent": self.user_agent,
        }
        params = {"q": query, "count": max(1, min(limit, 20))}
        if self.client is not None:
            return await self.client.get(self.base_url, headers=headers, params=params)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            return await client.get(self.base_url, headers=headers, params=params)

    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        response = await self._request(query, limit)
        response.raise_for_status()
        payload = response.json()
        raw_results = payload.get("web", {}).get("results", [])
        hits: list[SearchHit] = []
        for item in raw_results:
            title = item.get("title")
            url = item.get("url")
            if not isinstance(title, str) or not isinstance(url, str):
                continue
            description = item.get("description", "")
            hits.append(
                SearchHit(
                    title=title,
                    url=url,
                    snippet=description if isinstance(description, str) else "",
                )
            )
            if len(hits) >= limit:
                break
        return hits


class FallbackSearchProvider(SearchProvider):
    provider_name = "fallback_chain"

    def __init__(self, providers: list[SearchProvider]):
        if not providers:
            raise ValueError("At least one search provider is required")
        self.providers = providers

    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        failures: list[str] = []
        for provider in self.providers:
            try:
                hits = await provider.search(query, limit)
                if hits:
                    return hits
                failures.append(f"{provider.provider_name}: no results")
            # Provider/network/parser failures are isolated by design.
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{provider.provider_name}: {exc}")
        raise SearchProviderError("All search providers failed: " + "; ".join(failures))


def build_search_provider(settings: RuntimeSettings | None = None) -> SearchProvider:
    settings = settings or runtime_settings()
    provider_name = settings.search_provider.strip().lower()
    duckduckgo = DuckDuckGoHtmlSearch(settings)

    if provider_name == "duckduckgo":
        return duckduckgo
    if provider_name == "brave":
        brave = BraveSearchProvider(
            settings.brave_search_api_key,
            base_url=settings.brave_search_base_url,
            timeout_seconds=settings.search_timeout_seconds,
            user_agent=settings.http_user_agent,
        )
        return FallbackSearchProvider([brave, duckduckgo])
    if provider_name == "auto":
        if not settings.brave_search_api_key:
            return duckduckgo
        brave = BraveSearchProvider(
            settings.brave_search_api_key,
            base_url=settings.brave_search_base_url,
            timeout_seconds=settings.search_timeout_seconds,
            user_agent=settings.http_user_agent,
        )
        return FallbackSearchProvider([brave, duckduckgo])
    raise ValueError(f"Unknown SEARCH_PROVIDER={settings.search_provider}")
