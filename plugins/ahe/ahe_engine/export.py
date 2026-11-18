"""将被接受的最终候选导出为可直接使用的 harness。"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

# ---------------------------------------------------------------------------
# 导出最终被接受的 harness
# ---------------------------------------------------------------------------

def _usage_hints(target: Path, renamed_commands: list[str] | None = None,
                 codex_commands: list[str] | None = None) -> list[str]:
    hints = [f"通用用法: 在你原来使用 harness 的任何命令里，把 harness 路径替换为 {target}"]
    if renamed_commands:
        hints.append(f"新命令（已改名，重启 Pi 后可用）: " + ", ".join(f"/{c}" for c in renamed_commands))
    if codex_commands:
        hints.append(f"Codex 命令（已安装为插件，重启 Codex 后可用）: "
                     + ", ".join(f"/{c}" for c in codex_commands))
    if (target / "install.py").is_file() and (target / "assets" / "pi" / "extensions").is_dir():
        hints.append(f"作为 Pi 插件注册: python3 {target}/install.py  (或 pi install {target})")
    if (target / "prompts").is_dir():
        hints.append(f"作为 prompt harness: 把 {target}/prompts 下的文件用作 system prompt")
    if (target / "run_benchmark.py").is_file():
        hints.append(f"直接运行: cd {target} && python3 run_benchmark.py")
    return hints


_REGISTER_COMMAND_RE = re.compile(
    r'(registerCommand\s*\(\s*)(["\'])([^"\']+)(\2)')


def _rename_registered_commands(target: Path, suffix: str) -> list[str]:
    """为导出副本中注册的 Pi 扩展命令名添加后缀。"""
    renamed: list[str] = []
    ext_root = target / "assets" / "pi" / "extensions"
    if ext_root.is_dir():
        candidates = sorted(list(ext_root.glob("*.ts")) + list(ext_root.glob("*.js")))
    else:
        candidates = sorted(list(target.glob("*.ts")) + list(target.glob("*.js")))
    for ext_file in candidates:
        try:
            text = ext_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        def _sub(match: "re.Match[str]") -> str:
            name = match.group(3)
            if name.endswith(f"-{suffix}"):
                return match.group(0)
            renamed.append(f"{name}-{suffix}")
            return f"{match.group(1)}{match.group(2)}{name}-{suffix}{match.group(4)}"

        new_text = _REGISTER_COMMAND_RE.sub(_sub, text)
        if new_text != text:
            ext_file.write_text(new_text, encoding="utf-8")
    return sorted(set(renamed))


def _register_pi_package(package_root: Path) -> Path:
    """将导出的 harness 追加到 Pi 的 packages，使其命令可加载。"""
    import install as ahe_install

    settings = ahe_install.pi_agent_dir() / "settings.json"
    payload = ahe_install.read_json(settings)
    source = str(package_root)
    for entry in payload.get("packages", []):
        candidate = entry if isinstance(entry, str) else (
            entry.get("source") if isinstance(entry, dict) else None)
        if candidate == source or (
                isinstance(candidate, str) and Path(candidate).resolve() == package_root):
            return settings
    if not settings.is_file():
        backup = None
    else:
        backup = settings.with_suffix(".json.ahe-export-backup")
        shutil.copy2(settings, backup)
    packages = payload.get("packages", [])
    packages.append(source)
    payload["packages"] = packages
    ahe_install.atomic_write(settings, json.dumps(payload, indent=2) + "\n")
    if backup is not None:
        print(f"Pi settings backup: {backup}")
    return settings


_PLUGIN_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _register_codex_plugin(target: Path, suffix: str | None,
                           original_root: Path | None) -> tuple[list[str], str | None]:
    """把导出 harness 安装为 Codex 插件（Agent Plugins 规范）。

    现代 Codex（≥0.14x）的扩展入口是插件体系：marketplace manifest 位于
    ``.agents/plugins/marketplace.json``，命令模板为插件根下 ``commands/*.md``
    （正文以 ``$ARGUMENTS``/``$N`` 接收用户参数），与官方 marketplace 插件
    结构一致。本函数在导出副本内：

    1. 渲染 ``assets/codex/prompts/*.md``（占位符 ``__AHE_PLUGIN_ROOT__``/
       ``__AHE_SCRIPT__`` 替换为导出副本的绝对路径）为 ``commands/<name>-<后缀>.md``；
       已自带 ``.codex-plugin/plugin.json`` 的 harness 无需模板
       也会被注册；
    2. 生成/改写 ``.codex-plugin/plugin.json``（插件名带后缀，与原 harness 共存）；
    3. 生成单插件 marketplace manifest（``path: "."``，与官方插件同构）；
    4. 调用官方 ``codex plugin marketplace add`` + ``codex plugin add`` 注册
       （插件被缓存到 ``<CODEX_HOME>/plugins/cache/``，重启 Codex 后生效）；
    5. 兼容：同时把渲染后的模板写入 ``<CODEX_HOME>/prompts/``，供仍读取该
       目录的旧版 codex 使用。

    注意：插件 commands 的斜杠命令呈现由 codex 版本决定（0.166 实测未激活
    prompts 目录与插件 commands；插件本身可经 $提及/MCP 使用）。

    返回 (命令名列表, 插件名)；CLI 不可用时命令文件仍写好并返回命令名、
    插件名为 None（打印手动注册命令）。
    """
    import subprocess

    import install as ahe_install

    template_root = target / "assets" / "codex" / "prompts"
    templates = sorted(template_root.glob("*.md")) if template_root.is_dir() else []
    has_own_manifest = (target / ".codex-plugin" / "plugin.json").is_file()
    if not templates and not has_own_manifest:
        return [], None

    base = _PLUGIN_NAME_SANITIZE_RE.sub("-", target.name).strip("-") or "ahe-export"
    plugin_name = f"{base}-{suffix}" if suffix else base

    replacements: list[tuple[str, str]] = []
    if original_root is not None and str(original_root) != str(target):
        replacements.append((str(original_root), str(target)))
    replacements.append(("__AHE_PLUGIN_ROOT__", str(target)))
    engine = target / "ahe.py"
    if engine.is_file():
        replacements.append(("__AHE_SCRIPT__", str(engine)))

    commands: list[str] = []
    rendered: dict[str, str] = {}
    if templates:
        commands_dir = target / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)
        for template in templates:
            try:
                text = template.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for old, new in replacements:
                text = text.replace(old, new)
            stem = f"{template.stem}-{suffix}" if suffix else template.stem
            (commands_dir / f"{stem}.md").write_text(text, encoding="utf-8")
            rendered[stem] = text
            commands.append(stem)

    # legacy fallback for codex builds that load <CODEX_HOME>/prompts/*.md
    if rendered:
        try:
            prompts_dir = ahe_install.codex_home(None) / "prompts"
            prompts_dir.mkdir(parents=True, exist_ok=True)
            for stem, text in rendered.items():
                prompt_path = prompts_dir / f"{stem}.md"
                if prompt_path.is_file():
                    backup = prompt_path.with_suffix(".md.ahe-export-backup")
                    shutil.copy2(prompt_path, backup)
                    print(f"Codex prompt backup: {backup}")
                ahe_install.atomic_write(prompt_path, text)
        except Exception as exc:
            print(f"WARNING: legacy codex prompts write failed: {exc}")

    plugin_dir = target / ".codex-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    manifest = plugin_dir / "plugin.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            payload = {}
    else:
        payload = {}
    payload["name"] = plugin_name
    payload.setdefault("version", "0.1.0")
    payload.setdefault(
        "description",
        "AHE-exported harness (commands: "
        + ", ".join(f"/{c}" for c in commands) + ")")
    manifest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    market_dir = target / ".agents" / "plugins"
    market_dir.mkdir(parents=True, exist_ok=True)
    market = {
        "name": plugin_name,
        "interface": {"displayName": plugin_name},
        "plugins": [{
            "name": plugin_name,
            "source": {"source": "local", "path": "."},
            "policy": {"installation": "AVAILABLE",
                       "authentication": "ON_INSTALL"},
            "category": "Developer Tools",
        }],
    }
    (market_dir / "marketplace.json").write_text(
        json.dumps(market, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    codex = shutil.which("codex")
    if codex is None:
        return commands, None
    try:
        ahe_install.codex_home(None).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    for args in (("plugin", "marketplace", "add", str(target)),
                 ("plugin", "add", f"{plugin_name}@{plugin_name}")):
        try:
            result = subprocess.run([codex, *args], capture_output=True,
                                    text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"WARNING: codex CLI unavailable: {exc}")
            return commands, None
        out = (result.stdout + result.stderr).strip()
        if result.returncode != 0 and "already" not in out.lower():
            print(f"WARNING: codex {' '.join(args[:3])} failed: {out[:200]}")
            return commands, None
    return commands, plugin_name


def _slugify(text: str) -> str:
    """把任务名收敛为安全的命令后缀字符。"""
    import re as _re
    slug = _re.sub(r"[^0-9A-Za-z._-]+", "-", str(text).strip().lower()).strip("-.")
    return slug


def _derive_task_suffix(report: dict) -> str:
    """导出命名后缀 = 评测任务名（如 pypto / swebench）。

    优先级：report 记录的 --eval-project 任务名，其次 adapter 名。
    推导不出时返回空串（保留原命令名，与原 harness 冲突时自行处理）。
    """
    project = str(report.get("eval_task_project") or "").strip()
    if project:
        return _slugify(project)
    adapter = str(report.get("adapter") or "").strip()
    if adapter:
        return _slugify(adapter)
    return ""


def export_best(report_path: Path, target: str | None = None,
                *, register: bool = True, suffix: str | None = None,
                no_rename: bool = False) -> Path | None:
    """将被接受的最终候选导出为可直接使用的 harness。

    导出命名规则：``<原 harness 名>-<任务名>``（如 my-tool-pypto、
    my-tool-swebench）。suffix 未显式指定时自动从 report 的评测任务名
    推导；推导不出则保留原命令名；no_rename=True 强制保留原命令名。
    """
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read report {report_path}: {exc}")
        return None
    if no_rename:
        suffix = ""
    elif suffix is None:
        suffix = _derive_task_suffix(report)
    source = report.get("accepted_candidate")
    if not source:
        print("ERROR: report has no accepted candidate (nothing passed the keep gate)")
        return None
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_dir():
        print(f"ERROR: accepted candidate is missing on disk: {source_path}")
        return None
    harness = report.get("harness") or str(source_path.parent)
    target_path = (Path(target).expanduser().resolve() if target
                   else Path(str(harness) + ".ahe-best").expanduser().resolve())
    if target_path.exists():
        print(f"ERROR: target already exists: {target_path} (pick another --target)")
        return None
    shutil.copytree(source_path, target_path, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    renamed_commands: list[str] = []
    if suffix:
        renamed_commands = _rename_registered_commands(target_path, suffix)
        if renamed_commands:
            print(f"命令已按 `<harness>-<任务名>` 规则改名: "
                  + ", ".join(f"/{c}" for c in renamed_commands))
        else:
            print(f"NOTE: no registerCommand calls found to rename (suffix '{suffix}' had no effect)")
    elif not suffix:
        print("NOTE: 未推导出任务名，保留原命令名（如与原 harness 冲突可用 --as-suffix 指定）")
    codex_commands: list[str] = []
    if register:
        try:
            settings = _register_pi_package(target_path)
            print(f"已注册为 Pi 插件（packages += {target_path}）: {settings}")
            print("重启 Pi 会话后，新 harness 的命令即可使用（不带 --no-extensions 启动）")
        except Exception as exc:
            print(f"WARNING: Pi registration failed: {exc}")
            print(f"可手动注册: pi install {target_path}")
        try:
            original_root = Path(str(harness)).expanduser().resolve()
            codex_commands, codex_plugin = _register_codex_plugin(
                target_path, suffix, original_root)
            if codex_plugin:
                print("已为 Codex 安装插件: " + codex_plugin)
                if codex_commands:
                    print("  命令模板: " + ", ".join(f"/{c}" for c in codex_commands)
                          + "（参数经 $ARGUMENTS 注入；斜杠命令的呈现依赖 codex 版本"
                            "对插件 commands 的支持）")
                print("  同时写入 <CODEX_HOME>/prompts/ 兼容仍读取该目录的旧版 codex")
                print("重启 Codex 会话后生效；插件方式也可经 $提及 / MCP server 使用")
            elif codex_commands:
                print("已生成 Codex 插件文件（commands/、.agents/plugins/marketplace.json）；"
                      "未完成自动注册，可手动执行:")
                print(f"  codex plugin marketplace add {target_path}")
                print(f"  codex plugin add <插件名>@<marketplace>")
            else:
                print("NOTE: harness 未携带 assets/codex/prompts/*.md 或 .codex-plugin/"
                      "plugin.json，未安装 Codex 侧工件（Codex 侧可通过 MCP server 或 "
                      "install.sh 接入导出目录）")
        except Exception as exc:
            print(f"WARNING: Codex registration failed: {exc}")
            print(f"可手动安装: 渲染 {target_path}/assets/codex/prompts/*.md 后放入插件 commands/")

    baseline = report.get("baseline") or {}
    final = report.get("final") or {}
    keeps = sum(1 for row in (report.get("history") or []) if row.get("action") == "keep")
    print(f"Final harness exported: {target_path}")
    print(f"Source candidate: {source_path}")
    print(f"Original harness: {harness} (unchanged)")
    print(f"Kept candidates: {keeps}")
    for key in sorted(set(baseline) | set(final)):
        if isinstance(final.get(key), (int, float)) or isinstance(baseline.get(key), (int, float)):
            print(f"  {key}: baseline={baseline.get(key)} final={final.get(key)}")
    print()
    print("使用方式:")
    for hint in _usage_hints(target_path, renamed_commands, codex_commands):
        print(f"  - {hint}")
    print()
    print(f"对比全部改动: diff -ru {harness} {target_path}")
    print(f"逐轮 patch 记录: {report.get('trace_dir')}/changes/ (或报告内 change_manifest)")
    return target_path
