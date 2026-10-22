# 文件职责：用内存关系查询与调用替身验证接纳读取门控及混合检索/工作台旁路，不访问外部服务。
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from autodidact import embedding_runtime, models, workbench
from autodidact.learning_state import LearningState
from autodidact.retrieval import HybridRetriever


class QuerySession:
    # 功能：捕获真实SQL构造并返回空行，不模拟PostgreSQL全文检索或向量执行。
    def __init__(self, active=None):
        self.queries = []
        self.active = active

    # 功能：提供语义通道所需的保存点上下文，不打开数据库连接。
    @asynccontextmanager
    async def begin_nested(self):
        yield

    # 功能：返回预置活动索引指针，以覆盖新旧向量分支。
    async def scalar(self, query):
        return self.active

    # 功能：记录实际查询并返回空标量，触发最近记录回退。
    async def scalars(self, query):
        self.queries.append(query)
        return SimpleNamespace(all=list)


# 功能：建立仅包含门控所需列的内存关系数据，执行真实谓词而不创建项目数据库或迁移。
@pytest.fixture
def belief_rows():
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE beliefs (id TEXT, status TEXT, lifecycle_state TEXT);"
        "CREATE TABLE evidence (id TEXT, belief_id TEXT, stance TEXT, "
        "evidence_level INTEGER, source_id TEXT);"
        "CREATE TABLE disputes (id TEXT, belief_id TEXT, status TEXT);"
    )
    rows = {}
    cases = [
        ("supported", "supported", "active", [("support", 1, "source")], []),
        ("verified", "verified", "active", [("support", 2, "source")], []),
        ("resolved", "verified", "active", [("support", 1, "source")], ["resolved"]),
        ("missing", "verified", "active", [], []),
        ("zero", "verified", "active", [("support", 0, "source")], []),
        ("no_source", "verified", "active", [("support", 2, None)], []),
        ("attack", "verified", "active", [("attack", 2, "source")], []),
        ("context", "verified", "active", [("context", 2, "source")], []),
        ("split", "verified", "active", [("support", 0, "source"), ("support", 2, None)], []),
        ("archived", "verified", "archived", [("support", 2, "source")], []),
        *[
            (state, state, "active", [("support", 2, "source")], [])
            for state in ("provisional", "weakened", "retracted")
        ],
        *[
            (state, "verified", "active", [("support", 2, "source")], [state])
            for state in ("open", "investigating", "unresolved")
        ],
    ]
    for label, status, lifecycle, evidence, disputes in cases:
        row = SimpleNamespace(
            id=uuid4(),
            status=status,
            lifecycle_state=lifecycle,
            topic="研究",
            statement=label,
            confidence=0.9,
            created_at=datetime.now(UTC),
        )
        rows[label] = row
        db.execute("INSERT INTO beliefs VALUES (?, ?, ?)", (str(row.id), status, lifecycle))
        for stance, level, source in evidence:
            db.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?)",
                (str(uuid4()), str(row.id), stance, level, source),
            )
        for dispute in disputes:
            db.execute(
                "INSERT INTO disputes VALUES (?, ?, ?)", (str(uuid4()), str(row.id), dispute)
            )
    yield db, rows
    db.close()


# 功能：在内存SQLite执行由业务代码生成的相关子查询，检验关系条件和不同信念间的隔离。
def selected_ids(db, predicate):
    query = select(models.Belief.id).where(predicate)
    sql = str(query.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    return {row[0] for row in db.execute(sql)}


# 功能：两个接纳入口均拒绝无证据、0级、无来源、非支持、跨边拼接、归档、争议及未接纳状态。
@pytest.mark.parametrize("path", ["memory", "retrieval", "retrieval_include_archived"])
async def test_accepted_reads_require_same_positive_source_edge(belief_rows, path):
    db, rows = belief_rows
    session = QuerySession()
    if path == "memory":
        await LearningState.accepted_memory(SimpleNamespace(s=session))
        predicate = session.queries[0].whereclause
    else:
        predicate = HybridRetriever(SimpleNamespace(s=session)).eligible(
            "belief", accepted_only=True, include_archived=path.endswith("include_archived")
        )
    assert selected_ids(db, predicate) == {
        str(rows[label].id) for label in ("supported", "verified", "resolved")
    }


# 功能：关键词、模糊回退、新旧向量及最近记录查询都包含同一接纳条件；嵌入失败也不能绕过。
@pytest.mark.parametrize("mode", ["disabled", "legacy", "generation", "failure"])
async def test_all_retrieval_channels_apply_acceptance_filter(mode):
    session = QuerySession()
    await LearningState.accepted_memory(SimpleNamespace(s=session))
    expected = session.queries.pop().whereclause
    if mode == "generation":
        session.active = SimpleNamespace(
            payload={
                "protocol": "retrieval_active_v1",
                "generation_id": str(uuid4()),
                "fingerprint": "offline-vector",
            }
        )

    # 功能：仅返回本地常量向量或注入失败，不调用嵌入服务。
    async def embed(query):
        if mode == "failure":
            raise RuntimeError("offline embedding failure")
        return [1.0] + [0.0] * 1535

    embedding = (
        None if mode == "disabled" else SimpleNamespace(embed=embed, fingerprint="offline-vector")
    )
    result = await HybridRetriever(SimpleNamespace(s=session), embedding).retrieve(
        "研究", accepted_only=True, include_archived=True
    )
    assert result == []
    assert len(session.queries) == {"disabled": 3, "legacy": 5, "generation": 4, "failure": 3}[mode]
    for query in session.queries:
        assert any(part.compare(expected) for part in query._where_criteria)


# 功能：普通研究仍可看到无证据或有争议信念，冲突保护显式含归档；不将接纳规则扩大到调查。
@pytest.mark.parametrize("include_archived", [False, True])
def test_research_visibility_keeps_existing_behavior(belief_rows, include_archived):
    db, rows = belief_rows
    predicate = HybridRetriever(SimpleNamespace(s=None)).eligible(
        "belief", accepted_only=False, include_archived=include_archived
    )
    assert selected_ids(db, predicate) == {
        str(row.id)
        for row in rows.values()
        if row.status != "retracted" and (include_archived or row.lifecycle_state == "active")
    }


# 功能：直接调用工作台问答路由，确认无来源/0级等旧信念不进入模型解释上下文；无需HTTP服务。
async def test_workbench_ask_only_supplies_accepted_evidence_backed_memory(
    monkeypatch, belief_rows
):
    db, rows = belief_rows
    by_id = {str(row.id): row for row in rows.values()}
    prompts = []

    class Session(QuerySession):
        # 功能：提供工作台所需会话上下文，不操作外部数据库。
        async def __aenter__(self):
            return self

        # 功能：退出只读测试会话，保持内存样本供断言使用。
        async def __aexit__(self, *args):
            return False

        # 功能：执行首个门控谓词并返回样本，全文和向量匹配不在此替身的验证范围。
        async def scalars(self, query):
            if query.column_descriptions[0]["entity"] is models.Dispute:
                return SimpleNamespace(all=list)
            assert query.column_descriptions[0]["entity"] is models.Belief
            records = [by_id[item] for item in selected_ids(db, query._where_criteria[0])]
            return SimpleNamespace(all=lambda: records)

    # 功能：只替换工作台控制器锁，不连接数据库获取锁。
    @asynccontextmanager
    async def lock(engine):
        yield

    # 功能：保存模型实际收到的记忆，不调用LLM也不返回引用以外的结论。
    async def answer(system, user, schema):
        prompts.append(json.loads(user))
        return schema(explanation="offline", belief_ids=[], missing_knowledge=[])

    monkeypatch.setattr(workbench, "SessionLocal", Session)
    monkeypatch.setattr(workbench, "controller_lock", lock)
    monkeypatch.setattr(embedding_runtime, "RecordedEmbedding", lambda *args: None)
    monkeypatch.setattr(workbench, "build_llm", lambda: None)
    monkeypatch.setattr(
        workbench, "ObservedLLM", lambda *args, **kwargs: SimpleNamespace(structured=answer)
    )
    await workbench.Workbench(8765).dispatch("/api/ask", "POST", {"question": "研究"})
    assert len(prompts) == 1
    assert {item["id"] for item in prompts[0]["beliefs"]} == {
        str(rows[label].id) for label in ("supported", "verified", "resolved")
    }
