"""Read-only diagnostics shared by host displays and the installed CLI."""

import os
from pathlib import Path
import platform
import shutil
from typing import Literal

from goal_plus._version import __version__
from goal_plus.infrastructure.api_debug import api_debug_enabled
from goal_plus.installation import snapshot


RUNTIME_INFO_URI = "goal-plus://runtime/info"


def runtime_info(
    root: Path | None, *, agent_harness: Literal["codex", "pi"],
    runtime_provider: Literal["direct", "thinkthread"] | None = None,
) -> dict:
    installation = snapshot()
    build = installation.get("build")
    if runtime_provider is None:
        thinkthread = (os.environ.get("GOAL_PLUS_THINKTHREAD_ROLE") == "main" if agent_harness == "codex"
                       else bool(os.environ.get("GOAL_PLUS_AGENT_POSIX_SDK_ENTRY")))
        runtime_provider = "thinkthread" if thinkthread else "direct"
    provider = runtime_provider
    thinkthread = provider == "thinkthread"
    system = platform.system()
    backend = "thinkthread" if thinkthread else "codex_native"
    executable = None
    if agent_harness == "pi" and not thinkthread:
        backend = {"Darwin": "seatbelt", "Linux": "bubblewrap"}.get(system, "unsupported")
        command = {"Darwin": "/usr/bin/sandbox-exec", "Linux": "bwrap"}.get(system)
        executable = shutil.which(command) if command else None
    return {
        "schema": "goal-plus.runtime-info.v1",
        "version": __version__,
        "server_version": f"{__version__}+{build}" if build else __version__,
        "installation": installation,
        "host": {"agent_harness": agent_harness, "runtime_provider": provider},
        "platform": {"system": system, "release": platform.release(), "machine": platform.machine()},
        "runtime": {"root": str(root.expanduser().resolve()) if root is not None else None,
                    "debug_enabled": api_debug_enabled()},
        "sandbox": {
            "worker_backend": backend,
            "backend_executable": executable,
            "backend_executable_found": bool(executable) if agent_harness == "pi" and not thinkthread else None,
            "worker_policies": ["workspace-write", "read-only"] if provider == "direct" else [],
            "main_policy": "launcher-process-user" if agent_harness == "pi" and not thinkthread else "host_managed",
            "execution_verified": False,
            "scope": "Configured worker defaults; this read does not inspect or launch workers.",
        },
    }


def runtime_info_title(info: dict) -> str:
    # The full build is retained in serverInfo.version and the JSON resource.
    build = info["installation"].get("build")
    identity = f"build {build[:12]}" if build else "checkout"
    debug = "on" if info["runtime"]["debug_enabled"] else "off"
    backend = info["sandbox"]["worker_backend"]
    if info["sandbox"]["backend_executable_found"] is False:
        backend += " (unavailable)"
    return (f"Goal Plus {info['version']} | {identity} | "
            f"worker sandbox: {backend} | DEBUG {debug}")


def runtime_info_text(info: dict) -> str:
    installation = info["installation"]
    sandbox = info["sandbox"]
    if sandbox["worker_backend"] == "thinkthread":
        executable = "managed by ThinkThread (not inspected)"
    elif sandbox["worker_backend"] == "codex_native":
        executable = "managed by Codex (not inspected)"
    else:
        executable = sandbox["backend_executable"] or "unavailable (executable not found)"
    return "\n".join([
        runtime_info_title(info),
        f"Host: {info['host']['agent_harness']} / {info['host']['runtime_provider']}",
        f"Build: {installation.get('build') or 'checkout'}",
        f"Python: {installation['python']}",
        f"Package: {installation.get('package') or installation.get('module') or installation.get('source')}",
        f"Platform: {info['platform']['system']} {info['platform']['release']} {info['platform']['machine']}",
        f"Runtime root: {info['runtime']['root'] or 'not inspected (session-specific)'}",
        f"Worker policies: {', '.join(info['sandbox']['worker_policies']) or 'provider-managed'}",
        f"Backend executable: {executable}",
        f"Main permissions: {info['sandbox']['main_policy']}",
        "Sandbox execution: not probed (worker defaults only)",
    ])
