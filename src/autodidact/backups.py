# 文件职责：原生PostgreSQL一致快照备份、摘要清单及仅新空库恢复验证；不覆盖已有库、不在argv暴露密码。
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from autodidact.delivery import DeliveryError, file_digest
from autodidact.migrations import HEAD_REVISION, expected_schema_columns
from autodidact.runtime import controller_lock
from autodidact.service_runtime import database_readiness, service_lease


# 功能：限制原生客户端连接参数，避免dbname被误作连接串覆盖主机/密码；未知query拒绝猜测。
def pg_environment(url):
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql") or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]{0,62}", parsed.database or ""
    ):
        raise DeliveryError("仅支持PostgreSQL及ASCII普通数据库名")
    if set(parsed.query) - {"ssl", "sslmode"}:
        raise DeliveryError("备份连接仅支持普通连接字段及ssl/sslmode，不猜测其它query参数")
    mode = parsed.query.get("sslmode", parsed.query.get("ssl", "prefer"))
    mode = {"true": "require", "false": "disable"}.get(mode, mode)
    if mode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise DeliveryError("未知PostgreSQL SSL模式")
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "LANG"}
    }
    env.update(
        PGHOST=parsed.host or "localhost",
        PGPORT=str(parsed.port or 5432),
        PGUSER=parsed.username or "postgres",
        PGDATABASE=parsed.database,
        PGPASSWORD=parsed.password or "",
        PGSSLMODE=mode,
        PGCONNECT_TIMEOUT="10",
    )
    return parsed, env


# 功能：不允许恢复到当前配置库，且只接收显式*_restore/*_test新空库；真正空库检查另在数据库执行。
def validate_restore_target(source_url, target_url, confirmed):
    if not confirmed:
        raise DeliveryError("恢复须明确--confirm-empty-target，只能导入可信备份到新空库")
    target, _ = pg_environment(target_url)
    source = make_url(source_url)
    if not target.database.endswith(("_restore", "_test")):
        raise DeliveryError("恢复目标名称必须以_restore或_test结尾")
    if (target.host, target.port or 5432, target.database) == (
        source.host,
        source.port or 5432,
        source.database,
    ):
        raise DeliveryError("禁止恢复到当前配置学习库")
    return target


# 功能：启动固定PostgreSQL客户端，密码仅短期子进程环境；失败/超时不暴露stderr，并终止本次子进程。
async def pg_process(name, args, env, *, output=None, timeout=600):
    tool = shutil.which(name)
    if not tool:
        raise DeliveryError(f"未找到{name}，请安装PostgreSQL客户端并配置PATH")
    process = await asyncio.create_subprocess_exec(
        tool,
        *args,
        env=env,
        stdout=output if output is not None else asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        async with asyncio.timeout(timeout):
            code = await process.wait()
        if code:
            raise DeliveryError(
                f"{name}失败(exit={code})；保留现场，先检查权限/版本/连接，不自动重试"
            )
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()


# 功能：在同一只读快照下按主键流式核对20个业务表的行数/规范JSON摘要，不读取整库到内存。
async def database_inventory(connection):
    readiness = await database_readiness(connection)
    if not readiness["ready"]:
        raise DeliveryError("数据库版本/表不匹配，不执行备份或自动迁移")
    inventory = {}
    for name in sorted(expected_schema_columns()):
        digest, count = hashlib.sha256(), 0
        result = await connection.stream(
            text(f'SELECT to_jsonb(t)::text FROM "{name}" t ORDER BY id')
        )
        async for row in result:
            raw = row[0].encode("utf-8")
            digest.update(len(raw).to_bytes(8, "big"))
            digest.update(raw)
            count += 1
        inventory[name] = {"rows": count, "sha256": digest.hexdigest()}
    return inventory


# 功能：独占新备份路径；导出快照供pg_dump复用，保留旧认知/历史，失败残留文件不冒充完整备份。
async def backup_database(engine, output):
    path = Path(output).resolve()
    manifest = path.with_name(path.name + ".manifest.json")
    if path.exists() or manifest.exists() or not path.parent.is_dir():
        raise DeliveryError("请选择已存在目录中的新备份文件名，不能覆盖旧文件")
    _, env = pg_environment(engine.url)
    async with (
        service_lease(engine),
        controller_lock(engine),
        engine.connect() as connection,
        connection.begin(),
    ):
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        await connection.execute(text("SET LOCAL timezone='UTC'"))
        await connection.execute(text("SET LOCAL statement_timeout='60000ms'"))
        if await connection.scalar(text("SELECT current_schema()")) != "public":
            raise DeliveryError("当前原生备份最小范围仅支持专用public schema数据库")
        snapshot = await connection.scalar(text("SELECT pg_export_snapshot()"))
        tables = await database_inventory(connection)
        version = await connection.scalar(text("SHOW server_version_num"))
        with path.open("xb") as stream:
            await pg_process(
                "pg_dump",
                ["--format=custom", "--no-password", "--no-acl", f"--snapshot={snapshot}"],
                env,
                output=stream,
            )
    payload = {
        "protocol": "autodidact_backup_v1",
        "revision": HEAD_REVISION,
        "server_version_num": str(version),
        "created_at": datetime.now(UTC).isoformat(),
        "archive": file_digest(path),
        "tables": tables,
        "note": "含完整私有认知数据，须自行加密/离线保存；SHA256不认证备份来源。",
    }
    with manifest.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return {
        "backup": str(path),
        "manifest": str(manifest),
        "revision": HEAD_REVISION,
        "tables": len(tables),
    }


# 功能：先检查有界清单/协议/业务表集合和归档摘要，再允许任何恢复写入；只接受当前HEAD。
def verify_backup(path):
    path = Path(path).resolve()
    with path.with_name(path.name + ".manifest.json").open("rb") as stream:
        raw = stream.read(1048577)
    if len(raw) > 1048576:
        raise DeliveryError("备份清单超过1MiB")
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != "autodidact_backup_v1"
        or payload.get("revision") != HEAD_REVISION
        or not isinstance(payload.get("tables"), dict)
        or set(payload.get("tables", {})) != set(expected_schema_columns())
    ):
        raise DeliveryError("未知备份协议、版本或业务表集合，不自动升级恢复")
    for signature in payload["tables"].values():
        if (
            not isinstance(signature, dict)
            or set(signature) != {"rows", "sha256"}
            or type(signature["rows"]) is not int
            or signature["rows"] < 0
            or not isinstance(signature["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", signature["sha256"])
        ):
            raise DeliveryError("备份业务表摘要格式不合法，不执行恢复")
    if payload.get("archive") != file_digest(path):
        raise DeliveryError("备份文件摘要或大小不一致")
    with path.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise DeliveryError("需要可信PostgreSQL custom归档")
    return payload


# 功能：在新目标库持计划/控制器锁并拒绝所有非系统既有关系；不用clean/create/drop覆盖数据库。
async def restore_database(source_url, target_url, path, *, confirmed=False):
    target = validate_restore_target(source_url, target_url, confirmed)
    payload = verify_backup(path)
    _, env = pg_environment(target)
    query = dict(target.query)
    if "sslmode" in query:
        query["ssl"] = query.pop("sslmode")
    target_engine = create_async_engine(
        target.set(drivername="postgresql+asyncpg", query=query), pool_pre_ping=True
    )
    try:
        async with service_lease(target_engine), controller_lock(target_engine):
            async with target_engine.connect() as connection:
                if await connection.scalar(text("SELECT current_schema()")) != "public":
                    raise DeliveryError("恢复目标必须是专用public schema新空库")
                count = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
                        "AND c.relkind IN ('r','p','v','m','S','f')"
                    )
                )
                if count:
                    raise DeliveryError("目标库非空，拒绝覆盖；请另建空_restore库")
            await pg_process(
                "pg_restore",
                [
                    "--no-password",
                    "--single-transaction",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "--dbname",
                    target.database,
                    str(Path(path).resolve()),
                ],
                env,
            )
            async with target_engine.connect() as connection, connection.begin():
                await connection.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
                await connection.execute(text("SET LOCAL timezone='UTC'"))
                await connection.execute(text("SET LOCAL statement_timeout='60000ms'"))
                actual = await database_inventory(connection)
            if actual != payload["tables"]:
                raise DeliveryError(
                    "恢复后行数/内容摘要不一致；原库未改，不自动清理目标，请保留现场检查"
                )
        return {
            "verified": True,
            "revision": HEAD_REVISION,
            "tables": len(actual),
            "note": "新副本版本/行数/内容摘要一致，不自动切换DATABASE_URL或开始学习。",
        }
    finally:
        await target_engine.dispose()
