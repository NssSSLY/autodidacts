# 文件职责：离线验证持久索引工作流合同、版本隔离SQL与本地后台API；替身不代表实库验收。
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from autodidact import models, retrieval_jobs, workbench
from autodidact.retrieval import HybridRetriever
from autodidact.retrieval_jobs import (
    ACTIVE_PROTOCOL,
    PROTOCOL,
    RetrievalJobs,
    content_hash,
    valid_vector,
)
from autodidact.runtime import BudgetExceeded

VECTOR = [1.0] + [0.0] * 1535


class MemoryStore:
    # 功能：建立独立报告与工作项替身，每次读取复制对象以模拟提交后分离的SQLAlchemy行。
    def __init__(self, count=2):
        self.jobs, self.entries, self.active = {}, {}, None
        self.rows = {
            uuid4(): models.Belief(topic="测试", statement=f"结论{i}", status="supported")
            for i in range(count)
        }
        for key, row in self.rows.items():
            row.id = key
        self.indexes = True
        self.keyword_missing = 0

    # 功能：复制读取任务，避免测试通过引用共享伪造事务行为。
    async def load(self, job_id):
        return deepcopy(self.jobs[job_id])

    # 功能：在上下文成功退出时提交可变任务快照。
    @asynccontextmanager
    async def edit(self, job_id):
        payload = await self.load(job_id)
        yield payload
        self.jobs[job_id] = payload

    # 功能：创建候选任务，不主动触发嵌入调用。
    async def create(self, fingerprint, max_attempts):
        key = uuid4()
        self.jobs[key] = {
            "protocol": PROTOCOL,
            "fingerprint": fingerprint,
            "max_attempts": max_attempts,
            "status": "queued",
        }
        return key

    # 功能：幂等补齐当前信念工作项，保持已完成项不变。
    async def populate(self, job_id):
        for key in self.rows:
            if not any(
                v.generation_id == job_id and v.entity_id == key for v in self.entries.values()
            ):
                item = SimpleNamespace(
                    id=uuid4(),
                    generation_id=job_id,
                    entity_kind="belief",
                    entity_id=key,
                    status="pending",
                    attempts=0,
                    content_hash=None,
                    embedding=None,
                    fingerprint=None,
                    error_category=None,
                )
                self.entries[item.id] = item

    # 功能：逐项复制工作项与当前实体，支持失效和删除场景。
    async def items(self, job_id):
        for item in list(self.entries.values()):
            if item.generation_id == job_id:
                yield deepcopy(item), self.rows.get(item.entity_id)

    # 功能：提交单项更新，row参数只模拟关键词同步入口。
    async def mark(self, item_id, *, row=None, **values):
        for key, value in values.items():
            setattr(self.entries[item_id], key, value)

    # 功能：取得分离的下一待处理项，避免预算退款读到已递增次数。
    async def next_item(self, job_id):
        async for item, row in self.items(job_id):
            if item.status == "pending":
                return item, row
        return None

    # 功能：数出新出现但未纳入该版本的实体。
    async def missing_entities(self, job_id):
        ids = {v.entity_id for v in self.entries.values() if v.generation_id == job_id}
        return len(set(self.rows) - ids)

    # 功能：恢复崩溃在途项，保留已使用次数及超限失败。
    async def recover(self, job_id, max_attempts):
        for item in self.entries.values():
            if item.generation_id == job_id and (
                item.status == "running" or item.error_category == "interrupted"
            ):
                item.status = "pending" if item.attempts < max_attempts else "failed"
                item.error_category = "interrupted"

    # 功能：重排未超限失败项而不清零次数。
    async def retry(self, job_id, max_attempts):
        for item in self.entries.values():
            if (
                item.generation_id == job_id
                and item.status == "failed"
                and item.attempts < max_attempts
            ):
                item.status = "pending"

    # 功能：聚合进度与尝试数，返回状态检查使用的相同字段。
    async def stats(self, job_id, limit=20):
        counts = dict.fromkeys(["pending", "running", "completed", "failed", "skipped"], 0)
        attempts = 0
        async for item, _ in self.items(job_id):
            counts[item.status] += 1
            attempts += item.attempts
        return {"counts": counts, "attempts": attempts, "total": sum(counts.values())}

    # 功能：模拟物理索引存在或缺失，不宣称验证真实HNSW。
    async def indexes_present(self):
        return self.indexes

    # 功能：模拟关键词入口缺失，检查覆盖不完整时禁止启用。
    async def keyword_gaps(self, job_id):
        return self.keyword_missing

    # 功能：模拟原子指针切换和启用后封存，不删除任何工作项。
    async def activate(self, job_id, fingerprint):
        self.active = {
            "protocol": ACTIVE_PROTOCOL,
            "generation_id": str(job_id) if job_id else None,
            "fingerprint": fingerprint,
        }
        if job_id:
            self.jobs[job_id]["sealed"] = True

    # 功能：只读取得单个或多个任务的快照及进度。
    async def status(self, job_id=None, limit=20):
        keys = [job_id] if job_id else list(self.jobs)[:limit]
        return {
            "active": deepcopy(self.active),
            "jobs": [dict(id=str(k), **await self.load(k), **await self.stats(k)) for k in keys],
        }


class Embedding:
    fingerprint = "test-fingerprint"

    # 功能：配置可控返回或异常，记录实际逻辑调用数。
    def __init__(self, error=None, callback=None):
        self.calls, self.error, self.callback = 0, error, callback

    # 功能：返回固定非零1536维向量，或执行暂停/故障场景。
    async def embed(self, text):
        self.calls += 1
        if self.callback:
            await self.callback()
        if self.error:
            raise self.error
        return VECTOR.copy()


# 功能：用不访问数据库的上下文替代控制器锁，只验证业务状态机。
@asynccontextmanager
async def unlocked(engine):
    yield


# 功能：为本文件状态机测试安装离线锁替身，不改变数据库集成测试。
@pytest.fixture(autouse=True)
def offline_lock(monkeypatch):
    monkeypatch.setattr(retrieval_jobs, "controller_lock", unlocked)


# 功能：建立候选任务及替身环境，创建阶段应零模型调用。
async def setup_job(count=2, embedding=None, max_attempts=3):
    store, provider = MemoryStore(count), embedding or Embedding()
    jobs = RetrievalJobs(None, provider, store)
    key = UUID((await jobs.create(max_attempts))["job_id"])
    assert provider.calls == 0
    return jobs, store, provider, key


# 功能：验证有限批次暂停、显式恢复与完成项复用，并保持未启用的活动版本不变。
async def test_batch_resume_complete_activate_and_rollback():
    jobs, store, embedding, key = await setup_job()
    await jobs.run(key, 1)
    assert store.jobs[key]["status"] == "paused" and embedding.calls == 1
    await jobs.run(key)
    assert embedding.calls == 1
    await jobs.control(key, "resume")
    await jobs.run(key)
    assert store.jobs[key]["status"] == "ready" and embedding.calls == 2
    assert store.active is None
    await jobs.activate(key)
    for action in ("pause", "resume", "retry"):
        with pytest.raises(ValueError, match="不可修改"):
            await jobs.control(key, action)
    with pytest.raises(ValueError, match="不可重建"):
        await jobs.run(key)
    await jobs.legacy()
    assert store.active["generation_id"] is None and len(store.entries) == 2


# 功能：失败重试不重置次数，达到上限以后即使人工重排也不再次调用。
async def test_failed_retry_retains_attempt_cap():
    jobs, store, embedding, key = await setup_job(1, Embedding(RuntimeError("secret token")), 2)
    await jobs.run(key)
    await jobs.control(key, "retry")
    await jobs.run(key)
    await jobs.control(key, "retry")
    await jobs.run(key)
    item = next(iter(store.entries.values()))
    assert embedding.calls == item.attempts == 2 and item.status == "failed"
    assert "secret" not in item.error_category


# 功能：预算拒绝不算一次逻辑请求，取消保留可能已消费的尝试并能从中断继续。
@pytest.mark.parametrize("error", [BudgetExceeded(), asyncio.CancelledError()])
async def test_budget_and_interruption_resume(error):
    jobs, store, embedding, key = await setup_job(1, Embedding(error))
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await jobs.run(key)
    else:
        await jobs.run(key)
    assert store.jobs[key]["status"] == "paused"
    assert next(iter(store.entries.values())).attempts == (
        1 if isinstance(error, asyncio.CancelledError) else 0
    )
    embedding.error = None
    await jobs.control(key, "resume")
    await jobs.run(key)
    assert store.jobs[key]["status"] == "ready" and embedding.calls == 2


# 功能：在途调用收到暂停仍保存结果，但不启动下一个工作项。
async def test_pause_during_inflight_and_fingerprint_mismatch():
    jobs, store, embedding, key = await setup_job()

    # 功能：模拟在途网络期间另一请求记录暂停。
    async def pause():
        await jobs.control(key, "pause")

    embedding.callback = pause
    await jobs.run(key)
    assert embedding.calls == 1 and (await store.stats(key))["counts"]["completed"] == 1
    await jobs.control(key, "resume")
    embedding.fingerprint = "new-model"
    with pytest.raises(ValueError, match="指纹"):
        await jobs.run(key)
    assert embedding.calls == 1


# 功能：空版本、坏向量、物理索引缺失、新实体与旧内容变化都禁止启用，不触发付费核验。
@pytest.mark.parametrize("damage", ["empty", "invalid", "index", "keyword", "new", "stale"])
async def test_integrity_blocks_activation(damage):
    jobs, store, embedding, key = await setup_job(0 if damage == "empty" else 1)
    await jobs.run(key)
    if damage == "invalid":
        next(iter(store.entries.values())).embedding = [0.0] * 1536
    elif damage == "index":
        store.indexes = False
    elif damage == "keyword":
        store.keyword_missing = 1
    elif damage == "new":
        store.rows[uuid4()] = models.Belief(topic="新", statement="新结论")
    elif damage == "stale":
        next(iter(store.rows.values())).statement = "已修改内容"
    with pytest.raises(ValueError, match="不完整"):
        await jobs.activate(key)
    assert store.active is None and embedding.calls == (0 if damage == "empty" else 1)
    if damage == "empty":
        assert store.jobs[key]["status"] == "failed"


# 功能：未启用候选内容变化可以重建，已启用版本仅核验失效而不原地改向量。
async def test_changed_content_rebuild_and_sealed_audit():
    jobs, store, embedding, key = await setup_job(1)
    await jobs.run(key)
    row = next(iter(store.rows.values()))
    row.statement = "修改后"
    await jobs.run(key)
    assert embedding.calls == 2 and next(iter(store.entries.values())).content_hash == content_hash(
        "belief", row
    )
    await jobs.activate(key)
    row.statement = "再修改"
    assert (await jobs.verify(key))["stale"] == 1
    assert embedding.calls == 2 and store.jobs[key]["sealed"]


# 功能：内部已发一次物理请求后预算拒绝后续重试，不能退还逻辑次数。
async def test_budget_after_physical_retry_preserves_logical_attempt():
    provider = Embedding(BudgetExceeded())
    provider.attempts_started = 1
    jobs, store, _, key = await setup_job(1, provider)
    await jobs.run(key)
    assert next(iter(store.entries.values())).attempts == 1


# 功能：检查数值/维数边界，避免无穷、布尔、巨大整数和零向量进入HNSW。
@pytest.mark.parametrize(
    "vector",
    [
        None,
        1,
        [],
        [0.0] * 1536,
        [True] * 1536,
        [float("nan")] * 1536,
        [float("inf")] * 1536,
        [10**400] * 1536,
        [1e-45] * 1536,
    ],
)
def test_invalid_vector_shapes(vector):
    assert not valid_vector(vector)
    assert valid_vector(VECTOR)


class QuerySession:
    # 功能：收集实际生成SQL和活动指针读取次数，不访问真实向量索引。
    def __init__(self, active):
        self.active, self.sql, self.reads = active, [], 0

    # 功能：提供保存点上下文，模拟语义通道故障隔离。
    @asynccontextmanager
    async def begin_nested(self):
        yield

    # 功能：一次召回只返回一次指针，用计数检测跨类型重复读取。
    async def scalar(self, query):
        self.reads += 1
        return self.active

    # 功能：记录每条类型化查询并返回空数据以便检查语义版本/旧路径分支。
    async def scalars(self, query):
        self.sql.append(str(query.compile(dialect=postgresql.dialect())))
        return SimpleNamespace(all=list)


# 功能：活动候选只召回匹配版本/指纹/内容摘要，缺指针兼容旧索引，坏指针只关键词降级。
@pytest.mark.parametrize("state", ["active", "legacy", "mismatch", "unknown"])
async def test_retrieval_generation_query_isolation(state):
    payload = {
        "protocol": ACTIVE_PROTOCOL,
        "generation_id": str(uuid4()),
        "fingerprint": "test-fingerprint",
    }
    if state == "mismatch":
        payload["fingerprint"] = "other"
    if state == "unknown":
        payload["protocol"] = "future"
    session = QuerySession(None if state == "legacy" else SimpleNamespace(payload=payload))
    await HybridRetriever(SimpleNamespace(s=session), Embedding()).retrieve(
        "研究", limit=5, kinds=("belief", "goal")
    )
    sql = "\n".join(session.sql)
    assert session.reads == 1
    if state == "active":
        assert (
            "retrieval_vectors.generation_id" in sql
            and "retrieval_vectors.content_hash = retrieval_entries.content_hash" in sql
        )
        assert "beliefs.embedding <=>" not in sql
    elif state == "legacy":
        assert "retrieval_entries.embedding <=>" in sql and "beliefs.embedding <=>" in sql
    else:
        assert "<=>" not in sql


# 功能：本地只读查看不启动工作者，显式运行异步返回，暂停等操作通过有限路由转发。
async def test_workbench_background_and_status(monkeypatch):
    calls, wait = [], asyncio.Event()

    # 功能：记录API动作，并阻塞run以证明网页请求不等待整个批次。
    async def action(engine, name, job_id=None, **options):
        calls.append((name, job_id, options))
        if name == "run":
            await wait.wait()
        return {"jobs": [], "status": name}

    monkeypatch.setattr(workbench, "job_action", action)
    app, key = workbench.Workbench(8765), uuid4()
    await app.dispatch("/api/retrieval-jobs?limit=5", "GET", {})
    assert app.index_task is None and calls[0][0] == "status"
    result = await app.dispatch(f"/api/retrieval-jobs/{key}/run", "POST", {"max_items": 2})
    assert result["status"] == "queued"
    await asyncio.sleep(0)
    assert (await app.dispatch(f"/api/retrieval-jobs/{key}/run", "POST", {}))["status"] == "running"
    await app.dispatch(f"/api/retrieval-jobs/{key}/pause", "POST", {})
    wait.set()
    await app.index_task
    assert [v[0] for v in calls] == ["status", "run", "pause"]


# 功能：拒绝任意动作、坏UUID、额外字段和超界批次，保留既有本地安全门控。
@pytest.mark.parametrize(
    "path,method,body",
    [
        ("/api/retrieval-jobs/bad/run", "POST", {}),
        (f"/api/retrieval-jobs/{uuid4()}/shell", "POST", {}),
        ("/api/retrieval-jobs/create", "POST", {"secret": "x"}),
        (f"/api/retrieval-jobs/{uuid4()}/run", "POST", {"max_items": 10001}),
        ("/api/retrieval-jobs?limit=1&limit=2", "GET", {}),
        ("/api/retrieval-jobs/create?x=1", "POST", {}),
    ],
)
async def test_workbench_rejects_bad_index_operations(path, method, body):
    with pytest.raises(ValueError):
        await workbench.Workbench(8765).dispatch(path, method, body)
