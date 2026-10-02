# 文件职责：检查搜索 URL 安全过滤、规范化、去重和返回数量。
from autodidact.tools.search import SearchHit, deduplicate_search_hits


# 功能：验证非网页地址被过滤、重复 URL 合并且受 limit 限制。
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
