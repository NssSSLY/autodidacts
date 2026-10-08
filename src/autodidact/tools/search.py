# 文件职责：抽象网络搜索，提供 Brave API、DuckDuckGo HTML、降级和结果 URL 去重。
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from autodidact.config import RuntimeSettings, runtime_settings
from autodidact.knowledge.sources import normalize_url


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""


# 功能：过滤非 HTTP(S) 结果，规范 URL 去重并截取请求数量。
def deduplicate_search_hits(hits: list[SearchHit], limit: int) -> list[SearchHit]:
    """Keep only safe HTTP(S) results with distinct normalized URLs."""
    unique: list[SearchHit] = []
    seen: set[str] = set()
    for hit in hits:
        normalized = normalize_url(hit.url)
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(SearchHit(hit.title, normalized, hit.snippet))
        if len(unique) >= limit:
            break
    return unique


class SearchProvider(ABC):
    provider_name: str

    # 功能：声明查询文本到 SearchHit 列表的异步协议，结果只是待阅读线索。
    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        raise NotImplementedError


class SearchProviderError(RuntimeError):
    pass


class DuckDuckGoHtmlSearch(SearchProvider):
    """Simple no-key fallback. Production should prefer a stable search API."""

    provider_name = "duckduckgo_html"

    # 功能：保存超时和 User-Agent 等运行配置，用于无密钥降级搜索。
    def __init__(self, settings: RuntimeSettings | None = None):
        self.settings = settings or runtime_settings()

    # 功能：请求并解析 DuckDuckGo HTML 结果，解开跳转链接并过滤重复 URL。
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
        from autodidact.provider_runtime import reject_challenge

        reject_challenge(response.text)
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
        return deduplicate_search_hits(hits, limit)


class BraveSearchProvider(SearchProvider):
    provider_name = "brave"

    # 功能：校验 API 密钥并保存地址、超时及可注入 HTTP 客户端。
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

    # 功能：组装认证头和查询数量，使用注入或临时客户端请求 Brave API。
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

    # 功能：解析 Brave JSON 的有效标题/URL/摘要，并规范去重返回。
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
        return deduplicate_search_hits(hits, limit)


class FallbackSearchProvider(SearchProvider):
    provider_name = "fallback_chain"

    # 功能：校验至少一个提供方，保存有序降级链。
    def __init__(self, providers: list[SearchProvider]):
        if not providers:
            raise ValueError("At least one search provider is required")
        self.providers = providers

    # 功能：依次尝试提供方，返回首个非空结果；全部失败时报告汇总错误，不规避反爬。
    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        failures: list[str] = []
        from autodidact.provider_runtime import classify_failure
        from autodidact.runtime import BudgetExceeded

        for provider in self.providers:
            try:
                hits = await provider.search(query, limit)
                if hits:
                    return hits
                failures.append(f"{provider.provider_name}: no results")
            except BudgetExceeded:
                raise
            # Provider/network/parser failures are isolated by design.
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{provider.provider_name}: {classify_failure(exc).category}")
        raise SearchProviderError("All search providers failed: " + "; ".join(failures))


# 功能：按 auto/brave/duckduckgo 配置构造提供方，配置 Brave 时附加 HTML 降级。
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
