# 文件职责：定义 PyInstaller 资源、隐藏模块和文件夹发布结构，不打包真实密钥、学习数据库或 Chromium 用户资料。
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
datas = [(str(root / "alembic.ini"), "autodidact/_resources"),
         (str(root / ".env.example"), "autodidact/_resources"),
         (str(root / "config" / "agent.yaml"), "autodidact/_resources/config"),
         (str(root / "config" / "run_plan.example.json"), "autodidact/_resources/config"),
         (str(root / "migrations" / "script.py.mako"), "autodidact/_resources/migrations")]
# 只收明确源码/样例，不递归复制__pycache__、用户配置或临时备份。
for path in (root / "migrations").rglob("*.py"):
    if "__pycache__" not in path.parts:
        datas.append((str(path), str(Path("autodidact/_resources") / path.parent.relative_to(root))))
for name in ("sample.json", "reliable_sample.json", "belief_audit.example.json"):
    datas.append((str(root / "data" / "benchmark" / name), "autodidact/_resources/data/benchmark"))
datas += collect_data_files("playwright")
hidden = collect_submodules("sqlalchemy.dialects.postgresql") + collect_submodules("asyncpg")
a = Analysis([str(root / "scripts" / "autodidact_launcher.py")],
             pathex=[str(root / "src")], binaries=[], datas=datas,
             hiddenimports=hidden, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=["pytest"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Autodidact", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Autodidact")
