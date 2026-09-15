from autodidact.tools.search import SearchHit, deduplicate_search_hits


def test_search_results_are_normalized_deduplicated_and_limited_to_web_urls():
    hits = [
        SearchHit("A", "https://example.com/doc/?utm_source=x#part"),
        SearchHit("A mirror", "https://EXAMPLE.com/doc"),
        SearchHit("Unsafe", "javascript:alert(1)"),
        SearchHit("B", "https://other.example/path"),
    ]

    assert deduplicate_search_hits(hits, 3) == [
        SearchHit("A", "https://example.com/doc"),
        SearchHit("B", "https://other.example/path"),
    ]
