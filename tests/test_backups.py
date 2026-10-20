# 文件职责：离线验证一致快照接线、新路径/新空库保护、摘要校验、无密码argv及子进程取消，不连接实际数据库。
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from autodidact import backups
from autodidact.delivery import DeliveryError, file_digest
from autodidact.migrations import HEAD_REVISION, expected_schema_columns

SOURCE = "postgresql+asyncpg://owner:private-password@localhost:5432/autodidact"
TARGET = "postgresql+asyncpg://owner:target-password@localhost:5432/autodidact_restore"


class Connection:
    # 功能：模拟只读快照/空库计数，记录SQL供验证，不执行数据库写入。
    def __init__(self, nonempty=False):
        self.sql, self.nonempty = [], nonempty

    # 功能：提供测试连接和事务上下文。
    async def __aenter__(self):
        return self

    # 功能：退出连接/事务，不假装验证真实回滚。
    async def __aexit__(self, *args):
        return None

    # 功能：开启替身事务。
    def begin(self):
        return self

    # 功能：记录只读/UTC语句配置。
    async def execute(self, query):
        self.sql.append(str(query))

    # 功能：返回已知快照/版本/空库信息，拒绝未预期SQL。
    async def scalar(self, query):
        sql = str(query)
        self.sql.append(sql)
        if "pg_export_snapshot" in sql:
            return "00000001-00000100-1"
        if "server_version_num" in sql:
            return "160000"
        if "current_schema" in sql:
            return "public"
        if "version_num" in sql:
            return HEAD_REVISION
        if "pg_class" in sql:
            return int(self.nonempty)
        raise AssertionError(sql)

    # 功能：健康查询仅返回当前业务表集合。
    async def scalars(self, query):
        if "FROM alembic_version" in str(query):
            return SimpleNamespace(all=lambda: [HEAD_REVISION])
        return SimpleNamespace(all=lambda: [*expected_schema_columns(), "alembic_version"])

    # 功能：提供有界流式记录，核验规范JSON摘要而不缓存整库。
    async def stream(self, query):
        self.sql.append(str(query))

        # 功能：模拟每表一条规范JSON记录。
        async def rows():
            yield ('{"id":"frozen-old-id","statement":"旧结论"}',)

        return rows()


class Engine:
    # 功能：保存显式假URL，不建立网络连接。
    def __init__(self, nonempty=False, url=SOURCE):
        self.url, self.connection, self.disposed = make_url(url), Connection(nonempty), False

    # 功能：返回替身连接上下文。
    def connect(self):
        return self.connection

    # 功能：记录目标引擎已释放。
    async def dispose(self):
        self.disposed = True


# 功能：离线锁替身只验证备份流程顺序，真实互斥另用隔离数据库验收。
@asynccontextmanager
async def unlocked(engine):
    yield object()


# 功能：安装无数据库锁与原生客户端替身，记录参数和环境，不实际启动pg工具。
@pytest.fixture
def offline(monkeypatch):
    calls = []

    # 功能：生成一个带PGDMP头的合成归档，仅供文件/清单接线测试。
    async def process(name, args, env, *, output=None, timeout=600):
        calls.append((name, args, env))
        if output is not None:
            output.write(b"PGDMP-synthetic-not-real-database")

    monkeypatch.setattr(backups, "service_lease", unlocked)
    monkeypatch.setattr(backups, "controller_lock", unlocked)
    monkeypatch.setattr(backups, "pg_process", process)
    return calls


# 功能：一致快照token明确传给pg_dump，规范摘要及行数保留，旧文件绝不覆盖。
async def test_backup_snapshot_manifest_and_no_overwrite(offline, tmp_path):
    engine, path = Engine(), tmp_path / "backup.dump"
    result = await backups.backup_database(engine, path)
    payload = backups.verify_backup(path)
    assert result["tables"] == 20 and payload["revision"] == HEAD_REVISION
    assert payload["archive"] == file_digest(path) and all(
        v["rows"] == 1 for v in payload["tables"].values()
    )
    name, args, env = offline[0]
    assert name == "pg_dump" and "--snapshot=00000001-00000100-1" in args
    assert env["PGPASSWORD"] == "private-password" and "private-password" not in str(args)
    assert "REPEATABLE READ, READ ONLY" in engine.connection.sql[0]
    with pytest.raises(DeliveryError, match="不能覆盖"):
        await backups.backup_database(engine, path)
    assert len(offline) == 1


# 功能：失败保留残留归档而不生成成功清单；不静默删除用户文件或宣告可恢复。
async def test_failed_dump_retains_partial_without_manifest(offline, tmp_path, monkeypatch):
    # 功能：模拟部分输出后失败，不暴露具体外部错误。
    async def fail(name, args, env, *, output=None):
        output.write(b"PGDMP-partial")
        raise RuntimeError("private")

    monkeypatch.setattr(backups, "pg_process", fail)
    path = tmp_path / "partial.dump"
    with pytest.raises(RuntimeError):
        await backups.backup_database(Engine(), path)
    assert path.exists() and not path.with_name(path.name + ".manifest.json").exists()


# 功能：归档内容/协议/版本/表集合被篡改均在恢复前拒绝，无SQL执行。
@pytest.mark.parametrize(
    "damage",
    [
        "archive",
        "protocol",
        "revision",
        "tables",
        "header",
        "payload",
        "rows",
        "digest",
        "table_type",
    ],
)
async def test_backup_integrity_fails_closed(offline, tmp_path, damage):
    path = tmp_path / "x.dump"
    await backups.backup_database(Engine(), path)
    manifest = path.with_name(path.name + ".manifest.json")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if damage == "archive":
        path.write_bytes(b"changed")
    elif damage == "header":
        path.write_bytes(b"NOTPG-custom")
        payload["archive"] = file_digest(path)
    elif damage == "tables":
        payload["tables"].pop("beliefs")
    elif damage == "payload":
        payload = []
    elif damage == "rows":
        payload["tables"]["beliefs"]["rows"] = True
    elif damage == "digest":
        payload["tables"]["beliefs"]["sha256"] = "unknown"
    elif damage == "table_type":
        payload["tables"] = []
    else:
        payload[damage] = "unknown"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DeliveryError):
        backups.verify_backup(path)


# 功能：当前库、非恢复名称、未确认和连接串伪装的数据库名都不能成为恢复目标。
@pytest.mark.parametrize(
    "target,confirmed",
    [
        (SOURCE, True),
        (TARGET, False),
        ("postgresql://u:p@localhost/prod", True),
        ("postgresql://u:p@localhost/host%3Devil%20dbname%3Dx_restore", True),
    ],
)
def test_restore_target_guard(target, confirmed):
    with pytest.raises(DeliveryError):
        backups.validate_restore_target(SOURCE, target, confirmed)


# 功能：新空库恢复使用单事务/无clean，并复核20表内容；非空库/摘要不符不覆盖原库或自动清理。
@pytest.mark.parametrize("case", ["success", "nonempty", "mismatch"])
async def test_restore_new_empty_target_only(offline, tmp_path, monkeypatch, case):
    path = tmp_path / "x.dump"
    await backups.backup_database(Engine(), path)
    target = Engine(case == "nonempty", url=TARGET)
    monkeypatch.setattr(backups, "create_async_engine", lambda *a, **k: target)
    if case == "mismatch":
        # 功能：模拟恢复后内容不同，拒绝把行数相同当认知保留成功。
        async def mismatch(connection):
            return {}

        monkeypatch.setattr(backups, "database_inventory", mismatch)
    if case != "success":
        with pytest.raises(DeliveryError):
            await backups.restore_database(SOURCE, TARGET, path, confirmed=True)
    else:
        assert (await backups.restore_database(SOURCE, TARGET, path, confirmed=True))["verified"]
    assert target.disposed
    restores = [c for c in offline if c[0] == "pg_restore"]
    assert len(restores) == int(case != "nonempty")
    if restores:
        _, args, env = restores[0]
        assert "--single-transaction" in args and "--clean" not in args and "--create" not in args
        assert env["PGPASSWORD"] == "target-password" and "target-password" not in str(args)


# 功能：子进程超时会被终止/等待，不让pg_dump在退出锁后继续运行；未使用shell。
async def test_pg_process_cancel_kills_only_owned_child(monkeypatch):
    calls = []

    class Process:
        returncode = None

        # 功能：模拟直到终止才退出的客户端。
        async def wait(self):
            if self.returncode is None:
                await asyncio.sleep(10)
            return self.returncode

        # 功能：仅终止本测试启动的客户端。
        def terminate(self):
            self.returncode = 143

        # 功能：模拟必要时强制终止同一子进程。
        def kill(self):
            self.returncode = 137

    process = Process()

    # 功能：检查无shell参数且记录argv/env分离，返回受控子进程。
    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(backups.shutil, "which", lambda name: "trusted-pg-dump")
    monkeypatch.setattr(backups.asyncio, "create_subprocess_exec", create)
    with pytest.raises(TimeoutError):
        await backups.pg_process(
            "pg_dump", ["--no-password"], {"PGPASSWORD": "private"}, timeout=0.01
        )
    assert (
        process.returncode == 143
        and "shell" not in calls[0][1]
        and "private" not in str(calls[0][0])
    )


# 功能：客户端环境不继承LLM密钥/PGOPTIONS等旁路，SSL或不支持query不能默默连不同配置。
def test_client_environment_is_narrow(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "private")
    monkeypatch.setenv("PGOPTIONS", "untrusted")
    _, env = backups.pg_environment(SOURCE)
    assert "LLM_API_KEY" not in env and "PGOPTIONS" not in env
    for url in (SOURCE + "?options=unsafe", SOURCE + "?sslmode=unknown"):
        with pytest.raises(DeliveryError):
            backups.pg_environment(url)
