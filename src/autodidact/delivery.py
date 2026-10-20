# 文件职责：生成/核验EXE文件夹的版本、依赖和文件SHA256清单，不打包用户配置或承诺逐字节确定构建。
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
from importlib.metadata import distributions
from pathlib import Path


class DeliveryError(ValueError):
    """可安全展示的交付操作拒绝，外部异常仅展示类别。"""


# 功能：分块计算文件摘要和大小，不把大型EXE/备份一次读入内存。
def file_digest(path):
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
            size += len(block)
    return {"sha256": digest.hexdigest(), "size": size}


# 功能：只记录包名/精确版本，不使用可能暴露本地路径/私有仓库凭据的pip freeze原始输出。
def installed_versions():
    versions = {}
    for package in distributions():
        name = package.metadata.get("Name", "")
        if name.lower().replace("_", "-") == "autodidact-agent":
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not re.fullmatch(
            r"[A-Za-z0-9_.+!-]+", package.version
        ):
            raise DeliveryError("依赖名称/版本不适合生成锁定清单")
        versions[name] = package.version
    return dict(sorted(versions.items(), key=lambda item: item[0].casefold()))


# 功能：列出分发树文件，拒绝链接/目录跳转及用户配置/备份/浏览器资料，防把私有环境打包。
def release_files(root):
    root = Path(root).resolve()
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root):
            raise DeliveryError("发布目录不能包含链接或目录跳转")
        relative = path.relative_to(root).as_posix()
        if (
            path.name == ".env"
            or any(
                p in {".browser", ".codex-sync", "logs", "__pycache__"}
                for p in path.relative_to(root).parts
            )
            or path.suffix.lower() in {".dump", ".backup", ".sqlite", ".db"}
        ):
            raise DeliveryError("发布目录包含用户配置/备份或运行数据")
        if path.is_file() and relative != "RELEASE_MANIFEST.json":
            result[relative] = file_digest(path)
    return result


# 功能：在新构建目录独占生成依赖锁/文件校验清单，不覆盖旧发布，不声称已签名/已验收。
def create_release(root, revision):
    root = Path(root).resolve()
    if not root.is_dir() or not (root / "Autodidact" / "Autodidact.exe").is_file():
        raise DeliveryError("需要包含Autodidact/Autodidact.exe的新package目录")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DeliveryError("发布必须记录真实40位Git提交")
    if (root / "RELEASE_MANIFEST.json").exists() or (root / "requirements-release.txt").exists():
        raise DeliveryError("此目录已生成发布清单，请使用新构建目录")
    packages = installed_versions()
    with (root / "requirements-release.txt").open("x", encoding="utf-8") as stream:
        stream.write("\n".join(f"{name}=={version}" for name, version in packages.items()) + "\n")
    payload = {
        "protocol": "autodidact_release_v1",
        "source_revision": revision,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
        "files": release_files(root),
        "acceptance": "unverified",
        "note": "仅版本/完整性清单；不是数字签名、位级复现或干净机器验收。",
    }
    with (root / "RELEASE_MANIFEST.json").open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return verify_release(root)


# 功能：有界读取清单并比较完整文件集合/摘要，缺失、增加、篡改或未知协议均拒绝。
def verify_release(root):
    root = Path(root).resolve()
    if (root / "RELEASE_MANIFEST.json").is_symlink():
        raise DeliveryError("清单不能是符号链接")
    with (root / "RELEASE_MANIFEST.json").open("rb") as stream:
        raw = stream.read(4194305)
    if len(raw) > 4194304:
        raise DeliveryError("发布清单超过4MiB")
    payload = json.loads(raw)
    if (
        not isinstance(payload, dict)
        or payload.get("protocol") != "autodidact_release_v1"
        or not isinstance(payload.get("source_revision"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", payload["source_revision"])
        or payload.get("acceptance") != "unverified"
        or not (root / "Autodidact/Autodidact.exe").is_file()
        or not (root / "requirements-release.txt").is_file()
        or payload.get("files") != release_files(root)
    ):
        raise DeliveryError("发布文件与清单不一致或协议未知")
    return {
        "verified": True,
        "source_revision": payload["source_revision"],
        "files": len(payload["files"]),
        "acceptance": payload.get("acceptance", "unverified"),
        "note": "摘要一致不是发布者身份/无恶意代码证明。",
    }


# 功能：供构建脚本生成清单，不导入数据库/用户密钥或启动学习。
def main():
    parser = argparse.ArgumentParser(description="生成Autodidact文件夹发布清单")
    parser.add_argument("root")
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    print(json.dumps(create_release(args.root, args.source_revision), ensure_ascii=False))


if __name__ == "__main__":
    main()
