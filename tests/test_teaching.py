# 文件职责：用替身模型验证示范复用、反馈修订、重启保留、范围隔离和费用调用边界。
import asyncio
import json
import os
from uuid import uuid4

import pytest

from autodidact.local_models import LocalModels, ModelConfiguration, protect_key
from autodidact.local_tasks import TaskJournal
from autodidact.local_tools import LocalError
from autodidact.teaching import (
    CourseRequest,
    Demonstration,
    Feedback,
    TeachingService,
    TeachingStore,
    TryRequest,
)


class Model:
    provider_name, model_name = "offline_test", "demonstration-fixture"

    # 功能：保存预置回答和实际收到的模型上下文，不访问API。
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}

    # 功能：记录提示并逐次返回合成回答，只验证控制器流程。
    async def text(self, system, prompt):
        self.prompts.append((system, json.loads(prompt)))
        return self.answers.pop(0)


# 功能：每个教学用例建立独立日志，退出时关闭以验证重启兼容。
@pytest.fixture
def journal(tmp_path):
    value = TaskJournal(tmp_path / "state")
    yield value
    value.close()


# 功能：创建不调用模型的任务教学类别。
def course(store, name="工作周报"):
    return store.create_course(CourseRequest(name=name, instructions="按已完成、问题、下一步输出"))["course"]["id"]


# 功能：为显式发送/反馈生成独立幂等标识。
def request(text):
    return TryRequest(input=text, request_id=str(uuid4()))


# 功能：认可后的答案进入同任务示范，并在新输入的实际提示中复用。
async def test_demonstration_is_used_on_new_input(journal):
    model = Model(["已完成：A\n问题：无\n下一步：B", "已完成：C\n问题：无\n下一步：D"])
    service = TeachingService(journal, LocalModels(journal.directory), lambda: model)
    cid = course(service.store)
    original = service.store.add_sample(cid, Demonstration(input="完成X，准备Y", output="已完成：X\n下一步：Y"))
    run = service.send(cid, request("完成A，准备B"))
    await service.job
    first = service.store.run(run["id"])
    assert first["status"] == "review"
    assert model.prompts[0][1]["teaching_context"]["demonstrations"][0]["id"] == original["id"]
    accepted = service.feedback(run["id"], Feedback(action="accept", version=1, request_id=str(uuid4())))
    assert accepted["status"] == "accepted"
    next_run = service.send(cid, request("完成C，准备D"))
    await service.job
    snapshot = model.prompts[1][1]["teaching_context"]
    assert accepted["accepted_sample_id"] in {s["id"] for s in snapshot["demonstrations"]}
    assert service.store.run(next_run["id"])["versions"][0]["evidence_level"] == 0
    assert not snapshot["seen_input"]


# 功能：反馈保留原输出、产生新版本，用户明确保存的通用要求进入后续新任务。
async def test_feedback_revision_and_future_requirement(journal):
    model = Model(["A B", "已完成：A\n下一步：B", "已完成：C\n下一步：D"])
    service = TeachingService(journal, LocalModels(journal.directory), lambda: model)
    cid = course(service.store)
    run = service.send(cid, request("完成A，准备B"))
    await service.job
    correction = Feedback(action="revise", version=1, note="用分行标题，禁止省略下一步", remember=True,
                          ideal_output="已完成：A\n下一步：B", request_id=str(uuid4()))
    service.feedback(run["id"], correction)
    await service.job
    revised = service.store.run(run["id"])
    assert [v["answer"] for v in revised["versions"]] == ["A B", "已完成：A\n下一步：B"]
    assert model.prompts[1][1]["revise"]["user_feedback"] == correction.note
    before = len(revised["versions"])
    service.feedback(run["id"], correction)
    assert len(service.store.run(run["id"])["versions"]) == before
    service.send(cid, request("完成C，准备D"))
    await service.job
    rules = model.prompts[2][1]["teaching_context"]["user_requirements"]
    assert [r["text"] for r in rules] == [correction.note]
    with pytest.raises(LocalError):
        service.feedback(run["id"], Feedback(action="accept", version=1, request_id=str(uuid4())))


# 功能：只修订当前回答的反馈不会静默变成永久要求。
async def test_feedback_without_remember_is_local(journal):
    model = Model(["原回答", "本轮修订"])
    service = TeachingService(journal, LocalModels(journal.directory), lambda: model)
    cid = course(service.store)
    run = service.send(cid, request("材料"))
    await service.job
    service.feedback(run["id"], Feedback(action="revise", version=1, note="这次补充日期", request_id=str(uuid4())))
    await service.job
    assert service.store.detail(cid)["rules"] == []
    assert service.store.detail(cid)["samples"] == []


# 功能：未认可回答、其它任务示范与已停用示范不进入当前任务的示范集。
async def test_scope_and_deactivation(journal):
    model = Model(["未经认可"])
    service = TeachingService(journal, LocalModels(journal.directory), lambda: model)
    first, second = course(service.store), course(service.store, "其它任务")
    hidden = service.store.add_sample(second, Demonstration(input="SECRET_CASE", output="OTHER_OUTPUT"))
    own = service.store.add_sample(first, Demonstration(input="自己", output="本任务输出"))
    service.store.set_active(first, "samples", own["id"], False)
    run = service.send(first, request("新材料"))
    await service.job
    context = model.prompts[0][1]["teaching_context"]
    assert context["demonstrations"] == []
    assert hidden["id"] not in json.dumps(context)
    assert service.store.detail(first)["samples"][0]["id"] == own["id"]
    assert len(service.store.detail(first)["samples"]) == 1
    assert service.store.run(run["id"])["status"] == "review"


# 功能：重复输入标为示范重放；重复存入不虚增示范数量。
def test_seen_input_and_deduplicated_samples(journal):
    store = TeachingStore(journal)
    cid = course(store)
    sample = Demonstration(input="样例材料", output="理想结果")
    assert store.add_sample(cid, sample)["id"] == store.add_sample(cid, sample)["id"]
    assert store.context(cid, "样例材料", None)["seen_input"]
    assert len(store.detail(cid)["samples"]) == 1


# 功能：独立日志重启后示范和要求仍能检索，不依赖当前模型会话。
def test_teaching_survives_restart(tmp_path):
    journal = TaskJournal(tmp_path / "state")
    store = TeachingStore(journal)
    cid = course(store)
    sample = store.add_sample(cid, Demonstration(input="原始材料", output="理想结果"))
    store.add_rule(cid, "始终标明缺失信息", "user")
    journal.close()
    reopened = TaskJournal(tmp_path / "state")
    try:
        snapshot = TeachingStore(reopened).context(cid, "新材料", None)
        assert snapshot["demonstrations"][0]["id"] == sample["id"]
        assert snapshot["user_requirements"][0]["text"] == "始终标明缺失信息"
    finally:
        reopened.close()


# 功能：未配置模型时可保存示范，但不伪造回答、不预留收费调用。
def test_mock_requires_model_and_preserves_demonstration(journal):
    service = TeachingService(journal, LocalModels(journal.directory))
    cid = course(service.store)
    service.store.add_sample(cid, Demonstration(input="输入", output="输出"))
    with pytest.raises(LocalError, match="模型"):
        service.send(cid, request("新材料"))
    assert len(service.store.detail(cid)["samples"]) == 1
    assert service.store.detail(cid)["runs"] == []
    assert journal.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0


# 功能：相同发送标识只产生一次调用，进行中不能新开并发生成。
async def test_send_idempotency_and_concurrency(journal):
    entered, release = asyncio.Event(), asyncio.Event()

    class WaitingModel(Model):
        # 功能：模拟一条正在进行的请求，不涉及网络。
        async def text(self, *args):
            entered.set()
            await release.wait()
            return "回答"

    service = TeachingService(journal, LocalModels(journal.directory), lambda: WaitingModel([]))
    cid = course(service.store)
    req = request("新材料")
    run = service.send(cid, req)
    await entered.wait()
    assert service.send(cid, req)["id"] == run["id"]
    with pytest.raises(LocalError):
        service.send(cid, request("其它输入"))
    release.set()
    await service.job
    assert journal.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1


# 功能：立即取消和在途取消均有持久中断标记，不产生自动重试。
@pytest.mark.parametrize("immediate", [True, False])
async def test_stop_does_not_replay(journal, immediate):
    entered = asyncio.Event()

    class WaitingModel(Model):
        # 功能：模拟已开始但没有返回的生成。
        async def text(self, *args):
            entered.set()
            await asyncio.Event().wait()

    service = TeachingService(journal, LocalModels(journal.directory), lambda: WaitingModel([]))
    cid = course(service.store)
    run = service.send(cid, request("材料"))
    if not immediate:
        await entered.wait()
    await service.stop()
    assert service.store.run(run["id"])["status"] == "interrupted"
    assert journal.db.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    assert journal.db.execute("SELECT status FROM model_calls").fetchone()[0] == "interrupted"


# 功能：页面详情不重复传完整提示；原始快照和使用过的示范ID仍可追溯。
async def test_public_history_does_not_rewrite_snapshot(journal):
    model = Model(["回答"])
    service = TeachingService(journal, LocalModels(journal.directory), lambda: model)
    cid = course(service.store)
    service.send(cid, request("材料"))
    await service.job
    assert "prompt" not in service.store.public_detail(cid)["runs"][0]["versions"][0]
    assert "prompt" in service.store.detail(cid)["runs"][0]["versions"][0]


# 功能：错误和空回答不会加入示范，发送失败仍消耗一次预留且不重试。
async def test_invalid_answer_is_not_learned(journal):
    model = Model([""])
    service = TeachingService(journal, LocalModels(journal.directory), lambda: model)
    cid = course(service.store)
    run = service.send(cid, request("材料"))
    await service.job
    assert service.store.run(run["id"])["status"] == "failed"
    assert service.store.detail(cid)["samples"] == []
    assert len(model.prompts) == 1


# 功能：当日50次共享配额耗尽时不发送请求，不留永远等待生成的状态。
async def test_daily_budget_blocks_before_send(journal):
    from autodidact.local_tasks import now

    for _ in range(50):
        journal.db.execute("INSERT INTO model_calls(task_id,day,created,status,usage) VALUES(?,?,?,'received','{}')",
                           ("old", now()[:10], now()))
    journal.db.commit()
    model = Model(["不应调用"])
    service = TeachingService(journal, LocalModels(journal.directory), lambda: model)
    cid = course(service.store)
    with pytest.raises(LocalError, match="50"):
        service.send(cid, request("材料"))
    assert model.prompts == [] and service.store.detail(cid)["runs"][0]["status"] == "failed"


# 功能：真实Windows DPAPI往返只用合成密钥，不接触个人凭据。
@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI专用")
def test_dpapi_and_model_configuration(journal):
    secret = "synthetic-test-key-not-a-credential"
    assert protect_key(protect_key(secret.encode()), decrypt=True).decode() == secret
    models = LocalModels(journal.directory)
    view = models.save(ModelConfiguration(base_url="https://example.invalid/v1", model="fixture", api_key=secret))
    assert secret not in json.dumps(view)
    assert secret not in models.path.read_text()
    reopened = LocalModels(journal.directory)
    assert reopened.build().api_key == secret
    with pytest.raises(LocalError):
        reopened.save(ModelConfiguration(base_url="https://different.invalid/v1", model="fixture"))
    assert secret not in json.dumps(reopened.info())


# 功能：远端明文HTTP、URL账号或查询密钥不能作为新配置。
@pytest.mark.parametrize("url", ["http://example.invalid/v1", "https://user:secret@example.invalid/v1", "https://example.invalid/v1?key=x"])
def test_model_endpoint_validation(journal, url):
    with pytest.raises(LocalError):
        LocalModels(journal.directory).save(ModelConfiguration(base_url=url, model="fixture", api_key="synthetic"))


# 功能：旧配置URL含认证或查询参数时，页面信息不回显这些内容。
def test_legacy_model_info_redacts_url_credentials(journal, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("autodidact.local_models.runtime_settings", lambda: SimpleNamespace(
        llm_provider="openai_compatible", llm_model="fixture", llm_api_key="synthetic-key",
        llm_base_url="https://user:synthetic-secret@example.invalid/v1?key=synthetic-query#secret"))
    info = LocalModels(journal.directory).info()
    assert info["base_url"] == "https://example.invalid/v1"
    assert "synthetic" not in json.dumps(info)


# 功能：配额耗尽的修订不记录已处理反馈，不保存要求或增加版本；原答案仍可认可。
async def test_revision_budget_failure_keeps_feedback_retryable(journal):
    from autodidact.local_tasks import now

    model = Model(["原答案"])
    service = TeachingService(journal, LocalModels(journal.directory), lambda: model)
    cid = course(service.store)
    run = service.send(cid, request("材料"))
    await service.job
    for _ in range(49):
        journal.db.execute("INSERT INTO model_calls(task_id,day,created,status,usage) VALUES(?,?,?,'received','{}')",
                           ("old", now()[:10], now()))
    journal.db.commit()
    correction = Feedback(action="revise", version=1, note="完整列出缺失项", remember=True, request_id=str(uuid4()))
    with pytest.raises(LocalError, match="50"):
        service.feedback(run["id"], correction)
    current = service.store.run(run["id"])
    assert current["status"] == "review" and len(current["versions"]) == 1
    assert current["feedback"] == [] and service.store.detail(cid)["rules"] == []
    service.feedback(run["id"], Feedback(action="accept", version=1, request_id=str(uuid4())))
    assert len(model.prompts) == 1


# 功能：同轮最多四次生成；错误反馈标识复用不能改变内容或突破次数。
async def test_revision_limit_and_feedback_identity(journal):
    model = Model(["初稿", "二稿", "三稿", "四稿"])
    service = TeachingService(journal, LocalModels(journal.directory), lambda: model)
    cid = course(service.store)
    run = service.send(cid, request("材料"))
    await service.job
    for version in range(1, 4):
        feedback = Feedback(action="revise", version=version, note="继续调整", request_id=str(uuid4()))
        service.feedback(run["id"], feedback)
        await service.job
    with pytest.raises(LocalError, match="4次"):
        service.feedback(run["id"], Feedback(action="revise", version=4, note="再来", request_id=str(uuid4())))
    with pytest.raises(LocalError, match="标识"):
        service.feedback(run["id"], feedback.model_copy(update={"note": "另一条反馈"}))
    assert len(service.store.run(run["id"])["versions"]) == 4 and len(model.prompts) == 4
