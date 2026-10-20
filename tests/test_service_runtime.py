# 文件职责：离线验证F08有限计划/停止取消/预算/锁丢失/日志，以及只读健康与本地HTTP安全；不访问真实数据库/模型。
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_workbench_audit import http_request

from autodidact import service_runtime as runtime
from autodidact import workbench
from autodidact.runtime import ControllerBusy
from autodidact.service_runtime import PlanRunner, RunPlan, read_run_plan


class MemoryStore:
    # 功能：提供只保存计划/进度的替身，无数据库或事实写入。
    def __init__(self):
        self.rows = {}

    # 功能：模拟创建独立运行，保留计划值供合同检查。
    async def create(self, plan):
        key = uuid4()
        self.rows[key] = {"plan": plan.model_dump(), "status": "running", "cycles": 0}
        return key

    # 功能：合并安全进度，不把任意学习结果正文写进运行报告。
    async def update(self, key, **fields):
        self.rows[key].update(fields)


# 功能：为状态机提供离线健康/计划锁替身；真实数据库锁单独验收。
@pytest.fixture
def offline(monkeypatch):
    # 功能：默认数据库就绪，不发网络请求。
    async def health(engine):
        return {"ready": True}

    # 功能：无数据库的锁上下文，不宣称验证跨进程互斥。
    @asynccontextmanager
    async def lease(engine):
        yield object()

    # 功能：默认模拟持有真实服务锁。
    async def assert_lease(owner):
        return None

    monkeypatch.setattr(runtime, "service_health", health)
    monkeypatch.setattr(runtime, "service_lease", lease)
    monkeypatch.setattr(runtime, "assert_service_lease", assert_lease)


# 功能：样例/默认计划有限，未知参数/协议、布尔及越界值拒绝。
@pytest.mark.parametrize(
    "payload",
    [
        {"max_cycles": 0},
        {"max_cycles": 1001},
        {"max_cycles": True},
        {"interval_seconds": -1},
        {"max_runtime_seconds": 86401},
        {"shell": "do something"},
        {"protocol": "unknown"},
    ],
)
def test_plan_validation(payload):
    assert read_run_plan("config/run_plan.example.json").max_cycles == 3
    with pytest.raises(ValueError):
        RunPlan.model_validate(payload)


# 功能：有限循环严格到上限，持久/日志只含白名单状态和UUID，不含原始结果/凭据。
async def test_plan_finishes_and_logs_safe_progress(offline):
    calls, logs, store, session_id = [], [], MemoryStore(), str(uuid4())

    # 功能：返回带恶意正文的正常结果，检查运行器仅提取安全字段。
    async def cycle():
        calls.append(1)
        return {
            "status": "passed",
            "session_id": session_id,
            "reason": "secret-key",
            "goal": "私有主题",
        }

    result = await PlanRunner(None, cycle, store, SimpleNamespace(info=logs.append)).run(
        RunPlan(max_cycles=3, interval_seconds=0)
    )
    assert result["status"] == "completed" and len(calls) == 3
    row = next(iter(store.rows.values()))
    assert row["last_session_id"] == session_id and row["phase"] == "finished"
    combined = json.dumps(list(store.rows.values()), default=str) + "".join(logs)
    assert "secret-key" not in combined and "私有主题" not in combined
    assert len([json.loads(line) for line in logs]) == 5


# 功能：预算/空闲/失败/未知状态立即停止，不跳过预算或无限重试；未知字符串不能进入日志。
@pytest.mark.parametrize(
    "outcome", ["budget_exhausted", "busy", "idle", "failed", "resume_incompatible", "secret_token"]
)
async def test_plan_stops_on_terminal_outcomes(offline, outcome):
    store, logs = MemoryStore(), []

    # 功能：模拟控制器的有限终态，无外部调用。
    async def cycle():
        return {"status": outcome}

    result = await PlanRunner(None, cycle, store, SimpleNamespace(info=logs.append)).run(
        RunPlan(max_cycles=3, interval_seconds=0)
    )
    assert result["status"] == "stopped" and result["cycles"] == 1
    if outcome == "secret_token":
        assert result["reason"] == "unknown" and outcome not in "".join(logs)


# 功能：启动前的停止不被clear丢弃，当前轮收尾后不再启动下一轮。
@pytest.mark.parametrize("before_start", [True, False])
async def test_graceful_stop_not_lost(offline, before_start):
    store = MemoryStore()

    # 功能：在途轮请求停止后仍完成当前结果。
    async def cycle():
        runner.request_stop()
        return {"status": "passed"}

    runner = PlanRunner(None, cycle, store)
    if before_start:
        runner.request_stop()
    result = await runner.run(RunPlan(max_cycles=5, interval_seconds=0))
    assert result["status"] == "stopped" and result["cycles"] == int(not before_start)


# 功能：中断/运行时上限会取消在途轮并保存interrupted，不自动开始下一轮。
@pytest.mark.parametrize("mode", ["cancel", "deadline"])
async def test_cancel_and_runtime_limit(offline, mode):
    store, started, cleaned = MemoryStore(), asyncio.Event(), []

    # 功能：用可取消等待模拟在途操作，记录清理是否执行。
    async def cycle():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    task = asyncio.create_task(PlanRunner(None, cycle, store).run(RunPlan(max_runtime_seconds=1)))
    await started.wait()
    if mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert (await task)["reason"] == "runtime_limit"
    assert cleaned and next(iter(store.rows.values()))["status"] == "interrupted"


# 功能：外层周期TimeoutError不能冒充总时限，锁丢失则不调用学习回调。
@pytest.mark.parametrize("case", ["provider_timeout", "lease_lost", "unready"])
async def test_no_work_after_failed_admission(offline, monkeypatch, case):
    store, calls = MemoryStore(), []

    # 功能：模拟单轮抛超时，而非计划整体期限。
    async def cycle():
        calls.append(1)
        raise TimeoutError("secret error")

    # 功能：模拟数据库重连后已不持有原服务锁。
    async def lost(owner):
        raise ControllerBusy("secret")

    # 功能：数据库未就绪时不建任务/不请求模型。
    async def unready(engine):
        return {"ready": False}

    if case == "lease_lost":
        monkeypatch.setattr(runtime, "assert_service_lease", lost)
    if case == "unready":
        monkeypatch.setattr(runtime, "service_health", unready)
        with pytest.raises(ValueError):
            await PlanRunner(None, cycle, store).run(RunPlan())
        assert not store.rows and not calls
        return
    result = await PlanRunner(None, cycle, store).run(RunPlan())
    assert result["status"] == "failed" and result["reason"] == (
        "TimeoutError" if case == "provider_timeout" else "ControllerBusy"
    )
    assert len(calls) == int(case == "provider_timeout")


class ReadConnection:
    # 功能：记录健康查询，仅提供版本/表名，不模拟任何认知写入。
    def __init__(self, revision=None, error=False):
        self.revision, self.error, self.sql = revision, error, []

    # 功能：提供只读事务上下文。
    def begin(self):
        return self

    # 功能：进入连接/事务，可模拟拒绝连接且不泄漏凭据。
    async def __aenter__(self):
        if self.error:
            raise OSError("private password")
        return self

    # 功能：退出只读事务，不提交写入。
    async def __aexit__(self, *args):
        return None

    # 功能：记录SET只读及语句超时SQL。
    async def execute(self, query):
        self.sql.append(str(query))

    # 功能：返回就绪表集合，或单HEAD版本结果。
    async def scalars(self, query):
        self.sql.append(str(query))
        if "FROM alembic_version" in str(query):
            return SimpleNamespace(all=lambda: [self.revision])
        return SimpleNamespace(all=lambda: [*runtime.expected_schema_columns(), "alembic_version"])

    # 功能：返回当前迁移版本，不执行迁移。
    async def scalar(self, query):
        self.sql.append(str(query))
        return self.revision


# 功能：健康诊断只读/不迁移，未知版本和连接故障只给安全状态/类别。
@pytest.mark.parametrize("case", ["ready", "old", "offline"])
async def test_health_is_read_only_and_sanitized(case):
    connection = ReadConnection(
        runtime.HEAD_REVISION if case == "ready" else "old", case == "offline"
    )
    result = await runtime.service_health(SimpleNamespace(connect=lambda: connection))
    assert result["ready"] == (case == "ready") and "private" not in json.dumps(result)
    assert all(q.startswith(("SELECT", "SET")) for q in connection.sql)
    if case != "offline":
        assert connection.sql[0] == "SET TRANSACTION READ ONLY"


# 功能：真实回环HTTP存活无需token且无数据库详情；就绪仍需token且未就绪返回503。
async def test_http_health_protection(monkeypatch):
    app = workbench.Workbench(8765)

    # 功能：模拟只读未就绪，不访问实际数据库。
    async def health(engine):
        return {"ready": False, "reason": "test"}

    monkeypatch.setattr(workbench, "service_health", health)
    alive = await http_request(app, {}, "/healthz")
    assert "200 OK" in alive and '"alive":true' in alive and "revision" not in alive
    assert "400 Bad Request" in await http_request(app, {}, "/api/service/health")
    response = await http_request(app, {"X-Autodidact-Token": app.token}, "/api/service/health")
    assert "503 Service Unavailable" in response and '"ready": false' in response


# 功能：计划预览不启动任务/付费，立即停止已排队任务不丢失；未知动作不能执行任意控制。
async def test_workbench_plan_requires_explicit_start():
    app = workbench.Workbench(8765, RunPlan(max_cycles=2))
    assert (await app.dispatch("/api/service/plan", "GET", {}))["auto_start"] is False
    assert app.task is None
    await app.dispatch("/api/service/start", "POST", {})
    await app.dispatch("/api/service/stop", "POST", {})
    with pytest.raises(asyncio.CancelledError):
        await app.task
    assert app.job["reason"] == "cancelled_before_start"
    app.runner = SimpleNamespace(request_stop=lambda: pytest.fail("不能停止上一次已结束的runner"))
    # 功能：暂不创建新runner，固定在排队/启动窗口测试停止，不依赖AsyncSession退出时调度时序。
    app.learn = lambda: asyncio.sleep(3600)
    await app.dispatch("/api/run", "POST", {})
    assert app.runner is None
    await app.dispatch("/api/service/stop", "POST", {})
    with pytest.raises(asyncio.CancelledError):
        await app.task
    assert app.job["reason"] == "cancelled_before_start"
    for path, method, body in [
        ("/api/service/shell", "POST", {}),
        ("/api/service/start", "GET", {}),
        ("/api/service/plan?x=1", "GET", {}),
        ("/api/service/start", "POST", {"shell": "x"}),
    ]:
        with pytest.raises(ValueError):
            await app.dispatch(path, method, body)
