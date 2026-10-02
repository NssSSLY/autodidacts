# 文件职责：创建异步 PostgreSQL engine/会话工厂，并经 Alembic 初始化数据库。
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from autodidact.config import runtime_settings


class Base(DeclarativeBase):
    pass


settings = runtime_settings()
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# 功能：产出一个异步会话并在退出时关闭；不隐含自动提交业务事务。
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


# 功能：调用迁移管理升级数据库到当前 HEAD，不使用 create_all 覆盖已有学习状态。
async def init_database() -> None:
    from autodidact.migrations import upgrade_database

    await upgrade_database(engine)
