"""六站配置、受控动作、资料互斥与0级人工观察的离线契约；不访问真实数据库或网站。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from autodidact import cognitive_sources
from autodidact.config import RuntimeSettings
from autodidact.web_models import auxiliary, auxiliary_config
from autodidact.web_models.auxiliary_config import (
    AuxiliaryEntry,
    SaveAuxiliary,
    read_registry,
    save_source,
)
from autodidact.web_models.profile_lock import profile_lease
from autodidact.web_models.service import WebModelService
from autodidact.workbench import Workbench


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """配置只指向临时目录，不读取用户实际环境或个人清单。"""
    config = RuntimeSettings(
        _env_file=None,
        web_auxiliary_config=str(tmp_path / "aux.json"),
        cognitive_sources_config=str(tmp_path / "catalog.json"),
    )
    monkeypatch.setattr(auxiliary_config, "runtime_settings", lambda: config)
    monkeypatch.setattr(cognitive_sources, "runtime_settings", lambda: config)
    monkeypatch.chdir(tmp_path)
    return config


def entry(provider="aux_deepseek", **updates):
    """构造需要人工核查的模拟选择器；不声称真实站点支持。"""
    return AuxiliaryEntry(id=provider, **updates)


def browser_entry(provider="aux_deepseek"):
    """合格自动配置包含新会话动作；这些选择器仅属于测试页面。"""
    return entry(
        provider,
        enabled=True,
        mode="browser",
        input_selector="#input",
        send_selector="#send",
        assistant_message_selector=".answer",
        new_chat_selector="#new",
    )


def save(value):
    """以最新版本保存单个配置，生产代码执行原子写。"""
    return save_source(SaveAuxiliary(revision=read_registry()[1], source=value))


def test_default_six_sources_are_inactive_and_separate(settings):
    """六个站点默认关闭，Claude两入口和资料不同，只读不创建个人文件。"""
    view = auxiliary.AuxiliaryWorkbench(None).snapshot()
    assert len(view["sources"]) == 6
    assert not any(s["enabled"] for s in view["sources"])
    assert (
        entry("aux_claude").browser_config().persistent_profile_dir
        != entry("aux_claude_code").browser_config().persistent_profile_dir
    )
    assert not auxiliary_config.registry_path().exists()


@pytest.mark.parametrize(
    "change",
    [
        {"id": "arbitrary"},
        {"id": "aux_claude_code", "mode": "browser"},
        {"enabled": "true"},
        {"mode": "browser", "enabled": True},
        {"url": "http://127.0.0.1"},
        {"persistent_profile_dir": "C:/Users/private"},
    ],
)
def test_config_rejects_unsafe_or_incomplete_values(change):
    """拒绝未知站点、路径注入、隐式布尔转换、任务自动化和缺选择器。"""
    with pytest.raises(ValidationError):
        AuxiliaryEntry.model_validate({"id": "aux_deepseek", **change})


def test_atomic_save_preserves_other_sources_and_rejects_stale(settings, monkeypatch):
    """过期版本及替换失败都不覆盖原内容，其他来源保持不变。"""
    original_revision = read_registry()[1]
    save(entry(enabled=True))
    save(entry("aux_claude_code", enabled=True))
    before = auxiliary_config.registry_path().read_bytes()
    with pytest.raises(ValueError, match="配置已改变"):
        save_source(SaveAuxiliary(revision=original_revision, source=entry()))
    assert auxiliary_config.registry_path().read_bytes() == before

    def fail_replace(*args):
        """模拟磁盘提交失败而非真实破坏文件。"""
        raise OSError("simulated")

    monkeypatch.setattr(auxiliary_config.os, "replace", fail_replace)
    with pytest.raises(OSError):
        save(entry())
    assert auxiliary_config.registry_path().read_bytes() == before
    assert not list(auxiliary_config.registry_path().parent.glob("*.tmp"))


def test_catalog_merge_disable_and_service_gate(settings):
    """自动来源进入F11可选项，关闭立即阻止后续调用；人工来源不冒充自动盲答。"""
    save(entry("aux_claude_code", enabled=True))
    save(browser_entry())
    catalog = cognitive_sources.read_catalog()
    assert [s.id for s in catalog.sources] == ["aux_deepseek"]
    adapter = WebModelService(None).adapter("aux_deepseek")
    assert adapter.config.require_new_chat
    save(entry())
    with pytest.raises(ValueError, match="未启用"):
        WebModelService(None).adapter("aux_deepseek")


def test_catalog_collision_and_corruption_do_not_overwrite(settings):
    """既有F11同名配置拒绝合并，损坏辅助文件不会静默重置。"""
    from pathlib import Path

    Path(settings.cognitive_sources_config).write_text(
        '{"sources":[{"id":"aux_deepseek","kind":"mock"}]}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="重复"):
        save(browser_entry())
    assert not auxiliary_config.registry_path().exists()
    auxiliary_config.registry_path().write_text("invalid", encoding="utf-8")
    with pytest.raises(ValidationError):
        read_registry()


def test_profile_lock_releases_after_failure(tmp_path):
    """同资料排斥并发，异常退出可再次获取，不删除资料。"""
    profile = tmp_path / "profile"
    with pytest.raises(RuntimeError), profile_lease(profile):
        with pytest.raises(ValueError, match="使用"), profile_lease(profile):
            pytest.fail("重复锁成功")
        raise RuntimeError("simulated")
    with profile_lease(profile):
        pass


async def test_routes_are_strict_and_snapshot_needs_no_database(settings):
    """配置GET无数据库；未知方法、查询、字段均在任何副作用前拒绝。"""
    app = Workbench(8765)
    assert len((await app.dispatch("/api/auxiliary", "GET", {}))["sources"]) == 6
    for path, method, body in [
        ("/api/auxiliary/login", "GET", {}),
        ("/api/auxiliary/save?x=1", "POST", {}),
        ("/api/auxiliary/close", "POST", {"unexpected": True}),
        ("/api/auxiliary/history?id=x&id=y", "GET", {}),
    ]:
        with pytest.raises(ValueError):
            await app.dispatch(path, method, body)


async def test_ask_permission_revision_and_manual_mode(settings, monkeypatch):
    """未授权、过期及人工模式不会发送；合格自动调用经过已有预算审计服务。"""
    desk = auxiliary.AuxiliaryWorkbench(None)
    call = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {"response": "线索"}))
    monkeypatch.setattr(auxiliary.WebModelService, "ask", call)
    save(entry(enabled=True))
    body = {
        "id": "aux_deepseek",
        "revision": read_registry()[1],
        "prompt": "研究问题",
        "consent": True,
    }
    for modified in ({**body, "consent": False}, {**body, "revision": "0" * 64}, body):
        with pytest.raises(ValueError):
            await desk.action("ask", modified)
    call.assert_not_awaited()
    save(browser_entry())
    body["revision"] = read_registry()[1]
    result = await desk.action("ask", body)
    assert result["evidence_level"] == 0
    assert call.await_args.kwargs["fresh_chat"] is True
    assert len(call.await_args.kwargs["expected_config_sha256"]) == 64


@pytest.mark.parametrize(
    "reference",
    [
        "javascript:alert(1)",
        "https://evil.test/",
        "https://chat.deepseek.com/?token=x",
        "https://user:password@chat.deepseek.com/",
    ],
)
def test_manual_reference_cannot_be_foreign_or_contain_credentials(reference):
    """导入出处不是任意URL或秘密携带通道。"""
    with pytest.raises(ValidationError):
        auxiliary.ImportSource(
            id="aux_deepseek",
            revision="0" * 64,
            prompt="问题",
            consent=True,
            response="回答",
            observation_id=uuid4(),
            conversation_ref=reference,
        )


class MemorySession:
    """只捕获PostgreSQL INSERT与去重意图，不冒充真实事务或并发验收。"""

    def __init__(self):
        self.rows, self.statements = {}, []

    async def __aenter__(self):
        """返回同一个内存替身。"""
        return self

    async def __aexit__(self, *args):
        """不执行任何真实数据库操作。"""

    def begin(self):
        """提供异步事务上下文形状。"""
        return self

    async def execute(self, statement):
        """只模拟主键冲突不插入，检查SQL确实使用ON CONFLICT。"""
        compiled = statement.compile(dialect=postgresql.dialect())
        assert "ON CONFLICT (id) DO NOTHING" in str(compiled)
        self.statements.append(statement)
        self.rows.setdefault(compiled.params["id"], SimpleNamespace(**compiled.params))

    async def get(self, model, identity):
        """按显式主键返回已捕获观察。"""
        return self.rows.get(identity)


async def test_manual_import_is_zero_level_idempotent_and_not_a_model_call(settings, monkeypatch):
    """相同请求ID重试不加记录，内容变化不能覆盖旧观察，未创建任何知识实体。"""
    session = MemorySession()
    monkeypatch.setattr(auxiliary, "async_sessionmaker", lambda *a, **k: lambda: session)
    save(entry("aux_claude_code", enabled=True))
    request = {
        "id": "aux_claude_code",
        "revision": read_registry()[1],
        "prompt": "只读研究问题",
        "response": "待核验观点<script>不执行</script>",
        "consent": True,
        "observation_id": str(uuid4()),
    }
    desk = auxiliary.AuxiliaryWorkbench(None)
    first = await desk.action("import", request)
    assert await desk.action("import", request) == first
    assert len(session.rows) == 1
    row = next(iter(session.rows.values()))
    assert row.access_type == "web_manual"
    assert row.metadata_json["evidence_level"] == 0 and not row.metadata_json["provenance_verified"]
    with pytest.raises(ValueError, match="已有不同内容"):
        await desk.action("import", {**request, "response": "试图覆盖"})
    assert row.response == request["response"]


async def test_login_failure_releases_browser_and_profile(settings, monkeypatch):
    """导航失败关闭浏览器并释放资料，登录动作从不填充或发送内容。"""
    context = SimpleNamespace(
        pages=[SimpleNamespace(goto=AsyncMock(side_effect=RuntimeError("failed")))],
        close=AsyncMock(),
    )
    runtime = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context)),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(
        auxiliary,
        "async_playwright",
        lambda: SimpleNamespace(start=AsyncMock(return_value=runtime)),
    )
    desk = auxiliary.AuxiliaryWorkbench(None)
    with pytest.raises(RuntimeError):
        await desk.open_login(entry())
    context.close.assert_awaited_once()
    runtime.stop.assert_awaited_once()
    assert desk.login_id is None
    with profile_lease(entry().browser_config().persistent_profile_dir):
        pass


async def test_manual_observations_excluded_from_model_profile_query():
    """查询层排除人工观察，不能将人工粘贴计为模型成功调用。"""
    from autodidact.learning_state import LearningState

    captured = []

    async def capture(query):
        """只截获第一条SQL，拒绝继续任何数据库操作。"""
        captured.append(query)
        raise RuntimeError("captured")

    state = LearningState()
    state.s = SimpleNamespace(scalars=capture)
    with pytest.raises(RuntimeError, match="captured"):
        await state.refresh_model_profiles()
    sql = str(captured[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "model_observations.access_type != 'web_manual'" in sql


async def test_manual_observations_excluded_from_error_dashboard():
    """运营错误统计同样只算真实调用记录，不把人工观察当成成功调用。"""
    from autodidact.experiment_dashboard import ExperimentDashboard, ExperimentQuery

    captured = []

    async def capture(query):
        """截获汇总SQL并在访问数据库前结束。"""
        captured.append(query)
        raise RuntimeError("captured")

    dashboard = ExperimentDashboard(SimpleNamespace(s=SimpleNamespace(execute=capture)), None, ExperimentQuery())
    with pytest.raises(RuntimeError, match="captured"):
        await dashboard.errors()
    sql = str(captured[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "model_observations.access_type != 'web_manual'" in sql
