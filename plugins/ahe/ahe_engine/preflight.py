"""harness 安装入口检测、安装与只读预检。"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .fsutil import _read_head

# ---------------------------------------------------------------------------
# harness 安装（源码 checkout 尚不能运行时）
# ---------------------------------------------------------------------------

def _detect_install_command(harness_root: Path, manifest_payload: dict) -> list[str] | None:
    installation = manifest_payload.get("installation")
    command = installation.get("command") if isinstance(installation, dict) else installation
    if isinstance(command, str) and command.strip():
        return shlex.split(command)
    if isinstance(command, (list, tuple)) and command:
        return [str(item) for item in command]
    for name, argv in (
        ("install.sh", ["bash", "install.sh"]),
        ("install.py", [sys.executable, "install.py"]),
        ("setup.py", [sys.executable, "setup.py", "install"]),
    ):
        if (harness_root / name).is_file():
            return argv
    if (harness_root / "package.json").is_file():
        return ["npm", "install"]
    if (harness_root / "pyproject.toml").is_file():
        return [sys.executable, "-m", "pip", "install", "-e", "."]
    return None


def install_harness(harness: str, command: str | None = None,
                    manifest_path: str | None = None,
                    timeout_s: float = 3600.0) -> bool:
    """从源码目录安装 harness（使用其自带安装入口）。"""
    harness_root = Path(harness).expanduser().resolve()
    if not harness_root.is_dir():
        print(f"ERROR: harness directory not found: {harness_root}")
        return False
    manifest_payload: dict = {}
    if manifest_path:
        try:
            manifest_payload = json.loads(
                Path(manifest_path).expanduser().read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            print(f"WARNING: cannot read manifest {manifest_path}: {exc}")
    if command:
        argv = shlex.split(command) if isinstance(command, str) else [str(c) for c in command]
    else:
        argv = _detect_install_command(harness_root, manifest_payload)
        if argv is None:
            print("ERROR: no installation entry found for this harness. Options:")
            print("  1. pass one explicitly:  --install-harness '<command>'")
            print('  2. declare it in the manifest: "installation": {"command": ["bash", "install.sh"]}')
            return False
    print(f"[install] {' '.join(argv)}   (cwd={harness_root})")
    try:
        completed = subprocess.run(argv, cwd=str(harness_root), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        print(f"ERROR: installation timed out after {timeout_s}s")
        return False
    except FileNotFoundError as exc:
        print(f"ERROR: cannot execute installation command: {exc}")
        return False
    if completed.returncode != 0:
        print(f"ERROR: installation failed with exit code {completed.returncode}")
        return False
    print("[install] OK")
    print("安装产物落在源目录，后续每个候选快照都会带上它们。接下来可以先验证:")
    hint = f"python3 ahe.py {harness}"
    if manifest_path:
        hint += f" --manifest {manifest_path}"
    print(f"  {hint} --baseline-only --task 'verify harness runs'")
    return True


_MISSING_DEP_RE = re.compile(
    r"ModuleNotFoundError|No module named|Cannot find module|Module not found|"
    r"ImportError|npm err|command not found", re.IGNORECASE)


def _looks_like_missing_dependency(output: str) -> bool:
    return bool(_MISSING_DEP_RE.search(output or ""))


def preflight(harness: str, manifest_path: str | None = None) -> bool:
    """从源码目录做运行前检查：是否已安装、如何运行。

    只读：此处不修改任何内容。检查报告缺少安装时，后续执行
    ``--install-harness`` 即可。
    """
    harness_root = Path(harness).expanduser().resolve()
    if not harness_root.is_dir():
        print(f"ERROR: harness directory not found: {harness_root}")
        return False
    manifest_payload: dict = {}
    if manifest_path:
        try:
            manifest_payload = json.loads(
                Path(manifest_path).expanduser().read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            print(f"WARNING: cannot read manifest {manifest_path}: {exc}")

    print(f"[preflight] harness: {harness_root}")
    ready: bool | None = None
    missing_dep = False

    # --- 1. 安装状态 ---
    install_cmd = _detect_install_command(harness_root, manifest_payload)
    print("安装状态:")
    print(f"  安装入口: {' '.join(install_cmd) if install_cmd else '（未检出）'}")
    if (harness_root / "package.json").is_file():
        has_modules = (harness_root / "node_modules").is_dir()
        print(f"  node_modules: {'存在' if has_modules else '缺失（npm 依赖未装）'}")
        if not has_modules:
            ready = False
    if (harness_root / "pyproject.toml").is_file() or (harness_root / "setup.py").is_file():
        venv = any((harness_root / name).is_dir() for name in (".venv", "venv", "env"))
        print(f"  虚拟环境(.venv/venv/env): {'存在' if venv else '未检出'}")
    for name in ("install.sh", "install.py"):
        if (harness_root / name).is_file():
            print(f"  {name}: 存在（harness 自带安装入口）")

    validation = manifest_payload.get("validation")
    validation_cmd = validation.get("command") if isinstance(validation, dict) else None
    if validation_cmd:
        argv = (shlex.split(validation_cmd) if isinstance(validation_cmd, str)
                else [str(c) for c in validation_cmd])
        print(f"  运行探针(manifest validation.command): {' '.join(argv)}")
        try:
            completed = subprocess.run(argv, cwd=str(harness_root), timeout=600,
                                       capture_output=True, text=True)
            probe_ok = completed.returncode == 0
            print(f"  探针结果: {'通过' if probe_ok else '失败'}")
            if not probe_ok:
                preview = (completed.stderr or completed.stdout or "").strip()[-400:]
                if preview:
                    print(f"  失败输出（尾部）: {preview}")
                missing_dep = _looks_like_missing_dependency(completed.stderr or "")
                if missing_dep:
                    print("  疑似缺少依赖（ModuleNotFoundError / Cannot find module ...）")
            ready = probe_ok if ready is None else (ready and probe_ok)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            print(f"  探针无法执行: {exc}")
            ready = False
    if ready is None:
        ready = True
        print("  （未声明运行探针；仅按目录特征判断，建议先 --baseline-only 真实验证）")

    # --- 2. 如何运行 ---
    print("运行方式:")
    adapter = manifest_payload.get("adapter")
    if isinstance(adapter, dict):
        print(f"  - manifest adapter: {adapter.get('name')}")
    elif isinstance(adapter, str):
        print(f"  - manifest adapter: {adapter}")
    for key in ("evaluation", "evaluation_command"):
        if manifest_payload.get(key):
            print(f"  - manifest {key}: {manifest_payload[key]}")
    for name, desc in (
        ("harness.manifest.json", "组件模式入口（manifest/adapter）"),
        ("prompts", "prompt harness（prompts/ 下的文件作为 system prompt）"),
        ("run_benchmark.py", "直接运行入口（cd 到 harness 目录执行）"),
    ):
        if (harness_root / name).exists():
            print(f"  - {name}: {desc}")
    readme = harness_root / "README.md"
    if readme.is_file():
        print("  - README 摘要:")
        for line in _read_head(readme, max_chars=600).splitlines()[:6]:
            if line.strip():
                print(f"      {line.strip()[:100]}")

    # --- 3. 下一步建议 ---
    print()
    base_cmd = f"python3 ahe.py {harness}"
    if manifest_path:
        base_cmd += f" --manifest {manifest_path}"
    if not ready:
        print("结论: 尚未就绪。先从源目录安装:")
        print(f"  {base_cmd} --install-harness")
        return False
    print("结论: 就绪。建议下一步:")
    print(f"  {base_cmd} --task 'verify harness runs' --baseline-only")
    print("（评测器缺失时可用 --generate-evaluator 生成，然后进入完整优化循环）")
    return True

