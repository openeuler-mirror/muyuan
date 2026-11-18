#!/usr/bin/env python3
"""为 coding-agent 宿主一次性安装 /ahe 命令。

宿主（二选一；--pi 为默认，保持向后兼容）：

  --pi      Pi coding agent：把本 checkout 追加进 Pi settings.json
            的 packages 列表，此后所有正常启动的 Pi（不带
            --no-extensions）都自带 /ahe，并复用 Pi 自己的 API 配置。
  --codex   Codex CLI：渲染 assets/codex/prompts/ahe.md 模板（写入
            引擎绝对路径）并写到 <codex home>/prompts/ahe.md，
            此后所有 Codex 会话都自带 /ahe。

用法：
  python3 install.py --pi                 # 注册 Pi
  python3 install.py --codex              # 安装 Codex /ahe prompt
  python3 install.py --pi --status        # 只查看 Pi 注册状态
  python3 install.py --codex --status     # 只查看 Codex prompt 状态
  python3 install.py --pi --uninstall     # 取消 Pi 注册
  python3 install.py --codex --uninstall  # 卸载 Codex prompt
  python3 install.py --dry-run ...        # 只打印将执行的动作

bash 包装接受相同参数：
  bash install.sh --pi | --codex
  bash uninstall.sh --pi | --codex        # 等价于 --uninstall

环境变量：
  PI_CODING_AGENT_DIR   Pi agent 目录（默认 ~/.pi/agent）；
                        settings 位于 <dir>/settings.json
  CODEX_HOME            Codex home 目录（默认 ~/.codex）；
                        prompt 写入 <dir>/prompts/ahe.md
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 共享工具
# ---------------------------------------------------------------------------

def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        # utf-8-sig 兼容带或不带 BOM 的文件（部分 Windows 工具会添加 BOM）。
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: cannot parse {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"ERROR: {path} must contain a JSON object")
    return payload


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Pi 目标
# ---------------------------------------------------------------------------

EXTENSION = PACKAGE_ROOT / "assets" / "pi" / "extensions" / "ahe.ts"
PACKAGE_MANIFEST = PACKAGE_ROOT / "package.json"


def pi_agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    path = Path(configured).expanduser() if configured else Path.home() / ".pi" / "agent"
    return Path(os.path.abspath(path))


def registered_entries(payload: dict) -> list[str]:
    """返回指向本 checkout 的 package 来源（任意路径归一化形式）。"""
    matches = []
    for entry in payload.get("packages", []):
        source = entry if isinstance(entry, str) else (
            entry.get("source") if isinstance(entry, dict) else None
        )
        if not isinstance(source, str) or source.startswith(("npm:", "git:", "http", "ssh:", "git://")):
            continue
        candidate = Path(source).expanduser()
        if not candidate.is_absolute():
            continue
        if candidate.resolve() == PACKAGE_ROOT:
            matches.append(source)
    return matches


def run_pi(args: argparse.Namespace) -> int:
    if not EXTENSION.is_file():
        raise SystemExit(f"ERROR: extension not found: {EXTENSION}")
    if not PACKAGE_MANIFEST.is_file():
        raise SystemExit(f"ERROR: package manifest not found: {PACKAGE_MANIFEST}")

    settings = pi_agent_dir() / "settings.json"
    source = str(PACKAGE_ROOT)
    payload = read_json(settings)
    entries = registered_entries(payload)

    if args.status:
        if entries:
            print(f"AHE is registered as a Pi package: {settings}")
            for entry in entries:
                print(f"  packages: {entry}")
        else:
            print(f"AHE is NOT registered in {settings}")
            print(f"Register with: python3 {Path(__file__).name} --pi")
        return 0

    if args.uninstall:
        if not entries:
            print(f"AHE is not registered in {settings}; nothing to do")
            return 0
        if args.dry_run:
            print(f"Would remove {entries} from {settings}")
            return 0
        payload["packages"] = [
            entry for entry in payload.get("packages", [])
            if not (isinstance(entry, str) and entry in entries)
            and not (isinstance(entry, dict) and entry.get("source") in entries)
        ]
        if not payload["packages"]:
            payload.pop("packages", None)
        atomic_write(settings, json.dumps(payload, indent=2) + "\n")
        print(f"Removed AHE registration from {settings}")
        return 0

    if entries:
        print(f"AHE is already registered in {settings}")
        for entry in entries:
            print(f"  packages: {entry}")
        return 0

    if args.dry_run:
        print(f"Would append to {settings} packages: {source}")
        return 0

    packages = payload.get("packages", [])
    if not isinstance(packages, list):
        raise SystemExit(f"ERROR: {settings} packages must be a list")
    packages.append(source)
    payload["packages"] = packages
    if settings.is_file():
        backup = settings.with_suffix(".json.ahe-backup")
        shutil.copy2(settings, backup)
        print(f"Backup written: {backup}")
    atomic_write(settings, json.dumps(payload, indent=2) + "\n")
    print(f"Registered AHE as a Pi package: {settings}")
    print(f"  packages: {source}")
    print()
    print("Done. Launch Pi normally (without --no-extensions) and use:")
    print("  /ahe 优化harness, harness路径为/path/to/harness, 提示词为..., 3轮")
    return 0


# ---------------------------------------------------------------------------
# Codex 目标
# ---------------------------------------------------------------------------

TEMPLATE = PACKAGE_ROOT / "assets" / "codex" / "prompts" / "ahe.md"
ENGINE = PACKAGE_ROOT / "ahe.py"
MARKER_RE = re.compile(r"<!--\s*ahe-plugin-root:\s*(.+?)\s*-->")


def codex_home(arg_value: str | None) -> Path:
    configured = arg_value or os.environ.get("CODEX_HOME")
    path = Path(configured).expanduser() if configured else Path.home() / ".codex"
    return Path(os.path.abspath(path))


def render_prompt() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    return (text
            .replace("__AHE_PLUGIN_ROOT__", str(PACKAGE_ROOT))
            .replace("__AHE_SCRIPT__", str(ENGINE)))


def installed_root(prompt_path: Path) -> str | None:
    """返回已安装 prompt 中记录的 checkout 路径（若无则返回 None）。"""
    try:
        match = MARKER_RE.search(prompt_path.read_text(encoding="utf-8-sig"))
    except OSError:
        return None
    return match.group(1).strip() if match else None


def run_codex(args: argparse.Namespace) -> int:
    if not TEMPLATE.is_file():
        raise SystemExit(f"ERROR: prompt template not found: {TEMPLATE}")
    if not ENGINE.is_file():
        raise SystemExit(f"ERROR: engine not found: {ENGINE}")

    prompt_path = codex_home(args.codex_home) / "prompts" / "ahe.md"
    current = installed_root(prompt_path)

    if args.status:
        if prompt_path.is_file():
            origin = current or "unknown (no ahe marker)"
            state = "current" if current == str(PACKAGE_ROOT) else "stale"
            print(f"/ahe prompt installed: {prompt_path} ({state}, from {origin})")
        else:
            print(f"/ahe prompt is NOT installed in {prompt_path.parent}")
            print(f"Install with: python3 {Path(__file__).name} --codex")
        return 0

    if args.uninstall:
        if not prompt_path.is_file():
            print(f"/ahe prompt is not installed in {prompt_path.parent}; nothing to do")
            return 0
        if current is None:
            raise SystemExit(
                f"ERROR: {prompt_path} was not written by this installer; remove it manually")
        if args.dry_run:
            print(f"Would remove {prompt_path}")
            return 0
        prompt_path.unlink()
        print(f"Removed /ahe prompt: {prompt_path}")
        return 0

    body = render_prompt()
    if prompt_path.is_file():
        try:
            existing = prompt_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise SystemExit(f"ERROR: cannot read {prompt_path}: {exc}") from exc
        if existing == body:
            print(f"/ahe prompt already up to date: {prompt_path}")
            return 0

    if args.dry_run:
        print(f"Would write {prompt_path} (engine: {ENGINE})")
        return 0

    if prompt_path.is_file():
        backup = prompt_path.with_suffix(".md.ahe-backup")
        shutil.copy2(prompt_path, backup)
        print(f"Backup written: {backup}")
    atomic_write(prompt_path, body)
    print(f"Installed /ahe prompt for Codex: {prompt_path}")
    print(f"  engine: {ENGINE}")
    print()
    print("Done. Restart Codex and use:")
    print("  /ahe <harness 路径或命令>, <任务描述>, <目标/指标>, N轮")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--pi", action="store_true",
                        help="target the Pi coding agent (default)")
    target.add_argument("--codex", action="store_true",
                        help="target the Codex CLI")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--uninstall", action="store_true",
                      help="remove the /ahe registration for the selected host")
    mode.add_argument("--status", action="store_true",
                      help="only show the current registration for the selected host")
    parser.add_argument("--dry-run", action="store_true",
                        help="print actions without writing")
    parser.add_argument("--codex-home",
                        help="Codex home (default: $CODEX_HOME or ~/.codex)")
    args = parser.parse_args()
    return run_codex(args) if args.codex else run_pi(args)


if __name__ == "__main__":
    sys.exit(main())
