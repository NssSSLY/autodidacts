# 文件职责：兼容源码、wheel 和 EXE 的迁移/配置资源路径定位。
from pathlib import Path


# 功能：优先返回包含迁移资源的源码根，否则返回安装包资源；缺失时明确报错而不创建空状态。
def resource_root():
    source = Path(__file__).resolve().parents[2]
    if (source / "alembic.ini").is_file() and (source / "migrations").is_dir():
        return source
    bundled = Path(__file__).resolve().parent / "_resources"
    if not (bundled / "alembic.ini").is_file():
        raise RuntimeError("安装包缺少迁移资源，请保留完整源码或重新构建安装包")
    return bundled
