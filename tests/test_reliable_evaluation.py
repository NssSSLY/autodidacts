# 文件职责：无真实模型/数据库验证F03真值协议、双版兼容、训练隔离、评分重算、保持间隔及版本指标。
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from autodidact import models
from autodidact.experiments import (
    ModelMigrationProtocol,
    evaluate_suite,
    freeze_suite,
    prepare_suite_file,
    validate_skill,
)
from autodidact.learning.evaluation_contracts import ExamAnswer
from autodidact.learning.ground_truth import (
    ReliableItem,
    ReliableSuite,
    digest,
    evaluation_groups,
    grade_answer,
    model_identity,
)
from autodidact.learning.reliable_evaluation import (
    evaluate_reliable,
    recompute_run,
    run_benchmark,
    training_snapshot,
    verify_frozen,
)
from autodidact.learning_state import LearningState
from autodidact.metrics import dashboard
from autodidact.reporting import longitudinal_report


# 功能：复用真实静态算术样例，测试fixture不冒充专家真值。
def manifest():
    return ReliableSuite.model_validate(
        json.loads(Path("data/benchmark/reliable_sample.json").read_text(encoding="utf-8"))
    ).model_dump(mode="json")


# 功能：为完整可靠协议生成不可变摘要包，模拟冻结存储而非真实数据库。
def frozen_payload(raw=None):
    value = ReliableSuite.model_validate(raw or manifest()).model_dump(mode="json")
    return {**value, "sha256": digest(value)}


class FakeLLM:
    provider_name, model_name = "fixture", "reliable-test"
    base_url, default_temperature, max_output_tokens = "https://fixture.invalid", 0.2, 2048

    # 功能：预置确定答案或故障；记录所有请求以检查答案/训练题不泄漏。
    def __init__(self, fail=(), wrong=False):
        self.calls, self.context, self.fail, self.wrong = [], {}, set(fail), wrong

    # 功能：保存观察上下文供测试确认版本/题ID与分集追踪。
    def bind(self, **context):
        self.context = context

    # 功能：仅响应ExamAnswer；不可作为裁判，提供方故障不返回伪造正确回答。
    async def structured(self, system, user, schema):
        assert schema is ExamAnswer
        request = json.loads(user)
        self.calls.append(request)
        if len(self.calls) in self.fail:
            raise TimeoutError("secret fixture credentials")
        q = request["question"]
        answer = "15" if "7加8" in q else "12 m" if "速度" in q else "14"
        if "人工标签" in q:
            answer = "human-approved"
        return ExamAnswer(
            answer="wrong" if self.wrong and not request.get("method") else answer, confidence=0.8
        )


class NoJudge:
    # 功能：任何裁判调用都使可靠路径测试失败，证明未使用模型自评。
    async def structured(self, *args):
        raise AssertionError("可靠评分不应调用裁判")


class Repo:
    # 功能：只提供测试记录/幂等报告/接纳记忆，不运行真实数据库或学习器。
    def __init__(self, records=()):
        self.rows = list(records)
        self.reports = {}
        self.s = self
        self.memory = []
        self.commits = 0

    # 功能：按实体类型/ID返回预置记录。
    async def get(self, model, entity_id):
        return next((r for r in self.rows if isinstance(r, model) and r.id == entity_id), None)

    # 功能：模拟实际学习Goal查询，记录SQL以断言EXISTS/上限；不是实库语义证明。
    async def scalars(self, statement):
        self.last_statement = statement
        learned_ids = {r.goal_id for r in self.rows if isinstance(r, models.LearningSession)}
        return SimpleNamespace(
            all=lambda: [r for r in self.rows if isinstance(r, models.Goal) and r.id in learned_ids]
        )

    # 功能：按类型/键保存新快照或返回旧快照，复制payload以检测调用方意外覆盖。
    async def save_report(self, kind, payload, key=None):
        key = key or str(uuid4())
        if (kind, key) not in self.reports:
            row = models.ResearchReport(
                id=uuid4(),
                kind=kind,
                report_key=key,
                payload=copy.deepcopy(payload),
                created_at=datetime.now(UTC),
            )
            self.rows.append(row)
            self.reports[kind, key] = row
        return self.reports[kind, key]

    # 功能：返回预置记忆，报告之外不混入基准真值。
    async def accepted_memory(self, *args):
        return self.memory

    # 功能：记录技能元数据提交次数，不进行实际持久化。
    async def commit(self):
        self.commits += 1


# 功能：创建载有可靠协议的冻结报告，供各实际运行入口共用校验。
def frozen_report(payload=None):
    return models.ResearchReport(
        id=uuid4(),
        kind="frozen_benchmark",
        payload=payload or frozen_payload(),
        created_at=datetime.now(UTC),
    )


# 功能：重复题ID/题面/家族、缺保持/迁移、未知版本和未知字段都不进入可靠评估。
@pytest.mark.parametrize(
    "case", ["id", "family", "question", "transfer", "protocol", "scoring", "extra"]
)
def test_manifest_rejects_invalid_contract(case):
    raw = manifest()
    if case in {"id", "family", "question"}:
        field = {"id": "id", "family": "family_id", "question": "question"}[case]
        raw["items"][1][field] = raw["items"][0][field]
    elif case == "transfer":
        raw["items"][2]["split"] = "train"
    elif case in {"protocol", "scoring"}:
        raw["protocol" if case == "protocol" else "scoring_protocol"] = "unknown_v99"
    else:
        raw["execute_code"] = "never"
    with pytest.raises(ValidationError):
        ReliableSuite.model_validate(raw)


# 功能：人工真值必须保存人工记录/时区与匹配的参考快照；测试声明不是专家认证。
def reviewed_item():
    reference = "SENTINEL_REFERENCE_ANSWER_42：人工核查标签是human-approved。"
    return {
        "id": "reviewed-test",
        "family_id": "reviewed-family",
        "split": "test",
        "question": "人工标签是什么？",
        "truth": {
            "kind": "reviewed",
            "accepted_answers": ["human-approved"],
            "reference_uri": "fixture://review",
            "reference_text": reference,
            "reference_sha256": hashlib.sha256(reference.encode()).hexdigest(),
            "review": {
                "review_id": "fixture-review",
                "reviewer": "fixture-human-not-an-expert-certification",
                "role": "human",
                "decision": "approved",
                "reviewed_at": "2026-01-01T00:00:00Z",
                "note": "仅测试人工声明格式",
            },
        },
    }


# 功能：模型核查、未来/无时区时间、参考篡改、空答案不能冒充人工真值。
@pytest.mark.parametrize("case", ["role", "future", "naive", "digest", "empty"])
def test_reviewed_truth_rejects_untrusted_attestation(case):
    item = reviewed_item()
    truth = item["truth"]
    if case == "role":
        truth["review"]["role"] = "model"
    elif case in {"future", "naive"}:
        truth["review"]["reviewed_at"] = (
            "2999-01-01T00:00:00Z" if case == "future" else "2026-01-01"
        )

    elif case == "digest":
        truth["reference_text"] += "tampered"
    else:
        truth["accepted_answers"] = [" "]
    with pytest.raises(ValidationError):
        ReliableItem.model_validate(item)


# 功能：白名单精确十进制/单位评分拒绝近似乱猜、自由表达式、错误单位，不执行代码。
@pytest.mark.parametrize(
    "answer,correct",
    [
        ("12 m", True),
        ("12.0 m", True),
        ("12", False),
        ("12 km", False),
        ("11.9 m", False),
        ("3*4 m", False),
        ("NaN m", False),
        ("<script>12</script> m", False),
    ],
)
def test_numeric_grading(answer, correct):
    item = ReliableSuite.model_validate(manifest()).items[2]
    assert grade_answer(item, answer)["correct"] is correct


# 功能：非法计算/超界数值/除零/容差不能被导入为可复算真值。
@pytest.mark.parametrize(
    "operands,operation,tolerance",
    [
        (["1", "0"], "divide", "0"),
        (["NaN", "1"], "add", "0"),
        (["1e999", "1"], "add", "0"),
        (["1", "2"], "eval", "0"),
        (["1", "2"], "add", "1"),
    ],
)
def test_computed_truth_validation(operands, operation, tolerance):
    item = manifest()["items"][1]
    item["truth"].update(operands=operands, operation=operation, absolute_tolerance=tolerance)
    with pytest.raises(ValidationError):
        ReliableItem.model_validate(item)


# 功能：全路径只发held-out题/允许记忆，人工出处/答案/训练题不发给模型；不调用裁判。
async def test_reliable_evaluation_has_no_answer_leak():
    raw = manifest()
    raw["items"][1] = reviewed_item()
    llm = FakeLLM()
    result = await evaluate_suite(
        llm, NoJudge(), frozen_payload(raw), [{"statement": "一般研究经验"}]
    )
    assert result["score"] == 1 and result["n"] == 3 and result["complete"]
    assert len(llm.calls) == 3
    assert not any("训练示例" in request["question"] for request in llm.calls)
    serialized = json.dumps(llm.calls)
    assert "SENTINEL_REFERENCE" not in serialized and "human-approved" not in serialized
    assert all(set(request) == {"question", "memory", "method"} for request in llm.calls)
    assert result["split_metrics"]["transfer"]["n"] == 1
    assert result["calibration"] == pytest.approx(0.96)
    assert 0 < result["wilson_95"][0] < 1
    assert llm.context["scoring_protocol"] == "exact_numeric_v1"


# 功能：部分/全部提供方故障记unknown，不泄露异常凭据，不声称失败样本正确或涨分。
@pytest.mark.parametrize("fail", [(2,), (1, 2, 3)])
async def test_provider_failures_are_unknown(fail):
    result = await evaluate_suite(FakeLLM(fail=fail), NoJudge(), frozen_payload(), [])
    assert result["unknown_n"] == len(fail) and not result["complete"]
    assert "credentials" not in json.dumps(result)
    assert result["score"] is None if len(fail) == 3 else result["score"] == 1


# 功能：篡改题、真值、版本、评分协议或摘要均不能在任何完整快照入口通过。
@pytest.mark.parametrize("case", ["question", "truth", "version", "digest"])
def test_snapshot_tampering(case):
    payload = frozen_payload()
    if case == "question":
        payload["items"][1]["question"] += "tampered"
    elif case == "truth":
        payload["items"][1]["truth"]["operands"][0] = "100"
    elif case == "version":
        payload["suite_version"] += 1
    else:
        payload["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        verify_frozen(payload)


# 功能：冻结同内容重试幂等，同版本改真值拒绝，版本递增追加新报告；旧历史快照不改。
async def test_freeze_version_reservation_and_legacy_preservation(tmp_path):
    path = tmp_path / "suite.json"
    raw = manifest()
    path.write_text(json.dumps(raw), encoding="utf-8")
    repo = Repo()
    first = await freeze_suite(repo, path)
    saved = copy.deepcopy(first.payload)
    assert (await freeze_suite(repo, path)).id == first.id
    raw["items"][1]["truth"]["operands"][0] = "9"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="suite_version"):
        await freeze_suite(repo, path)
    raw["suite_version"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert (await freeze_suite(repo, path)).id != first.id
    assert first.payload == saved
    legacy = await freeze_suite(repo, "data/benchmark/sample.json")
    assert verify_frozen(legacy.payload) is None
    assert legacy.payload["sha256"] == digest(legacy.payload["items"])


# 功能：本地校验既有例子，无数据库/模型；超大文件在读取上限拒绝。
def test_offline_file_check_and_size_limit(tmp_path):
    assert prepare_suite_file("data/benchmark/reliable_sample.json")["suite_version"] == 1
    path = tmp_path / "oversize.json"
    path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="2MiB"):
        prepare_suite_file(path)


# 功能：旧精确答案协议继续可运行，但明确legacy而不自动认证成新真值。
async def test_legacy_protocol_remains_distinct():
    items = [
        {"question": "7加8是多少？", "accepted_answers": ["15"], "category": "demo", "rubric": ""}
    ]
    result = await evaluate_suite(
        FakeLLM(), NoJudge(), {"items": items, "sha256": digest(items)}, []
    )
    assert result["score"] == 1
    assert (
        result["protocol"] == "legacy_benchmark_v1"
        and result["truth_status"] == "legacy_unverified"
    )


# 功能：可靠报告可逐答案离线重算；原分数即使被改也按真值恢复，不执行答案代码。
async def test_recompute_from_saved_answers():
    suite = ReliableSuite.model_validate(manifest())
    result = await evaluate_reliable(FakeLLM(), suite, [])
    payload = {**result, "score": 0.01, "memory_snapshot": [], "skill_snapshot": None}
    assert recompute_run(payload, suite)["score"] == 1
    assert payload["score"] == 0.01
    bad = copy.deepcopy(payload)
    bad["details"].pop()
    with pytest.raises(ValueError):
        recompute_run(bad, suite)
    bad = copy.deepcopy(payload)
    bad["memory_snapshot"] = ["answer leakage"]
    with pytest.raises(ValueError):
        recompute_run(bad, suite)


# 功能：保持复测必须真实延迟且同模型/记忆；立即复测拒绝，旧分数/时间不被改写。
async def test_real_retention_interval_and_frozen_memory():
    frozen = frozen_report()
    repo, llm = Repo([frozen]), FakeLLM()
    repo.memory = [{"statement": "old memory"}]
    result = await run_benchmark(repo, llm, NoJudge(), frozen.id, use_memory=True)
    baseline = await repo.get(models.ResearchReport, UUID(result["report_id"]))
    saved = copy.deepcopy(baseline.payload)
    with pytest.raises(ValueError, match="间隔"):
        await run_benchmark(
            repo, llm, NoJudge(), frozen.id, use_memory=True, baseline_id=baseline.id
        )
    baseline.created_at = datetime.now(UTC) - timedelta(hours=48)
    repo.memory = [{"statement": "new memory must not replace snapshot"}]
    repeated = await run_benchmark(
        repo, llm, NoJudge(), frozen.id, use_memory=True, baseline_id=baseline.id
    )
    assert repeated["selected_splits"] == ["retention"]
    assert repeated["memory_snapshot"] == saved["memory_snapshot"]
    assert repeated["retention_comparison"]["retention_rate"] == 1
    assert baseline.payload == saved
    llm.base_url = "https://different.invalid"
    with pytest.raises(ValueError, match="模型"):
        await run_benchmark(
            repo, llm, NoJudge(), frozen.id, use_memory=True, baseline_id=baseline.id
        )


# 功能：规范训练题重复和train分集调用被拒绝，结构隔离不被模型提供方绕过。
async def test_training_leakage_rejected_before_model_call():
    llm, suite = FakeLLM(), ReliableSuite.model_validate(manifest())
    with pytest.raises(ValueError, match="重复"):
        await evaluate_reliable(
            llm, suite, [], training_questions=["  " + suite.items[1].question + "  "]
        )
    with pytest.raises(ValueError):
        await evaluate_reliable(llm, suite, [], splits=("train",))
    assert llm.calls == []


# 功能：四组比较的可靠版本一致，未知组不给伪造增益，也不自动切换模型。
async def test_model_migration_groups_are_versioned():
    frozen = frozen_report()
    repo = Repo([frozen])
    result = await ModelMigrationProtocol(repo, NoJudge()).compare(
        FakeLLM(), FakeLLM(fail=(1,)), frozen.id
    )
    assert set(result["groups"]) == {"A1", "A2", "B1", "B2"}
    assert result["base_model_gain"] is None and result["old_memory_gain"] == 0
    assert len({g["comparison_key"] for g in result["groups"].values()}) == 1


# 功能：旧未版本评分和新协议/不同模型分别汇总，不能生成跨版均值。
def test_version_groups_and_endpoint_identity():
    old = models.Evaluation(score=0.2, components={"audit": {"protocol": "closed_book_v1"}})
    new = models.Evaluation(
        score=0.9,
        components={"audit": {"protocol": "closed_book_v1", "scoring_protocol": "weighted_v1"}},
    )
    assert len(evaluation_groups([old, new])) == 2
    first, second = FakeLLM(), FakeLLM()
    second.base_url = "https://other.invalid"
    assert model_identity(first) != model_identity(second)


# 功能：可靠技能只有已知训练来源、5个独立held-out题、完整正增益才可验证；旧基准不晋升。
async def test_skill_requires_reliable_truth_and_training_provenance():
    raw = manifest()
    raw["independence_review"] = reviewed_item()["truth"]["review"]
    for index in range(2):
        item = copy.deepcopy(raw["items"][3])
        item.update(
            id=f"extra-{index}",
            family_id=f"independent-extra-{index}",
            question=f"独立任务{index}：20减6是多少？",
        )
        raw["items"].append(item)
    frozen = frozen_report(frozen_payload(raw))
    goal = models.Goal(id=uuid4(), title="实际训练任务", description="训练过程")
    attempt = models.LearningSession(id=uuid4(), goal_id=goal.id)
    skill = models.Skill(
        id=uuid4(),
        procedure=["核对单位"],
        confidence=0.3,
        metadata_json={"source_session_ids": [str(attempt.id)]},
    )
    repo = Repo([frozen, goal, attempt, skill])
    result = await validate_skill(repo, FakeLLM(wrong=True), NoJudge(), skill.id, frozen.id)
    assert result["status"] == "validated" and result["gain"] == 1
    assert skill.metadata_json["validation_protocol"] == "reliable_benchmark_v1"
    skill.metadata_json["source_session_ids"] = []
    result = await validate_skill(repo, FakeLLM(wrong=True), NoJudge(), skill.id, frozen.id)
    assert result["status"] == "candidate"


# 功能：实际学习目标污染在基准运行前拒绝，候选未研究目标不冒充训练记录。
async def test_actual_learning_goal_contamination_is_blocked():
    frozen = frozen_report()
    goal = models.Goal(id=uuid4(), title=manifest()["items"][1]["question"], description="")
    attempt = models.LearningSession(id=uuid4(), goal_id=goal.id)
    repo, llm = Repo([frozen, goal, attempt]), FakeLLM()
    with pytest.raises(ValueError, match="重复"):
        await run_benchmark(repo, llm, NoJudge(), frozen.id)
    assert llm.calls == [] and "EXISTS" in str(repo.last_statement)
    assert repo.last_statement._limit_clause.value == 1001


# 功能：实际训练扫描截断不能被当作全量隔离通过。
async def test_actual_training_scan_fails_closed_at_cap():
    rows = []
    for _ in range(1001):
        goal = models.Goal(id=uuid4(), title="训练", description="")
        rows.extend([goal, models.LearningSession(id=uuid4(), goal_id=goal.id)])
    with pytest.raises(ValueError, match="上限"):
        await training_snapshot(Repo(rows))


# 功能：冻结摘要被改的手动运行入口在答题前拒绝，不只在模型比较入口检查。
async def test_manual_run_rejects_tamper_before_provider():
    frozen = frozen_report()
    frozen.payload["sha256"] = "0" * 64
    llm = FakeLLM()
    with pytest.raises(ValueError):
        await run_benchmark(Repo([frozen]), llm, NoJudge(), frozen.id)
    assert llm.calls == []


class ConsumerSession:
    # 功能：给实际指标/成长/Profile消费者返回预置实体，记录写入；不声称模拟SQL过滤。
    def __init__(self, records):
        self.records, self.added, self.commits = records, [], 0

    # 功能：返回查询对应实体，报告列表沿用当前kind筛选的预置集合。
    async def scalars(self, statement):
        model = statement.column_descriptions[0]["entity"]
        return SimpleNamespace(all=lambda: [r for r in self.records if isinstance(r, model)])

    # 功能：计数查询提供无关状态0值，以专门验证消费者的评分分组。
    async def execute(self, *args):
        return SimpleNamespace(scalar_one=lambda: 0)

    # 功能：Profile查询默认无记录，捕获新建的派生Profile。
    async def scalar(self, *args):
        return None

    # 功能：返回已知目标供Profile按领域分组。
    async def get(self, model, entity_id):
        return next((r for r in self.records if isinstance(r, model) and r.id == entity_id), None)

    # 功能：只捕获派生Profile，不写认知实体。
    def add(self, row):
        self.added.append(row)

    # 功能：记录派生Profile提交动作。
    async def commit(self):
        self.commits += 1


# 功能：实际工作台dashboard与成长报告同时隔离旧/新评分版本，不只是测试纯分组函数。
async def test_metrics_and_longitudinal_consumer_version_isolation():
    now = datetime.now(UTC)
    old = models.Evaluation(
        score=0.2,
        passed=False,
        created_at=now,
        components={"audit": {"protocol": "closed_book_v1"}},
    )
    new = models.Evaluation(
        score=0.9,
        passed=True,
        created_at=now,
        components={"audit": {"protocol": "closed_book_v1", "scoring_protocol": "weighted_v1"}},
    )
    session = ConsumerSession([old, new])
    status = await dashboard(session)
    assert status["average_evaluation_score"] is None
    assert len(status["evaluation_score_groups"]) == 2
    repo = SimpleNamespace(s=session)

    # 功能：成长报告开放争议读取为空，与评分版本隔离无关。
    async def no_disputes(*args):
        return []

    repo.open_disputes = no_disputes
    report = await longitudinal_report(repo)
    assert len(report["daily_learning"]) == 2
    assert {p["mean_score"] for p in report["daily_learning"]} == {0.2, 0.9}


# 功能：实际Profile对不同冻结条件分组，汇总单分数置未知，部分提供方失败报告不当模型能力。
async def test_profile_consumer_does_not_merge_benchmark_versions():
    reliable = {
        "model": "fixture",
        "provider": "fixture",
        "score": 1,
        "complete": True,
        "comparison_key": "reliable-key",
        "details": [{"score": 1, "category": "demo"}],
    }
    legacy = {**reliable, "score": 0.2, "comparison_key": "legacy-key"}
    records = [
        models.ResearchReport(id=uuid4(), kind="benchmark_run", payload=run)
        for run in [reliable, legacy]
    ]
    session = ConsumerSession(records)
    state = LearningState()
    state.s = session
    assert await state.refresh_model_profiles() == 1
    profile = session.added[0]
    assert profile.benchmark_score is None
    assert len(profile.error_profile["benchmark_score_groups"]) == 2
    assert len(profile.domain_scores) == 2
