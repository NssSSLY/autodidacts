# 文件职责：用无写入会话和回环HTTP替身验证认知审计、分页、未知边界、访问保护与预算查询。
import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from autodidact import models, workbench
from autodidact.audit import AuditNotFound, AuditQuery, CognitiveAudit, bounded_display
from autodidact.repository import Repository


class Rows:
    # 功能：提供标量/行结果接口，不模拟数据库的过滤和事务语义。
    def __init__(self, rows):
        self.rows = rows

    # 功能：返回测试预置的查询行。
    def all(self):
        return self.rows

    # 功能：兼容仓库的execute结果标量接口。
    def scalars(self):
        return self.rows


class ReadSession:
    # 功能：预置实体及查询结果并捕获SQL；没有add/commit/flush，误写将直接失败。
    def __init__(self, records=(), results=()):
        self.records = {(type(r), r.id): r for r in records}
        self.results = list(results)
        self.queries = []

    # 功能：作为独立会话上下文进入，不开始真实数据库事务。
    async def __aenter__(self):
        return self

    # 功能：退出替身上下文，不提交数据。
    async def __aexit__(self, *args):
        return False

    # 功能：只读返回指定类型/ID的预置实体。
    async def get(self, model, entity_id):
        return self.records.get((model, entity_id))

    # 功能：记录SELECT或事务限制指令；拒绝测试意料之外的SQL。
    async def execute(self, statement):
        self.queries.append(statement)
        if not getattr(statement, "is_select", False):
            assert str(statement).startswith(("SET TRANSACTION", "SET LOCAL"))
            return Rows([])
        assert self.results, "没有预置此查询结果"
        return Rows(self.results.pop(0))

    # 功能：返回预置标量结果，分页SQL本身另行断言而非用替身声称真实执行。
    async def scalars(self, statement):
        return await self.execute(statement)


# 功能：创建带确定时间/ID的测试实体，默认不涉及真实数据库。
def record(model, **kwargs):
    if model is models.Source:
        kwargs.setdefault("fetched_at", datetime.now(UTC))
    elif model is not models.SourceLink:
        kwargs.setdefault("created_at", datetime.now(UTC))
    return model(id=uuid4(), **kwargs)


# 功能：六类列表有稳定有界SQL，信念撤回/争议决议不被默认隐藏；Source使用fetched_at。
@pytest.mark.parametrize(
    "kind", ["sources", "claims", "beliefs", "disputes", "reports", "operations"]
)
async def test_lists_include_all_states_and_are_bounded(kind):
    types = {
        "sources": models.Source,
        "claims": models.Claim,
        "beliefs": models.Belief,
        "disputes": models.Dispute,
        "reports": models.ResearchReport,
        "operations": models.OperationEvent,
    }
    rows = [
        record(types[kind], status="retracted" if kind == "beliefs" else "resolved")
        if kind in {"beliefs", "disputes"}
        else record(types[kind])
        for _ in range(2)
    ]
    session = ReadSession(results=[rows, []] if kind == "operations" else [rows])
    result = await CognitiveAudit(Repository(session)).listing(kind, AuditQuery(limit=1, offset=5))
    view = result["view"]
    assert len(view["items"]) == 1
    assert view["pagination"]["next_offset"] == 6
    statement = session.queries[0]
    assert statement.whereclause is None
    assert statement._limit_clause.value == 2
    assert statement._offset_clause.value == 5
    assert (
        "fetched_at DESC" in str(statement)
        if kind == "sources"
        else "created_at DESC" in str(statement)
    )
    if kind in {"beliefs", "disputes"}:
        assert view["items"][0]["status"] in {"retracted", "resolved"}


# 功能：关键词使用绑定参数和转义字面量，精确筛选字段不允许调用者拼SQL。
async def test_literal_search_and_pagination_cap():
    session = ReadSession(results=[[record(models.Belief), record(models.Belief)]])
    result = await CognitiveAudit(Repository(session)).listing(
        "beliefs", AuditQuery(q="%_'; DROP TABLE beliefs", state="retracted", limit=1, offset=10000)
    )
    compiled = session.queries[0].compile(dialect=postgresql.dialect())
    assert "DROP TABLE" not in str(compiled)
    assert any("/%/_" in str(v) for v in compiled.params.values())
    assert "retracted" in compiled.params.values()
    assert result["view"]["pagination"]["next_offset"] is None
    assert result["view"]["pagination"]["truncated"]


# 功能：主张详情保留范围未知、引文状态与父子关联，不提升候选或触发模型。
async def test_claim_evidence_and_parent_reference():
    source = record(models.Source, url="https://example.test/s", metadata_json={})
    parent_id = uuid4()
    claim = record(
        models.Claim,
        statement="候选",
        scope={},
        structure={"parent_claim_ids": [str(parent_id), "not-uuid"]},
    )
    quote = record(
        models.ClaimEvidence,
        claim_id=claim.id,
        source_id=source.id,
        excerpt="<script>evil()</script>",
        status="unknown",
        assessment={},
    )
    session = ReadSession([source, claim], [[quote]])
    result = await CognitiveAudit(Repository(session)).detail(
        "claims", claim.id, AuditQuery(offset=2)
    )
    view = result["view"]
    assert view["data"]["scope"] == {}
    assert view["items"][0]["status"] == "unknown"
    assert view["items"][0]["excerpt"].startswith("<script>")
    assert {r["id"] for r in view["references"]} == {str(source.id), str(parent_id)}
    assert session.queries[0]._offset_clause.value == 2
    assert "claim_evidence.id" in str(session.queries[0])


# 功能：信念详情分别读取负面证据、旧新历史和复核报告，评分/状态不被改写。
@pytest.mark.parametrize("part", ["evidence", "history", "disputes", "reviews"])
async def test_belief_parts_preserve_stance_and_history(part):
    belief = record(
        models.Belief, statement="旧结论", status="retracted", confidence=0.9, metadata_json={}
    )
    evidence = record(
        models.Evidence, belief_id=belief.id, stance="attack", excerpt="反例", assessment={}
    )
    history = record(
        models.BeliefHistory,
        action="disputed",
        previous_state={"status": "verified"},
        new_state={"status": "disputed"},
        reason="冲突",
    )
    disputed = record(models.Dispute, status="resolved", resolution_notes="保留旧结论")
    report = record(
        models.ResearchReport,
        kind="belief_review",
        payload={"belief_id": str(belief.id)},
        report_key="test",
    )
    others = {"history": history, "disputes": disputed, "reviews": report}
    session = ReadSession([belief], [[evidence]] + ([[others[part]]] if part != "evidence" else []))
    result = await CognitiveAudit(Repository(session)).detail(
        "beliefs", belief.id, AuditQuery(part=part)
    )
    view = result["view"]
    assert view["data"]["status"] == "retracted"
    assert view["data"]["latest_review_attempt"] == {}
    assert belief.confidence == 0.9
    if part == "evidence":
        assert view["items"][0]["stance"] == "attack"
    elif part == "history":
        assert view["items"][0]["previous_state"] == {"status": "verified"}
    elif part == "reviews":
        assert "payload" in str(session.queries[-1])
        assert "belief_review" in session.queries[-1].compile().params.values()


# 功能：来源原文有界分段，血缘普通引用不变同源；缺旧质量信息仍不伪造认证。
async def test_source_text_and_lineage():
    source = record(
        models.Source,
        url="https://example.test/a",
        extracted_text="a" * 12000 + "tail",
        metadata_json={},
        evidence_level=0,
        source_type="web",
        credibility_score=0.3,
    )
    linked = record(models.Source, url="https://other.test/b", title="B")
    edge = record(
        models.SourceLink,
        source_id=source.id,
        from_url=source.url,
        to_url=linked.url,
        relation="cites",
        details={},
    )
    session = ReadSession([source], [[edge], [source, linked]])
    audit = CognitiveAudit(Repository(session))
    text = (await audit.detail("sources", source.id, AuditQuery(part="text", text_offset=12000)))[
        "view"
    ]
    assert text["data"]["original_text"] == "tail"
    assert text["pagination"]["next_offset"] is None
    assert text["data"]["bibliography"] == {}
    view = (await audit.detail("sources", source.id, AuditQuery()))["view"]
    assert view["items"][0]["dependency"] is False
    assert view["references"][0]["id"] == str(linked.id)
    assert source.metadata_json == {}


# 功能：四种既有决议仅展示保存的审计和关联，不执行Resolver或覆盖旧信念。
@pytest.mark.parametrize("outcome", ["keep_old", "adopt_new", "conditional", "unresolved"])
async def test_four_dispute_outcomes_are_observations(outcome):
    belief_id, claim_id = uuid4(), uuid4()
    dispute = record(
        models.Dispute,
        belief_id=belief_id,
        incoming_claim_id=claim_id,
        status="resolved",
        previous_belief_state={"confidence": 0.9},
        resolution_metadata={"outcome": outcome},
    )
    session = ReadSession([dispute])
    view = (await CognitiveAudit(Repository(session)).detail("disputes", dispute.id, AuditQuery()))[
        "view"
    ]
    assert view["data"]["resolution_metadata"]["outcome"] == outcome
    assert {r["id"] for r in view["references"]} == {str(belief_id), str(claim_id)}
    assert session.queries == []


# 功能：预算汇总使用coalesce而不是将真实0当未知，没有创建预留或提供方调用。
async def test_budget_uses_actual_or_reserved():
    session = ReadSession(results=[[("tokens", 0), ("estimated_usd", 2.5)]])
    result = await CognitiveAudit(Repository(session)).budget()
    assert "coalesce" in str(session.queries[0])
    assert {r["resource"]: r["used_or_reserved"] for r in result["resources"]}["tokens"] == 0
    assert "不是提供方账单" in result["note"]


# 功能：超大/深层展示被截断但分页与可信UUID引用保留，原始JSON不变。
def test_display_budget_preserves_navigation():
    body = {"large": ["x" * 100000] * 100}
    page = {"next_offset": 20, "truncated": True}
    entity_id = str(uuid4())
    result = bounded_display(
        {
            "kind": "reports",
            "data": body,
            "items": [],
            "pagination": page,
            "references": [{"kind": "beliefs", "id": entity_id, "label": "关联"}],
        }
    )
    assert result["display_truncated"]
    assert result["view"]["pagination"] == page
    assert result["view"]["references"][0]["id"] == entity_id
    assert len(json.dumps(result)) < 150000
    assert len(body["large"]) == 100


# 功能：边界/未知参数严格拒绝，不靠客户端约束防止全量遍历。
@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 51},
        {"offset": -1},
        {"offset": 10001},
        {"text_offset": 2000001},
        {"unknown": "x"},
    ],
)
def test_query_validation(params):
    with pytest.raises(ValidationError):
        AuditQuery(**params)


# 功能：路由使用独立只读事务与超时，分页query保留且不调用学习器。
async def test_dispatch_read_only_transaction(monkeypatch):
    session = ReadSession(results=[[]])
    monkeypatch.setattr(workbench, "SessionLocal", lambda: session)
    app = workbench.Workbench(8765)
    result = await app.dispatch("/api/audit/beliefs?limit=10&offset=30", "GET", {})
    assert result["view"]["items"] == []
    assert str(session.queries[0]) == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert "5000ms" in str(session.queries[1])
    assert session.queries[2]._offset_clause.value == 30
    assert app.task is None


# 功能：审计写方法、重复参数与非法路径不进入数据库。
@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/audit/beliefs", "POST"),
        ("/api/audit/beliefs?limit=1&limit=2", "GET"),
        ("/api/audit/beliefs?unknown=x", "GET"),
        ("/api/audit/beliefs/a/b", "GET"),
        ("/api/audit/unknown", "GET"),
        ("/api/audit/beliefs/not-uuid", "GET"),
    ],
)
async def test_bad_audit_requests_do_not_open_session(monkeypatch, path, method):
    # 功能：证明非法请求在取得数据库会话之前被拒绝。
    def forbidden():
        raise AssertionError("不应打开数据库")

    monkeypatch.setattr(workbench, "SessionLocal", forbidden)
    with pytest.raises(ValueError):
        await workbench.Workbench(8765).dispatch(path, method, {})


# 功能：记录不存在返回专门异常；未知类型和不适用分页参数拒绝而非静默忽略。
async def test_missing_and_irrelevant_parameters():
    session = ReadSession()
    audit = CognitiveAudit(Repository(session))
    with pytest.raises(AuditNotFound):
        await audit.detail("beliefs", uuid4(), AuditQuery())
    with pytest.raises(ValueError):
        await audit.listing("unknown", AuditQuery())
    report = record(models.ResearchReport)
    session.records[(type(report), report.id)] = report
    with pytest.raises(ValueError):
        await audit.detail("reports", report.id, AuditQuery(offset=1))


# 功能：只在短生命周期回环服务发送HTTP请求，不启动学习/数据库或访问公网。
async def http_request(app, headers, target="/api/audit/beliefs", method="GET"):
    async with await asyncio.start_server(app.connection, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        app.port = port
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        lines = [f"{method} {target} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
        lines.extend(f"{k}: {v.replace('PORT', str(port))}" for k, v in headers.items())
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
    return response.decode()


# 功能：真实回环HTTP验证Token/Origin/Host、防缓存、安全头与404，不访问外部服务。
async def test_http_protection_and_safe_errors(monkeypatch):
    app = workbench.Workbench(8765)
    seen = []

    # 功能：替代后端读取以记录完整query；特定路径模拟缺失和不暴露密钥的失败。
    async def dispatch(path, method, body):
        seen.append(path)
        if path.endswith("missing"):
            raise AuditNotFound("记录不存在")
        if path.endswith("failure"):
            raise RuntimeError("secret database credentials")
        return {"ok": True}

    monkeypatch.setattr(app, "dispatch", dispatch)
    good = {"X-Autodidact-Token": app.token}
    response = await http_request(app, good, "/api/audit/beliefs?limit=10")
    assert "200 OK" in response and seen == ["/api/audit/beliefs?limit=10"]
    assert "no-store" in response and "nosniff" in response and "frame-ancestors 'none'" in response
    for bad in [{}, {**good, "Origin": "https://evil.test"}, {**good, "Host": "evil.test"}]:
        assert "400 Bad Request" in await http_request(app, bad)
    assert len(seen) == 1
    assert "200 OK" in await http_request(app, {**good, "Origin": "http://127.0.0.1:PORT"})
    assert "404 Not Found" in await http_request(app, good, "/api/audit/missing")
    response = await http_request(app, good, "/api/audit/failure")
    assert "RuntimeError" in response and "credentials" not in response
