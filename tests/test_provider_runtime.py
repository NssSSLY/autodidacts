# 文件职责：用内存状态、模拟时间和无网络提供方验证F06退避/熔断/探针/计费/取消/状态读取，不访问日常库。
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from autodidact.brain.observed import ObservedLLM, valid_usage
from autodidact.config import ProviderPolicy
from autodidact.embedding_runtime import RecordedEmbedding
from autodidact.embeddings import DisabledEmbeddingProvider, EmbeddingUnavailable
from autodidact.provider_runtime import (
    CircuitOpen,
    ProviderFailure,
    ProviderIdentity,
    ProviderRuntime,
    classify_failure,
    provider_health,
    reject_challenge,
    retry_after_seconds,
)
from autodidact.runtime import BudgetedSearch, BudgetExceeded
from autodidact.tools.reader import WebReader
from autodidact.tools.safe_fetch import SourceRejected
from autodidact.tools.search import FallbackSearchProvider, SearchHit
from autodidact.web_models.service import WebModelService

IDENTITY = ProviderIdentity("llm", "example", "https://example.com/v1", "model", "secret")


class Clock:
    # 功能：保存可控时间和等待记录，不实际睡眠或调用网络。
    def __init__(self):
        self.now = 1800000000.0
        self.delays = []

    # 功能：提供运营时间戳。
    def time(self):
        return self.now

    # 功能：累计策略等待并推进模拟时钟。
    async def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay


# 功能：构造共用内存健康Store/时钟的运行器，允许模拟进程换实例而不重新置状态。
def runtime(clock=None, store=None, **policy):
    clock = clock or Clock()
    return ProviderRuntime(
        policy=ProviderPolicy(**policy), store=store, clock=clock.time, sleep=clock.sleep
    ), clock


# 功能：构造包含敏感错误正文的HTTP失败，验证分类/状态不保存外部异常文字。
def http_error(status, retry_after=None):
    request = httpx.Request("GET", "https://example.com/?token=secret")
    response = httpx.Response(
        status, request=request, headers={"Retry-After": retry_after} if retry_after else {}
    )
    return httpx.HTTPStatusError(
        "credential=secret unsafe body", request=request, response=response
    )


# 功能：HTTP失败按状态分类，仅指定暂时故障可重试；密钥/地址/正文不进入异常说明。
@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (401, "authentication", False),
        (403, "forbidden", False),
        (404, "not_found", False),
        (429, "rate_limit", True),
        (408, "timeout", True),
        (500, "upstream", True),
        (502, "upstream", True),
        (503, "upstream", True),
        (504, "upstream", True),
        (400, "request_rejected", False),
        (501, "upstream", False),
    ],
)
def test_failure_categories(status, category, retryable):
    failure = classify_failure(http_error(status))
    assert (failure.category, failure.retryable, failure.status_code) == (
        category,
        retryable,
        status,
    )
    assert "secret" not in str(failure)


# 功能：Retry-After日期/秒数有限且非法/无时区未知，普通正文讨论验证码不会触发封锁。
def test_retry_after_and_challenge_boundaries():
    assert retry_after_seconds("15", 0) == 15
    assert retry_after_seconds("Thu, 01 Jan 1970 00:01:00 GMT", 0) == 60
    assert retry_after_seconds("Thu, 01 Jan 1970 00:01:00", 0) is None
    assert retry_after_seconds("9" * 99, 0) == 604800
    assert retry_after_seconds("invalid", 0) is None
    reject_challenge("<title>验证码研究方法</title><p>verify you are human</p>")
    with pytest.raises(ProviderFailure, match="access_challenge"):
        reject_challenge("<title>Just a moment...</title><form id='challenge-form'>cf-chl-</form>")


# 功能：临时失败有限退避后成功，尝试独立且统一调用ID，等待在策略界限内。
async def test_retry_then_success():
    runner, clock = runtime()
    calls = []

    # 功能：模拟首个请求失败、第二个请求成功。
    async def invoke(context):
        calls.append(context)
        if len(calls) == 1:
            raise httpx.ConnectError("secret")
        return "ok"

    assert await runner.run(IDENTITY, invoke) == "ok"
    assert [c["provider_attempt"] for c in calls] == [1, 2]
    assert calls[0]["provider_call_id"] == calls[1]["provider_call_id"]
    assert 0.25 <= clock.delays[0] <= 0.5
    state = runner.store.rows[IDENTITY.key()]
    assert (state["attempts"], state["successes"], state["failures"], state["state"]) == (
        2,
        1,
        1,
        "closed",
    )
    assert "secret" not in str(state) and "secret" not in repr(IDENTITY)


# 功能：连续失败熔断后禁止实际请求，换运行器实例仍保留状态，冷却后仅一个并发探针恢复。
async def test_circuit_single_probe_and_restart():
    runner, clock = runtime()
    calls = 0

    # 功能：制造连续暂时故障。
    async def fail(context):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline")

    with pytest.raises(ProviderFailure):
        await runner.run(IDENTITY, fail)
    assert calls == 3
    other, _ = runtime(clock, runner.store)
    with pytest.raises(CircuitOpen):
        await other.run(IDENTITY, fail)
    assert calls == 3
    clock.now += 61
    entered, release = asyncio.Event(), asyncio.Event()

    # 功能：阻塞唯一探针以同时发起另一个调用。
    async def probe(context):
        entered.set()
        await release.wait()
        return "recovered"

    task = asyncio.create_task(runner.run(IDENTITY, probe))
    await entered.wait()
    with pytest.raises(CircuitOpen):
        await other.run(IDENTITY, probe)
    release.set()
    assert await task == "recovered"
    assert runner.store.rows[IDENTITY.key()]["state"] == "closed"


# 功能：Retry-After超过自动等待上限时只调用一次，持久冷却阻止提前重试。
async def test_long_retry_after_stops_early():
    runner, clock = runtime()
    calls = 0

    # 功能：模拟429的长冷却指令作为数据。
    async def invoke(context):
        nonlocal calls
        calls += 1
        raise http_error(429, "50")

    with pytest.raises(ProviderFailure, match="rate_limit"):
        await runner.run(IDENTITY, invoke)
    assert calls == 1 and not clock.delays
    clock.now += 49
    with pytest.raises(CircuitOpen):
        await runner.run(IDENTITY, invoke)
    assert runner.store.rows[IDENTITY.key()]["open_until"] - clock.now == 1


# 功能：短Retry-After等待后才允许单探针，成功关闭限流状态。
async def test_short_retry_after_is_respected():
    runner, clock = runtime()
    calls = 0

    # 功能：一次限流后恢复。
    async def invoke(context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise http_error(429, "2")
        return "ok"

    assert await runner.run(IDENTITY, invoke) == "ok"
    assert clock.delays == [2] and calls == 2


# 功能：鉴权/验证码不重发，安全拒绝/404不误熔断整个来源入口。
@pytest.mark.parametrize(
    "error",
    [
        http_error(401),
        http_error(403),
        ProviderFailure("access_challenge"),
        SourceRejected("unsafe"),
        http_error(404),
        ProviderFailure("unsupported_content"),
    ],
)
async def test_non_retryable_failures(error):
    runner, clock = runtime()
    calls = 0

    # 功能：返回固定不可重试故障。
    async def invoke(context):
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(ProviderFailure):
        await runner.run(IDENTITY, invoke)
    assert calls == 1 and not clock.delays
    expected = (
        "open"
        if classify_failure(error).category in {"authentication", "forbidden", "access_challenge"}
        else "closed"
    )
    assert runner.store.rows[IDENTITY.key()]["state"] == expected


# 功能：取消半开请求释放探针为冷却，取消和预算不自动重试。
async def test_cancelled_probe_releases_and_budget_propagates():
    runner, clock = runtime(circuit_failure_threshold=1)

    # 功能：一次失败打开熔断。
    async def fail(context):
        raise http_error(403)

    with pytest.raises(ProviderFailure):
        await runner.run(IDENTITY, fail)
    clock.now += 61
    entered = asyncio.Event()

    # 功能：持续等待以模拟外部取消。
    async def pending(context):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(runner.run(IDENTITY, pending))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.store.rows[IDENTITY.key()]["state"] == "open"
    fresh, _ = runtime()

    # 功能：预算耗尽必须上抛。
    async def exhausted(context):
        raise BudgetExceeded("daily limit")

    with pytest.raises(BudgetExceeded):
        await fresh.run(IDENTITY, exhausted)
    assert fresh.store.rows[IDENTITY.key()]["attempts"] == 0


# 功能：故障代号防止早期成功响应关闭后来开启的熔断。
async def test_late_success_cannot_close_new_circuit():
    runner, _ = runtime(circuit_failure_threshold=1)
    old = await runner.acquire(IDENTITY)
    failure_token = await runner.acquire(IDENTITY)
    await runner.finish(IDENTITY, failure_token, classify_failure(http_error(503)), 0)
    await runner.finish(IDENTITY, old, None, 0)
    assert runner.store.rows[IDENTITY.key()]["state"] == "open"


class Budget:
    # 功能：保存每次预算预留/结算，允许模拟耗尽。
    def __init__(self, limit=100):
        self.events = []
        self.finished = []
        self.limit = limit

    # 功能：模拟预算准入，失败不发请求。
    async def reserve(self, charges, details):
        if len(self.events) >= self.limit:
            raise BudgetExceeded("budget")
        self.events.append((charges, details))
        return len(self.events)

    # 功能：记录结算分类及真实/未知消耗。
    async def finish(self, batch, **details):
        self.finished.append((batch, details))


class Search:
    # 功能：配置无网络搜索提供方，含可选HTTP故障。
    def __init__(self, name, error=None):
        self.provider_name = name
        self.error = error
        self.calls = 0

    # 功能：累计实际请求并返回来源线索或故障。
    async def search(self, query, limit=5):
        self.calls += 1
        if self.error:
            raise self.error
        return [SearchHit("source", "https://example.com/paper")]


# 功能：首个搜索封锁后切次级，每个提供方分别计账；再次调用跳过熔断叶而不重复扣费。
async def test_fallback_budget_per_provider_and_circuit():
    primary, fallback = Search("primary", http_error(403)), Search("secondary")
    budget = Budget()
    search = BudgetedSearch(FallbackSearchProvider([primary, fallback]), budget)
    assert await search.search("query")
    assert len(budget.events) == 2 and primary.calls == 1 and fallback.calls == 1
    assert await search.search("query")
    assert len(budget.events) == 3 and primary.calls == 1 and fallback.calls == 2
    limited = BudgetedSearch(
        FallbackSearchProvider([Search("primary", http_error(403)), Search("secondary")]),
        Budget(limit=1),
    )
    with pytest.raises(BudgetExceeded):
        await limited.search("query")
    assert limited.inner.providers[1].inner.calls == 0


class Sink:
    # 功能：保存观察对象并模拟独立会话/事务，无数据库写入。
    def __init__(self):
        self.rows = []

    # 功能：进入替身事务。
    async def __aenter__(self):
        return self

    # 功能：退出替身事务。
    async def __aexit__(self, *args):
        return None

    # 功能：提供事务上下文。
    def begin(self):
        return self

    # 功能：保留待写观察，供逐次审计断言。
    def add(self, row):
        self.rows.append(row)


class Model:
    provider_name = "api"
    model_name = "model"
    base_url = "https://example.com/v1"
    api_key = "secret"

    # 功能：模拟第一次连接失败，第二次模型作答成功。
    async def text(self, system, user, temperature=None):
        if not getattr(self, "called", False):
            self.called = True
            raise httpx.ConnectError("secret")
        return "answer"


# 功能：模型重试每次单独计调用/Token/费用预留并保存等级0失败/成功观察。
async def test_model_attempt_budget_and_observations():
    model = ObservedLLM(Model(), None)
    model.budget = Budget()
    sink = Sink()
    model.sessions = lambda: sink
    model.runtime, _ = runtime()
    assert await model.text("system", "user") == "answer"
    assert len(model.budget.events) == 2 and len(sink.rows) == 2
    assert [r.metadata_json["failure_category"] for r in sink.rows] == ["connection", None]
    assert all(r.metadata_json["evidence_level"] == 0 for r in sink.rows)


# 功能：来源连接失败后重读仍走safe_fetch，逐次预算；验证码/不支持内容均不进入资料。
async def test_reader_retries_and_rejects_challenge(monkeypatch):
    calls = 0

    # 功能：替代网络读取，先失败再返回普通文本。
    async def fetch(url):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("offline")
        return url, {"content-type": "text/plain"}, b"reference text for study", "utf-8"

    monkeypatch.setattr("autodidact.tools.safe_fetch.fetch_public", fetch)
    reader = WebReader()
    reader.budget = Budget()
    reader.runtime, _ = runtime()
    assert (await reader.read("https://example.com/paper")).text
    assert calls == 2 and len(reader.budget.events) == 2

    # 功能：模拟200状态的明确验证页。
    async def challenge(url):
        return url, {"content-type": "text/html"}, b"<title>CAPTCHA</title>", "utf-8"

    monkeypatch.setattr("autodidact.tools.safe_fetch.fetch_public", challenge)
    with pytest.raises(ProviderFailure, match="access_challenge"):
        await reader.read("https://example.com/challenge")


# 功能：网页模型失败记录预算/观察但不自动重发，避免重复发送问题。
async def test_web_submission_is_not_retried():
    service = WebModelService(None)
    service.budget = Budget()
    service.runtime, _ = runtime()
    sink = Sink()
    service.sessions = lambda: sink
    calls = 0

    # 功能：模拟点击后超时。
    async def ask(prompt):
        nonlocal calls
        calls += 1
        raise TimeoutError("may already submitted")

    adapter = SimpleNamespace(
        config=SimpleNamespace(url="https://example.com", model_dump_json=lambda: "{}"), ask=ask
    )
    service.adapter = lambda provider: adapter
    with pytest.raises(ProviderFailure, match="timeout"):
        await service.ask("web", "question")
    assert calls == 1 and len(service.budget.events) == 1 and len(sink.rows) == 1


# 功能：状态查询是有界只读，不运行探针或改payload，冷却已过只报告可探测。
async def test_health_read_only_query_and_unknown():
    row = SimpleNamespace(
        id="id",
        report_key="key",
        payload={"state": "open", "open_until": 1, "protocol": "provider_health_v1"},
    )
    statements = []

    # 功能：保存查询SQL形状并返回已存快照。
    async def scalars(statement):
        statements.append(str(statement))
        return SimpleNamespace(all=lambda: [row])

    result = await provider_health(SimpleNamespace(scalars=scalars), 1)
    assert result["providers"][0]["probe_available"]
    assert row.payload == {"state": "open", "open_until": 1, "protocol": "provider_health_v1"}
    assert "LIMIT" in statements[0]
    with pytest.raises(ValueError):
        await provider_health(SimpleNamespace(scalars=scalars), 0)


# 功能：坏usage不会导致预算结算异常/低估或让原失败消失。
def test_invalid_usage_keeps_unknown_budget():
    assert (
        valid_usage({"total_tokens": "100", "prompt_tokens": float("nan"), "completion_tokens": -2})
        == {}
    )
    assert (
        valid_usage({"total_tokens": 30, "prompt_tokens": 10, "completion_tokens": 20})[
            "total_tokens"
        ]
        == 30
    )
    assert valid_usage("not a mapping") == {}
    assert valid_usage({"total_tokens": 10**400, "prompt_tokens": float("inf")}) == {}


# 功能：嵌入关闭不计账；网络失败重试按次记录，最终保持上层关键词降级可用的异常分类。
async def test_embedding_attempts_and_disabled_provider():
    disabled = RecordedEmbedding(DisabledEmbeddingProvider(), None)
    disabled.budget = Budget()
    with pytest.raises(EmbeddingUnavailable):
        await disabled.embed("input")
    assert not disabled.budget.events
    calls = 0

    # 功能：模拟嵌入连接失败后成功。
    async def embed(text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("secret")
        return [0.1] * 1536

    inner = SimpleNamespace(
        dimension=1536, model="embedding", base_url="https://example.com", embed=embed
    )
    recorded = RecordedEmbedding(inner, None)
    recorded.budget = Budget()
    recorded.runtime, _ = runtime()
    sink = Sink()
    recorded.sessions = lambda: sink
    assert len(await recorded.embed("input")) == 1536
    assert len(recorded.budget.events) == 2 and len(sink.rows) == 2
    assert sink.rows[0].metadata_json["failure_category"] == "connection"


# 功能：实际异步限时防止永不返回的提供方占住运行器，最多一次尝试并记录timeout。
async def test_attempt_timeout_is_bounded():
    runner, _ = runtime(max_attempts=1, attempt_timeout_seconds=1)

    # 功能：模拟永不响应的提供方。
    async def pending(context):
        await asyncio.Event().wait()

    with pytest.raises(ProviderFailure, match="timeout"):
        await runner.run(IDENTITY, pending)
    assert runner.store.rows[IDENTITY.key()]["last_error"]["category"] == "timeout"
