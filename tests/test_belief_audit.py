# 文件职责：离线验证F04冻结/盲答隔离、阶段缓存/次数/失败、后置核源和旧状态安全，不冒充真实模型/数据库验收。
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_belief_review import Session

from autodidact import belief_audit, models
from autodidact.belief_audit import (
    PROTOCOL,
    AuditManifest,
    BeliefMigrationAudit,
    BlindAnswer,
    belief_snapshot,
    check_question,
    freeze_audit,
    read_manifest,
)
from autodidact.belief_audit_commands import candidate
from autodidact.learning.ground_truth import model_identity
from autodidact.repository import Repository
from autodidact.runtime import BudgetExceeded
from autodidact.schemas import ContradictionResult


class Candidate:
    provider_name, model_name = "fake", "new-model"

    # 功能：记录每次真实替身请求和上下文，可模拟失败/取消/弃答。
    def __init__(self, error=None, abstained=False):
        self.calls, self.error, self.abstained = [], error, abstained

    # 功能：保存阶段上下文，不混入用户提示或旧答案。
    def bind(self, **context):
        self.context = context

    # 功能：提供盲答与后置关系观察，并记录收到的严格提示边界。
    async def structured(self, system, user, schema):
        self.calls.append((system, user, schema))
        if self.error:
            raise self.error
        if schema is BlindAnswer:
            return BlindAnswer(answer="新模型的独立答案", confidence=0.6, abstained=self.abstained)
        if schema is ContradictionResult:
            return ContradictionResult(relation="contradicts", score=0.99, explanation="只是观察")
        raise AssertionError(schema)


# 功能：建立有旧结论/来源/评分的内存仓库，复用Repository报告/目标/会话实现而非另造业务Store。
async def environment(provider=None):
    session = Session()
    belief = models.Belief(
        id=uuid4(),
        statement="必须保密的旧结论",
        topic="隐私主题",
        explanation="旧答案解释",
        status="verified",
        lifecycle_state="active",
        confidence=0.9,
        evidence_score=0.8,
        test_score=0.7,
        stability_score=0.6,
        metadata_json={"claim_scope": {"conditions": ["旧限定条件"]}},
    )
    source = models.Source(
        id=uuid4(),
        url="https://old.example/private",
        content_hash="old-hash",
        extracted_text="旧原文",
        source_type="web",
        evidence_level=1,
        credibility_score=0.3,
        publisher_key="old.example",
        metadata_json={},
    )
    edge = models.Evidence(
        id=uuid4(),
        belief_id=belief.id,
        source_id=source.id,
        stance="support",
        excerpt="旧支持摘录",
        evidence_level=1,
    )
    session.rows.extend([belief, source, edge])
    repo, llm = Repository(session), provider or Candidate()
    manifest = AuditManifest(
        protocol=PROTOCOL,
        label="batch-1",
        reviewer="人工审核者",
        neutral_questions_confirmed=True,
        items=[{"belief_id": belief.id, "question": "如何分析此技术的约束与误差？"}],
    )
    frozen = await freeze_audit(repo, manifest, {"model": "old"}, model_identity(llm))
    return (
        session,
        repo,
        belief,
        source,
        manifest,
        BeliefMigrationAudit(repo, llm),
        UUID(frozen["report_id"]),
    )


# 功能：核对同内容冻结幂等、只写报告、高置信与身份门槛及完整冻结摘要。
async def test_freeze_idempotent_no_model_and_snapshot_preserved():
    session, repo, belief, source, manifest, audit, key = await environment()
    again = await freeze_audit(repo, manifest, {"model": "old"}, model_identity(audit.llm))
    assert again["report_id"] == str(key) and not audit.llm.calls
    report = await audit.load(key)
    assert report.payload["items"][0]["belief"] == belief_snapshot(belief)
    assert report.payload["items"][0]["sources"][0]["url"] == source.url
    assert not [r for r in session.rows if isinstance(r, models.Goal)]
    with pytest.raises(ValueError, match="相同"):
        await freeze_audit(repo, manifest, model_identity(audit.llm), model_identity(audit.llm))


# 功能：严格拒绝低置信、非接纳和非有限置信，不能把坏旧状态标为合格样本。
@pytest.mark.parametrize(
    "status,confidence",
    [
        ("provisional", 0.9),
        ("disputed", 0.9),
        ("verified", 0.7),
        ("verified", float("nan")),
        ("verified", 1.1),
    ],
)
async def test_sample_eligibility(status, confidence):
    _, repo, belief, _, manifest, audit, _ = await environment()
    belief.status, belief.confidence = status, confidence
    with pytest.raises(ValueError, match="confidence"):
        await freeze_audit(repo, manifest, {}, model_identity(audit.llm))


# 功能：人工题面不得直接塞入旧结论/解释/引用或来源，纯问题可通过但不证明语义独立。
@pytest.mark.parametrize(
    "secret", ["结论原句", "旧解释", "https://old.example/a", "很长的引文" * 10]
)
def test_question_leakage_rejected(secret):
    snapshot = {"statement": "结论原句", "explanation": "旧解释"}
    sources = [{"url": "https://old.example/a", "excerpt": "很长的引文" * 10}]
    with pytest.raises(ValueError, match="泄漏"):
        check_question(f"问题：{secret}，对吗？", snapshot, sources)
    check_question("请独立分析该技术", snapshot, sources)


# 功能：盲答请求仅包含人工问题，不包含旧认知/来源/评分，重复运行复用已存答案。
async def test_answer_is_blind_and_replayed_before_any_verification():
    session, _, belief, source, _, audit, key = await environment()
    before = deepcopy(belief_snapshot(belief))
    assert (await audit.run(key, "verify"))["processed"] == 0 and not audit.llm.calls
    result = await audit.run(key, "answer")
    assert result["processed"] == 1 and len(audit.llm.calls) == 1
    system, user, schema = audit.llm.calls[0]
    assert set(json.loads(user)) == {"question"} and schema is BlindAnswer
    for secret in (
        belief.statement,
        belief.topic,
        belief.explanation,
        source.url,
        "旧限定条件",
        "0.9",
    ):
        assert secret not in system + user
    assert (await audit.run(key, "answer"))["processed"] == 0
    assert len(audit.llm.calls) == 1 and belief_snapshot(belief) == before
    goal = next(r for r in session.rows if isinstance(r, models.Goal))
    assert goal.status == "blocked" and (await audit.status(key))["items"][0]["review"] is None


# 功能：服务异常/弃答保存unknown，核验不以空答伪造争议，不返回异常正文。
@pytest.mark.parametrize("mode", ["failure", "abstained"])
async def test_unknown_answer_does_not_verify_or_rewrite(mode):
    provider = Candidate(
        error=TimeoutError("secret-token") if mode == "failure" else None,
        abstained=mode == "abstained",
    )
    _, _, belief, _, _, audit, key = await environment(provider)
    result = await audit.run(key, "answer")
    assert result["items"][0]["answer"]["outcome"] == "unknown"
    assert "secret-token" not in json.dumps(result)
    reviewed = await audit.run(key, "verify")
    assert reviewed["items"][0]["review"]["reason"] == "blind_answer_unavailable"
    assert len(provider.calls) == 1 and belief.status == "verified" and belief.confidence == 0.9


# 功能：预算和取消不被吞成成功；持久次数限制后续显式重试，原信念保持不变。
@pytest.mark.parametrize("error", [BudgetExceeded(), asyncio.CancelledError()])
async def test_interruption_attempt_cap(error):
    session, _, belief, _, _, audit, key = await environment(Candidate(error=error))
    for _ in range(belief_audit.agent_config().learning.max_retry):
        with pytest.raises(type(error)):
            await audit.run(key, "answer")
    with pytest.raises(ValueError, match="上限"):
        await audit.run(key, "answer")
    assert belief.status == "verified"
    assert len([r for r in session.rows if isinstance(r, models.LearningSession)]) == 1


# 功能：冻结内容篡改、换模型/端点和旧信念变化均在请求前拒绝，不复用错误身份答案。
@pytest.mark.parametrize("change", ["hash", "model", "statement", "source"])
async def test_changed_identity_or_snapshot_fails_closed(change):
    _, _, belief, source, _, audit, key = await environment()
    if change == "hash":
        (await audit.load(key)).payload["items"][0]["question"] = "篡改"
    elif change == "model":
        audit.llm.model_name = "other"
    elif change == "statement":
        belief.statement = "更新后的结论"
    else:
        source.content_hash = "changed"
    with pytest.raises(ValueError):
        await audit.run(key, "answer")
    assert not audit.llm.calls


# 功能：只读入口无LLM也能显示完整已存状态，不新增报告/会话或研究。
async def test_status_is_model_free():
    session, repo, _, _, _, _, key = await environment()
    count = len(session.rows)
    assert (await BeliefMigrationAudit(repo).status(key))["items"][0]["answer"] is None
    assert len(session.rows) == count


# 功能：后置核验在持久盲答之后，重读旧原文且完整排除旧来源；盲答异议本身不调用创建争议。
async def test_verify_reuses_gated_reviewer_not_model_opinion(monkeypatch):
    _, _repo, belief, source, _, audit, key = await environment()
    seen = []

    class Reader:
        # 功能：模拟旧源不可读，禁止把失败解释为旧结论错误。
        async def read(self, url):
            seen.append(url)
            raise TimeoutError("exception-secret-never-store")

    class Reviewer:
        # 功能：替身只检查接线，证据门控由现有F02真实逻辑回归单独验证。
        def __init__(self, repo, llm, collector):
            self.repo = repo

        # 功能：断言盲答已落库和冻结排除集完整，不凭异议创建争议。
        async def investigate(self, belief_id, attempt, *, extra_exclusions):
            assert await self.repo.get_report("belief_audit_answer", f"{key}:{belief.id}")
            assert {d.url for d in extra_exclusions} == {source.url}
            return {"outcome": "needs_follow_up", "dispute_ids": []}

    monkeypatch.setattr(belief_audit, "BeliefReviewer", Reviewer)
    audit.collector = SimpleNamespace(reader=Reader())
    await audit.run(key, "answer")
    result = await audit.run(key, "verify")
    review = result["items"][0]["review"]
    assert (
        review["outcome"] == "needs_follow_up"
        and review["relation_observation"]["relation"] == "contradicts"
    )
    assert review["original_checks"][0]["outcome"] == "unknown" and seen == [source.url]
    assert (
        "exception-secret-never-store" not in json.dumps(review)
        and belief.confidence == 0.9
        and belief.status == "verified"
    )
    await audit.run(key, "verify")
    assert seen == [source.url]


# 功能：冻结文件有界/唯一/schema严格，不把文档内容解释为控制器操作。
def test_manifest_examples_and_validation():
    manifest = read_manifest("data/benchmark/belief_audit.example.json")
    assert len(manifest.items) == 1
    payload = manifest.model_dump(mode="json")
    payload["items"] *= 2
    with pytest.raises(ValueError):
        read_manifest_from_payload(payload)
    payload["items"] = payload["items"][:1]
    payload["shell"] = "ignored instruction"
    with pytest.raises(ValueError):
        AuditManifest.model_validate(payload)


# 功能：用可控只读流测试文件边界与重复检测，避免为测试写入临时JSON。
def read_manifest_from_payload(payload):
    from io import BytesIO
    from unittest.mock import patch

    with patch("pathlib.Path.open", return_value=BytesIO(json.dumps(payload).encode())):
        return read_manifest("example.json")


# 功能：CLI更换端点时缺专用密钥在发送前拒绝，不将日常凭据泄漏给新服务。
def test_candidate_endpoint_credentials(monkeypatch):
    import autodidact.belief_audit_commands as commands
    from autodidact.config import RuntimeSettings

    settings = RuntimeSettings(
        llm_base_url="https://old.example/v1", llm_api_key="secret", migration_new_api_key=""
    )
    monkeypatch.setattr(commands, "runtime_settings", lambda: settings)
    assert candidate("new", "https://new.example/v1").model_name == "new"
    with pytest.raises(ValueError, match="MIGRATION_NEW_API_KEY"):
        candidate("new", "https://new.example/v1", running=True)


# 功能：实际串接F04与F02原文/替代句门控；有独立正等级原文才开争议，不自动采用新模型答案。
@pytest.mark.parametrize("supported", [True, False])
@pytest.mark.parametrize("readable", [True, False])
async def test_end_to_end_audit_requires_evidenced_alternative(supported, readable):
    from test_belief_review import QUOTE, STATEMENT, URL, Judge, fixture

    from autodidact.knowledge.decomposition import DecompositionProposal, DecompositionReview
    from autodidact.knowledge.support_assessment import SupportVerdict
    from autodidact.schemas import ClaimCitation, ClaimDraft, LearningResult, SourceDocument

    session, repo, old_source, belief, _ = fixture()
    belief.lifecycle_state = "active"
    belief.evidence_score = belief.test_score = belief.stability_score = 0.8
    old_source.url = "https://old.example/support"
    fresh = models.Source(
        id=uuid4(),
        url=URL,
        extracted_text=QUOTE + " Context." * 40,
        evidence_level=1,
        credibility_score=0.3,
        source_type="web",
        metadata_json={},
    )
    session.rows.append(fresh)
    alternative = "Observed drift grows over time under the stated operating conditions."

    class AuditJudge(Judge):
        # 功能：提供独立盲答、单句结构和分别核验原句/替代句的观察，不修改任何数据库。
        async def structured(self, system, user, schema):
            if schema is BlindAnswer:
                assert STATEMENT not in user
                return BlindAnswer(answer=alternative, confidence=0.7, abstained=False)
            if schema is LearningResult:
                old = old_source.url in user
                return LearningResult(
                    claims=[
                        ClaimDraft(
                            statement=STATEMENT if old else alternative,
                            topic="drift",
                            confidence=0.5,
                            citations=[
                                ClaimCitation(
                                    source_url=old_source.url if old else URL, excerpt=QUOTE
                                )
                            ],
                        )
                    ]
                )
            if schema is SupportVerdict and user.startswith(f"Claim: {alternative}"):
                return SupportVerdict(
                    relation="supports" if supported else "unclear", reason="替代核验"
                )
            if schema is DecompositionProposal:
                return DecompositionProposal(kind="atomic", reason="单句")
            if schema is DecompositionReview:
                return DecompositionReview(
                    equivalent=True,
                    all_atomic=True,
                    scope_preserved=True,
                    no_added_facts=True,
                    reason="结构复核",
                )
            if schema is ContradictionResult:
                return ContradictionResult(relation="contradicts", score=0.95, explanation="观察")
            return await super().structured(system, user, schema)

        # 功能：允许审计绑定阶段，无外部模型状态。
        def bind(self, **context):
            pass

    class Collector:
        reader = None

        # 功能：旧源不可读只记录未知；新独立来源必须排除完整冻结旧来源。
        async def read(self, url):
            if readable:
                return SourceDocument(url=url, text=old_source.extracted_text)
            raise TimeoutError()

        # 功能：用另一出版方返回真实摘录替身，并断言旧URL排除没有被窗口遗漏。
        async def fetch(self, query, *, exclude, limit):
            assert old_source.url in {d.url for d in exclude}
            return [SourceDocument(url=URL, text=fresh.extracted_text)]

    # 功能：复用预置原文，来源分类入库另有独立测试。
    async def upsert(doc, content_hash):
        return old_source if doc.url == old_source.url else fresh

    repo.upsert_source = upsert
    llm, collector = AuditJudge(), Collector()
    collector.reader = collector
    manifest = AuditManifest(
        protocol=PROTOCOL,
        label="gated",
        reviewer="人工",
        neutral_questions_confirmed=True,
        items=[{"belief_id": belief.id, "question": "What are the system drift characteristics?"}],
    )
    frozen = await freeze_audit(repo, manifest, {"model": "old"}, model_identity(llm))
    key = UUID(frozen["report_id"])
    audit = BeliefMigrationAudit(repo, llm, collector=collector)
    await audit.run(key, "answer")
    assert not [r for r in session.rows if isinstance(r, models.Dispute)]
    result = await audit.run(key, "verify")
    assert result["items"][0]["review"]["original_checks"][0]["outcome"] == (
        "read" if readable else "unknown"
    )
    disputes = [r for r in session.rows if isinstance(r, models.Dispute)]
    assert len(disputes) == int(supported)
    assert result["items"][0]["review"]["outcome"] == (
        "dispute_opened" if supported else "needs_follow_up"
    )
    assert belief.statement == STATEMENT and belief.confidence == 0.9
    assert belief.status == ("disputed" if supported else "verified")


# 功能：声明只接受显式true，拒绝1/false/字符串，不把模型或文档话语当审核授权。
@pytest.mark.parametrize("value", [1, False, "true"])
def test_manifest_requires_explicit_confirmation(value):
    payload = read_manifest("data/benchmark/belief_audit.example.json").model_dump(mode="json")
    payload["neutral_questions_confirmed"] = value
    with pytest.raises(ValueError):
        AuditManifest.model_validate(payload)


# 功能：128KiB上限在解析前拒绝，避免超大输入占用无界内存。
def test_manifest_is_bounded():
    with pytest.raises(ValueError, match="128KiB"):
        read_manifest_from_payload({"too_large": "x" * (128 * 1024)})


# 功能：不在完整冻结范围内的旧支持超过100边时拒绝，已有开放争议也不重新盲审选边。
@pytest.mark.parametrize("case", ["overflow", "dispute"])
async def test_freeze_rejects_unsafe_sample(case):
    session, repo, belief, source, manifest, audit, _ = await environment()
    if case == "overflow":
        session.rows.extend(
            models.Evidence(id=uuid4(), belief_id=belief.id, source_id=source.id, stance="support")
            for _ in range(100)
        )
    else:
        session.rows.append(models.Dispute(id=uuid4(), belief_id=belief.id, status="open"))
    with pytest.raises(ValueError):
        await freeze_audit(repo, manifest, {}, model_identity(audit.llm))


# 功能：显式阶段及条数必须有界，未知阶段不能调用任意方法。
@pytest.mark.parametrize("phase,limit", [("shell", 1), ("answer", 0), ("verify", 21)])
async def test_phase_and_limits(phase, limit):
    _, _, _, _, _, audit, key = await environment()
    with pytest.raises(ValueError):
        await audit.run(key, phase, limit)
    assert not audit.llm.calls
