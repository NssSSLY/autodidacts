# 文件职责：离线验证版本清单/完整目录校验、私有文件拒绝与EXE用户配置不覆盖，不构建或运行实际EXE。
import json
from pathlib import Path

import pytest

from autodidact import delivery
from scripts.autodidact_launcher import initialize_user_home


# 功能：用合成EXE文件建立完整发布树，依赖清单替身不依赖宿主环境。
def package(tmp_path, monkeypatch):
    root = tmp_path / "package"
    (root / "Autodidact").mkdir(parents=True)
    (root / "Autodidact/Autodidact.exe").write_bytes(b"synthetic executable")
    monkeypatch.setattr(delivery, "installed_versions", lambda: {"example": "1.0"})
    delivery.create_release(root, "a" * 40)
    return root


# 功能：精确版本/文件摘要可复核，清单明确unverified而非假称正式签名或验收。
def test_release_manifest_and_dependency_inventory(tmp_path, monkeypatch):
    root = package(tmp_path, monkeypatch)
    result = delivery.verify_release(root)
    assert result["verified"] and result["acceptance"] == "unverified"
    assert (root / "requirements-release.txt").read_text(encoding="utf-8") == "example==1.0\n"
    with pytest.raises(ValueError):
        delivery.create_release(root, "b" * 40)


# 功能：缺失/新增/篡改/未知协议或私有.env都不能通过发布检查。
@pytest.mark.parametrize(
    "damage",
    ["missing", "extra", "changed", "protocol", "env", "acceptance", "source_revision", "payload"],
)
def test_release_integrity_and_privacy(tmp_path, monkeypatch, damage):
    root = package(tmp_path, monkeypatch)
    if damage == "missing":
        (root / "requirements-release.txt").unlink()
    elif damage == "extra":
        (root / "new.txt").write_text("extra")
    elif damage == "changed":
        (root / "Autodidact/Autodidact.exe").write_bytes(b"different")
    elif damage == "env":
        (root / ".env").write_text("private")
    else:
        path = root / "RELEASE_MANIFEST.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if damage == "payload":
            payload = []
        else:
            payload[damage] = "unknown"
        path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        delivery.verify_release(root)


# 功能：空目录即使声明同协议/空文件集也不冒充可分发EXE。
def test_empty_release_is_not_deliverable(tmp_path):
    (tmp_path / "RELEASE_MANIFEST.json").write_text(
        json.dumps(
            {
                "protocol": "autodidact_release_v1",
                "source_revision": "a" * 40,
                "files": {},
                "acceptance": "unverified",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        delivery.verify_release(tmp_path)


# 功能：首次补默认文件，第二次启动保留已有密钥/策略/计划，缺文件可单独补齐。
def test_launcher_seeds_without_overwriting_private_files(tmp_path):
    destination = tmp_path / "user"
    assets = Path(__file__).resolve().parents[1]
    initialize_user_home(destination, assets)
    assert (destination / "config/run_plan.json").exists()
    (destination / ".env").write_text("private-user-key", encoding="utf-8")
    (destination / "config/run_plan.json").write_text("custom plan", encoding="utf-8")
    initialize_user_home(destination, assets)
    assert (destination / ".env").read_text(encoding="utf-8") == "private-user-key"
    assert (destination / "config/run_plan.json").read_text(encoding="utf-8") == "custom plan"


# 功能：构建脚本必须使用新输出目录/明确安装开关/真实Git身份，不保留无条件覆盖旧包参数。
def test_build_recipe_safety():
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts/build_exe.ps1").read_text(encoding="utf-8-sig")
    spec = (root / "scripts/autodidact.spec").read_text(encoding="utf-8-sig")
    assert "--noconfirm" not in script and "--clean" not in script
    assert (
        "InstallDependencies" in script
        and "--source-revision" in script
        and "dist\\releases" in script
    )
    assert "__pycache__" in spec and "run_plan.example.json" in spec
