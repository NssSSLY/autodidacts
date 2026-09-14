from __future__ import annotations

import httpx
import pytest

from autodidact.tools.search import (
    BraveSearchProvider,
    FallbackSearchProvider,
    SearchHit,
    SearchProvider,
    SearchProviderError,
)


@pytest.mark.asyncio
async def test_brave_search_maps_structured_results_and_authenticates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "secret"
        assert request.url.params["q"] == "visual inertial odometry"
        assert request.url.params["count"] == "2"
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {
                            "title": "Primary source",
                            "url": "https://example.org/paper",
                            "description": "A source excerpt",
                        },
                        {
                            "title": "Official docs",
                            "url": "https://example.org/docs",
                            "description": "Documentation excerpt",
                        },
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = BraveSearchProvider("secret", client=client)
        hits = await provider.search("visual inertial odometry", limit=2)

    assert hits == [
        SearchHit("Primary source", "https://example.org/paper", "A source excerpt"),
        SearchHit("Official docs", "https://example.org/docs", "Documentation excerpt"),
    ]


class StubSearchProvider(SearchProvider):
    def __init__(
        self,
        name: str,
        *,
        hits: list[SearchHit] | None = None,
        error: Exception | None = None,
    ):
        self.provider_name = name
        self.hits = hits or []
        self.error = error
        self.calls = 0

    async def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        self.calls += 1
        if self.error:
            raise self.error
        return self.hits[:limit]


@pytest.mark.asyncio
async def test_fallback_search_uses_next_provider_after_failure():
    primary = StubSearchProvider("primary", error=httpx.ConnectError("offline"))
    fallback_hit = SearchHit("Fallback", "https://example.org/fallback")
    fallback = StubSearchProvider("fallback", hits=[fallback_hit])

    provider = FallbackSearchProvider([primary, fallback])

    assert await provider.search("query") == [fallback_hit]
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_fallback_search_reports_all_provider_failures():
    provider = FallbackSearchProvider(
        [
            StubSearchProvider("one", error=RuntimeError("first")),
            StubSearchProvider("two", error=RuntimeError("second")),
        ]
    )

    with pytest.raises(SearchProviderError, match="one: first; two: second"):
        await provider.search("query")
