# 文件职责：离线量化四项扩展性风险，执行真实业务方法与内存SQL，保留原始样本、源码摘要和验证边界。
"""Synthetic measurements; timings are not PostgreSQL or production latency."""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import math
import os
import sqlite3
import statistics
import sys
import time
import tracemalloc
from array import array
from contextlib import asynccontextmanager
from copy import copy
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import UUID


# 功能：在导入业务模块前禁止业务网络/子进程及真实.env，模型保持mock且不创建实际数据库连接。
def offline_environment():
    os.environ.update(
        DATABASE_URL="postgresql+asyncpg://offline:offline@127.0.0.1:1/offline_test",
        LLM_PROVIDER="mock",
        EMBEDDING_PROVIDER="disabled",
    )

    # 功能：阻止外部网络和子进程，仅允许Windows事件循环内部socketpair唤醒。
    def audit(event, args):
        if event not in {
            "socket.bind",
            "socket.connect",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyaddr",
            "socket.sendto",
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
        }:
            return
        frame = sys._getframe(1)
        while frame:
            if frame.f_globals.get("__name__") == "socket" and frame.f_code.co_name in {
                "socketpair",
                "_fallback_socketpair",
            }:
                return
            frame = frame.f_back
        raise RuntimeError(f"Offline measurement blocked: {event}")

    sys.addaudithook(audit)
    from autodidact.config import RuntimeSettings

    RuntimeSettings.model_config["env_file"] = None


# 功能：生成独立的定长文本，避免重复字符串共享掩盖历史正文的内存成本。
def blob(index, size):
    return f"{index:010d}:" + "x" * max(0, size - 11)


# 功能：提供无数据库结果接口；可统计实际被去重循环访问的行数。
class Rows:
    # 功能：绑定已物化行和可选访问计数。
    def __init__(self, rows, counters=None):
        self.rows, self.counters = rows, counters

    # 功能：返回完整物化集合。
    def all(self):
        return self.rows

    # 功能：逐行返回并计数，不替代业务文本规范化逻辑。
    def scalars(self):
        for row in self.rows:
            if self.counters is not None:
                self.counters["visited"] += 1
            yield row

    # 功能：为无关统计查询返回合成总量。
    def scalar_one(self):
        return self.rows


class HistorySession:
    # 功能：按需构建合成历史，观测真实统计函数的加载量与逐评估目标读取次数。
    def __init__(self, count, payload_bytes):
        self.n, self.width = count, payload_bytes
        self.counts = {
            "loaded_observations": 0,
            "loaded_evaluations": 0,
            "loaded_reports": 0,
            "goal_gets": 0,
            "payload_bytes": 0,
        }

    # 功能：响应实际SELECT的实体类型，每次读取新建正文对象，计入测量窗口。
    async def scalars(self, statement):
        from autodidact import models

        model = statement.column_descriptions[0]["entity"]
        if model is models.ModelObservation:
            self.counts["loaded_observations"] += self.n
            self.counts["payload_bytes"] += self.n * self.width * 2
            return Rows(
                [
                    NS(
                        provider="offline",
                        displayed_model="synthetic",
                        metadata_json={},
                        prompt=blob(i, self.width),
                        response=blob(i, self.width),
                    )
                    for i in range(self.n)
                ]
            )
        if model is models.Evaluation:
            self.counts["loaded_evaluations"] += self.n
            self.counts["payload_bytes"] += self.n * self.width
            return Rows(
                [
                    NS(
                        goal_id=UUID(int=i + 1),
                        score=0.5,
                        components={
                            "audit": {
                                "protocol": "closed_book_v1",
                                "learner_provider": "offline",
                                "learner_model": "synthetic",
                            },
                            "answers": blob(i, self.width),
                        },
                    )
                    for i in range(self.n)
                ]
            )
        if model is models.ResearchReport and any(
            isinstance(v, list) and "benchmark_run" in v
            for v in statement.compile().params.values()
        ):
            self.counts["loaded_reports"] += self.n
            self.counts["payload_bytes"] += self.n * self.width
            return Rows(
                [
                    NS(
                        id=UUID(int=i + 1),
                        kind="benchmark_run",
                        payload={
                            "provider": "offline",
                            "model": "synthetic",
                            "score": 0.5,
                            "complete": True,
                            "comparison_key": "fixed-condition",
                            "details": [
                                {"score": 0.5, "category": "domain", "answer": blob(i, self.width)}
                            ],
                        },
                    )
                    for i in range(self.n)
                ]
            )
        return Rows([])

    # 功能：统计目标访问次数，不模拟数据库或ORM缓存的实际时延。
    async def get(self, model, key):
        self.counts["goal_gets"] += 1
        return NS(title="domain")

    # 功能：统计查询返回固定总量，派生画像查询按无记录处理。
    async def scalar(self, statement):
        expr = statement.column_descriptions[0]["expr"]
        return 0 if getattr(expr, "name", None) == "count" else None

    # 功能：为仪表盘COUNT提供结果，不测量数据库聚合成本。
    async def execute(self, statement):
        return Rows(self.n)

    # 功能：接受临时派生画像，不写入外部存储。
    def add(self, row):
        pass

    # 功能：结束合成统计调用，不产生真实提交。
    async def commit(self):
        pass


# 功能：执行真实画像或仪表盘函数，返回实际物化量，不将加载字节数视为网络传输量。
async def history_case(count, width, kind):
    from autodidact.learning_state import LearningState
    from autodidact.metrics import dashboard

    session = HistorySession(count, width)
    if kind == "profiles":
        await LearningState.refresh_model_profiles(NS(s=session))
        assert session.counts["goal_gets"] == count
        assert session.counts["loaded_reports"] == count
    else:
        await dashboard(session)
        assert session.counts["loaded_evaluations"] == count
    return session.counts


class BeliefSession:
    # 功能：物化N条宽信念记录，并统计去重循环访问；1536维数组模拟载入的float32向量。
    def __init__(self, count, width, position):
        self.n, self.width, self.position = count, width, position
        self.claim_id = UUID(int=count + 100)
        self.counts = {"materialized": 0, "visited": 0, "vector_bytes": count * 1536 * 4}
        self.added = []

    # 功能：SELECT一次载入完整宽行；派生索引INSERT只记录，不连接数据库。
    async def execute(self, statement):
        if not getattr(statement, "is_select", False):
            return Rows([])
        rows = [
            NS(
                id=UUID(int=i + 1),
                statement=f"record {i}",
                status="verified",
                confidence=0.9,
                lifecycle_state="active",
                embedding=array("f", [0.0]) * 1536,
                metadata_json={
                    "promoted_claim_ids": [str(self.claim_id)],
                    "body": blob(i, self.width),
                },
            )
            for i in range(self.n)
        ]
        self.counts["materialized"] += len(rows)
        return Rows(rows, self.counts)

    # 功能：暂存新信念或历史，仅用于完整执行生产晋升方法。
    def add(self, row):
        self.added.append(row)

    # 功能：为新合成实体补ID，不访问数据库序列。
    async def flush(self):
        for row in self.added:
            if row.id is None:
                row.id = UUID(int=self.n + 200 + self.added.index(row))

    # 功能：不执行真实提交。
    async def commit(self):
        pass


# 功能：调用真实晋升方法，测量队首命中和全表未命中两种去重路径。
async def belief_case(count, width, position):
    from autodidact.repository import Repository

    session = BeliefSession(count, width, position)
    claim = NS(
        id=session.claim_id,
        statement="record 0" if position == "first" else "new record",
        structure={},
        confidence=0.9,
        topic="domain",
        reasoning="",
        source_ids=[],
    )
    await Repository(session).create_belief_from_claim(claim, 0.9, False)
    assert session.counts["materialized"] == count
    assert session.counts["visited"] == (1 if position == "first" else count)
    return session.counts


class FallbackSession:
    # 功能：关键词索引预置一个命中，真实执行由混合检索构建的模糊SQL投影，测量SQLite扫描成本。
    def __init__(self, db, hit, rows):
        self.db, self.hit, self.n = db, hit, rows
        self.steps = 0
        self.counts = {"index_hits": 0, "fallback_queries": 0, "fallback_vm_steps": 0}
        self.plan = []

    # 功能：按千条SQLite虚拟机指令计数；不代表PostgreSQL buffer或实际访问行数。
    def progress(self):
        self.steps += 1000
        return 0

    # 功能：关键词分支固定返回命中，随后实际模糊查询保留原WHERE/排序/LIMIT。
    async def scalars(self, statement):
        from sqlalchemy.dialects import sqlite

        from autodidact import models

        if "to_tsvector" in str(statement):
            self.counts["index_hits"] += 1
            return Rows([self.hit])
        query = statement.with_only_columns(models.Belief.id)
        sql = str(query.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        self.plan = [row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + sql)]
        self.db.set_progress_handler(self.progress, 1000)
        try:
            ids = self.db.execute(sql).fetchall()
        finally:
            self.db.set_progress_handler(None, 0)
        self.counts["fallback_queries"] += 1
        self.counts["fallback_vm_steps"] = self.steps
        return Rows([self.hit] if ids else [])


# 功能：在预建的内存表上调用真实检索；关键词已命中，观察无条件模糊回退仍执行。
async def fallback_case(db, count, terms):
    from autodidact.retrieval import HybridRetriever

    hit = NS(id=UUID(int=1), statement="needle0", topic="domain", explanation="")
    session = FallbackSession(db, hit, count)
    hits = await HybridRetriever(NS(s=session)).retrieve(
        " ".join(f"needle{i}" for i in range(terms)), kinds=("belief",), limit=5
    )
    assert hits and session.counts["index_hits"] == session.counts["fallback_queries"] == 1
    return {**session.counts, "query_plan": session.plan}


class IndexStore:
    # 功能：为真实批次控制器提供常量开销的内存持久层替身，统计校验访问和关键词同步请求。
    def __init__(self, count):
        from autodidact.retrieval_jobs import PROTOCOL

        self.payload = {
            "protocol": PROTOCOL,
            "fingerprint": "offline",
            "max_attempts": 3,
            "status": "queued",
            "sealed": False,
        }
        self.entries = [
            NS(
                id=i,
                entity_id=UUID(int=i + 1),
                entity_kind="belief",
                status="pending",
                attempts=0,
                content_hash=None,
                embedding=None,
                fingerprint=None,
            )
            for i in range(count)
        ]
        self.rows = [
            NS(id=UUID(int=i + 1), statement=f"record {i}", topic="domain") for i in range(count)
        ]
        self.counts = {
            "verify_visits": 0,
            "completed_verify_visits": 0,
            "keyword_sync_requests": 0,
            "repeated_keyword_sync_requests": 0,
            "populate_calls": 0,
        }
        self.pending = 0

    # 功能：返回独立的任务状态字典，不复制全体工作项制造额外开销。
    async def load(self, key):
        return dict(self.payload)

    # 功能：仅编辑任务状态，事务成本未模拟。
    @asynccontextmanager
    async def edit(self, key):
        yield self.payload

    # 功能：所有实体已预置，记录生产代码每批仍请求补齐候选的次数。
    async def populate(self, key):
        self.counts["populate_calls"] += 1

    # 功能：本场景无崩溃项，不引入恢复扫描的额外成本。
    async def recover(self, *args):
        pass

    # 功能：每次校验逐行读取当前候选，向量校验和摘要计算仍由生产代码执行。
    async def items(self, key):
        for item, row in zip(self.entries, self.rows, strict=True):
            self.counts["verify_visits"] += 1
            self.counts["completed_verify_visits"] += item.status == "completed"
            yield copy(item), row

    # 功能：记录同步请求并更新候选；不把请求计数误称为真实数据库写入量。
    async def mark(self, item_id, *, row=None, **values):
        item = self.entries[item_id]
        if row is not None:
            self.counts["keyword_sync_requests"] += 1
            self.counts["repeated_keyword_sync_requests"] += not values
        for key, value in values.items():
            setattr(item, key, value)

    # 功能：按游标给出待处理项，避免替身自身线性搜索污染验证次数测量。
    async def next_item(self, key):
        if self.pending == len(self.entries):
            return None
        index = self.pending
        self.pending += 1
        return copy(self.entries[index]), self.rows[index]

    # 功能：本场景无缺失实体，不模拟真实数据库覆盖查询成本。
    async def missing_entities(self, key):
        return 0

    # 功能：本场景预置物理索引存在，不执行HNSW/GIN。
    async def indexes_present(self):
        return True

    # 功能：预置关键词完整，其他重复成本仍统计。
    async def keyword_gaps(self, key):
        return 0

    # 功能：聚合内存状态，不计入verify_visits。
    async def stats(self, key):
        counts = dict.fromkeys(["pending", "running", "completed", "failed", "skipped"], 0)
        for item in self.entries:
            counts[item.status] += 1
        return {"counts": counts}

    # 功能：返回简单状态，不运行额外的全量导出。
    async def status(self, key):
        return dict(self.payload)


# 功能：真实执行全套有限批次，统计锁内合成执行时间、验证次数和重复索引同步请求。
async def index_case(count, batch):
    from autodidact import retrieval_jobs

    store = IndexStore(count)
    lock_times = []

    # 功能：替换数据库控制器锁但测量真实临界区代码耗时，不测并发竞争等待。
    @asynccontextmanager
    async def lock(engine):
        start = time.perf_counter()
        try:
            yield
        finally:
            lock_times.append((time.perf_counter() - start) * 1000)

    # 功能：返回固定非零1536维向量，不访问嵌入API。
    async def embed(text):
        return [1.0] + [0.0] * 1535

    saved = retrieval_jobs.controller_lock
    retrieval_jobs.controller_lock = lock
    try:
        job = retrieval_jobs.RetrievalJobs(None, NS(fingerprint="offline", embed=embed), store)
        for _ in range(math.ceil(count / batch)):
            store.payload["status"] = "queued"
            await job.run(UUID(int=1), batch)
    finally:
        retrieval_jobs.controller_lock = saved
    batches = math.ceil(count / batch)
    assert store.payload["status"] == "ready"
    assert store.counts["verify_visits"] == count * (batches + 1)
    expected_resyncs = sum(min(i * batch, count) for i in range(batches))
    assert store.counts["repeated_keyword_sync_requests"] == expected_resyncs
    return {
        **store.counts,
        "batches": batches,
        "lock_region_ms": sum(lock_times),
        "max_batch_lock_region_ms": max(lock_times),
    }


# 功能：分别计时和测峰值分配，保存全部时间样本；峰值只包括本Python调用，不含数据库/RSS。
async def measure(factory, repeats, memory=True):
    samples, counts = [], None
    await factory()
    for _ in range(repeats):
        gc.collect()
        start = time.perf_counter()
        counts = await factory()
        samples.append((time.perf_counter() - start) * 1000)
    peak = None
    if memory:
        gc.collect()
        tracemalloc.start()
        try:
            await factory()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    return {
        "elapsed_ms_samples": samples,
        "median_ms": statistics.median(samples),
        "peak_python_bytes": peak,
        "counts": counts,
    }


# 功能：验证数据量参数并按四种风险运行受限合成负载，输出JSON证据。
async def run(args):
    from sqlalchemy import __version__ as sqlalchemy_version

    results = []
    for n in args.sizes:
        for kind in ("profiles", "dashboard"):
            result = await measure(partial(history_case, n, args.payload_bytes, kind), args.repeats)
            results.append(dict(risk="history", scenario=kind, rows_per_table=n, **result))
            print(f"history/{kind} n={n}: {result['median_ms']:.2f} ms", flush=True)
        for position in ("first", "new"):
            result = await measure(
                partial(belief_case, n, args.payload_bytes, position), args.repeats
            )
            results.append(dict(risk="dedup", scenario=position, rows=n, **result))
            print(f"dedup/{position} n={n}: {result['median_ms']:.2f} ms", flush=True)
        db = sqlite3.connect(":memory:")
        try:
            db.execute(
                "CREATE TABLE beliefs (id TEXT, statement TEXT, topic TEXT, "
                "status TEXT, lifecycle_state TEXT, created_at TEXT)"
            )
            db.executemany(
                "INSERT INTO beliefs VALUES (?, ?, ?, 'verified', 'active', '2026-01-01')",
                (
                    (str(UUID(int=i + 1)), "needle0" if i == 0 else f"record {i}", "domain")
                    for i in range(n)
                ),
            )
            for terms in (1, 8):
                result = await measure(partial(fallback_case, db, n, terms), args.repeats)
                results.append(dict(risk="fallback", rows=n, terms=terms, **result))
                print(f"fallback/{terms} terms n={n}: {result['median_ms']:.2f} ms", flush=True)
        finally:
            db.close()
    for n in args.index_sizes:
        for batch in args.batch_sizes:
            result = await measure(partial(index_case, n, batch), args.repeats, memory=False)
            results.append(dict(risk="index_batches", rows=n, batch_size=batch, **result))
            print(f"index n={n} batch={batch}: {result['median_ms']:.2f} ms", flush=True)
    root = Path(__file__).resolve().parents[1]
    sources = [
        "scripts/measure_scalability.py",
        "src/autodidact/learning_state.py",
        "src/autodidact/metrics.py",
        "src/autodidact/repository.py",
        "src/autodidact/retrieval.py",
        "src/autodidact/retrieval_jobs.py",
    ]
    report = {
        "protocol": "offline_scalability_v1",
        "created_at_utc": datetime.now(UTC).isoformat(),
        "source_revision": args.revision,
        "python": sys.version,
        "platform": sys.platform,
        "sqlite_version": sqlite3.sqlite_version,
        "sqlalchemy_version": sqlalchemy_version,
        "configuration": vars(args) | {"output": str(args.output)},
        "methodology": {
            "warmup_runs_per_scenario": 1,
            "timing_repeats": args.repeats,
            "separate_memory_runs": "one per history/dedup/fallback scenario; none for index batches",
            "timing_clock": "perf_counter wall time; memory tracing disabled during timing",
        },
        "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
        "limitations": [
            "Synthetic Python object materialization, not actual ORM/driver latency.",
            "LIKE executes on in-memory SQLite; keyword index hit is a fixture.",
            "Index store and embedding are fixtures; vector validation and controller code are real.",
            "Lock regions do not measure real locks or contention; sync requests are not DB writes.",
            "Peak Python allocation excludes DB memory, RSS and prebuilt SQLite data.",
            "No PostgreSQL, production database, model API or business networking.",
        ],
        "validation": "All per-scenario operation-count assertions passed.",
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# 功能：限制规模与总向量检查量，默认只运行本机可控的离线合成测量。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 5000, 20000])
    parser.add_argument("--index-sizes", type=int, nargs="+", default=[128, 512])
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[16, 128])
    parser.add_argument("--payload-bytes", type=int, default=512)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--revision", default="unrecorded")
    parser.add_argument("--output", type=Path, default=Path("build/scalability/report.json"))
    args = parser.parse_args()
    if not (
        1 <= args.repeats <= 7
        and 32 <= args.payload_bytes <= 4096
        and all(1 <= n <= 50000 for n in args.sizes)
        and all(1 <= n <= 2048 for n in args.index_sizes)
        and all(1 <= n <= 10000 for n in args.batch_sizes)
    ):
        parser.error("Synthetic workload exceeds supported bounds")
    checks = (
        sum(n * (math.ceil(n / b) + 2) * 1536 for n in args.index_sizes for b in args.batch_sizes)
        * args.repeats
    )
    if checks > 200_000_000:
        parser.error("Vector-check budget exceeds 200 million coordinates")
    offline_environment()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
