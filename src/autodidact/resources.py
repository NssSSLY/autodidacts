from pathlib import Path


def resource_root():
    source = Path(__file__).resolve().parents[2]
    if (source / "alembic.ini").is_file() and (source / "migrations").is_dir():
        return source
    bundled = Path(__file__).resolve().parent / "_resources"
    if not (bundled / "alembic.ini").is_file():
        raise RuntimeError("安装包缺少迁移资源，请保留完整源码或重新构建安装包")
    return bundled
