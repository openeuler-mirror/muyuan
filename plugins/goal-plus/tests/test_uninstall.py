from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_uninstaller() -> ModuleType:
    path = ROOT / "scripts" / "uninstall.py"
    spec = importlib.util.spec_from_file_location("goal_plus_uninstall", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def uninstaller() -> ModuleType:
    return _load_uninstaller()


def test_uninstall_entrypoint_is_executable() -> None:
    entrypoint = ROOT / "uninstall.sh"

    assert entrypoint.is_file()
    assert entrypoint.stat().st_mode & 0o111


def _isolated_entrypoint_env(bin_dir: Path) -> dict[str, str]:
    dirname = shutil.which("dirname")
    assert dirname is not None
    (bin_dir / "dirname").symlink_to(dirname)
    env = os.environ.copy()
    env.pop("PYTHON", None)
    env["PATH"] = str(bin_dir)
    return env


def test_uninstall_entrypoint_accepts_python_shebang(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "goal-plus"
    command.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    command.chmod(0o755)

    completed = subprocess.run(
        ["/bin/bash", str(ROOT / "uninstall.sh"), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=_isolated_entrypoint_env(bin_dir),
    )

    assert completed.returncode == 0, completed.stderr
    assert "--codex" in completed.stdout
    assert "--pi" in completed.stdout


def test_uninstall_entrypoint_uses_sibling_python_for_shell_wrapper(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command = bin_dir / "goal-plus"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o755)
    (bin_dir / "python3").symlink_to(sys.executable)

    completed = subprocess.run(
        ["/bin/bash", str(ROOT / "uninstall.sh"), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=_isolated_entrypoint_env(bin_dir),
    )

    assert completed.returncode == 0, completed.stderr
    assert "--codex" in completed.stdout
    assert "--pi" in completed.stdout


def test_uninstall_entrypoint_rejects_non_python_override(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    env = _isolated_entrypoint_env(bin_dir)
    env["PYTHON"] = "/bin/sh"

    completed = subprocess.run(
        ["/bin/bash", str(ROOT / "uninstall.sh"), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 1
    assert "PYTHON is not a working Python 3.10+ interpreter" in completed.stderr


def test_component_selection_defaults_to_all(uninstaller: ModuleType) -> None:
    args = uninstaller.build_parser().parse_args([])

    assert uninstaller._selected_components(args) == uninstaller.ALL_COMPONENTS


def test_component_selection_keeps_only_explicit_flags(
    uninstaller: ModuleType,
) -> None:
    args = uninstaller.build_parser().parse_args(["--codex", "--pi"])

    assert uninstaller._selected_components(args) == frozenset({"codex", "pi"})


def test_component_selection_supports_thinkthread(uninstaller: ModuleType) -> None:
    args = uninstaller.build_parser().parse_args(["--thinkthread"])

    assert uninstaller._selected_components(args) == frozenset({"thinkthread"})


def test_codex_plan_removes_normal_registration_only(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        uninstaller.install_support,
        "_codex_inventory",
        lambda _codex: [
            {
                "pluginId": "goal-plus@goal-plus",
                "source": {"path": str(ROOT)},
            },
            {
                "pluginId": "goal-plus@goal-plus-local",
                "source": {"path": "/tmp/retired-goal-plus"},
            },
        ],
    )
    monkeypatch.setattr(
        uninstaller.install_support,
        "_codex_marketplaces",
        lambda _codex: {"goal-plus": ROOT.resolve()},
    )

    remove_plugin, remove_marketplace, notes = uninstaller._codex_plan("codex")

    assert remove_plugin is True
    assert remove_marketplace is True
    assert notes == ["Preserved unrelated Codex plugin: goal-plus@goal-plus-local"]


@pytest.mark.parametrize(
    ("plugin_source", "marketplace_source"),
    [
        ("/tmp/other-goal-plus", None),
        ("/tmp/other-goal-plus", "/tmp/other-goal-plus"),
        (None, "/tmp/other-goal-plus"),
        (None, None),
    ],
)
def test_codex_plan_warns_and_removes_registration_regardless_of_source(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    plugin_source: str | None,
    marketplace_source: str | None,
) -> None:
    monkeypatch.setattr(
        uninstaller.install_support,
        "_codex_inventory",
        lambda _codex: [
            {
                "pluginId": "goal-plus@goal-plus",
                "source": {"path": plugin_source},
            }
        ],
    )
    monkeypatch.setattr(
        uninstaller.install_support,
        "_codex_marketplaces",
        lambda _codex: (
            {"goal-plus": Path(marketplace_source).resolve()}
            if marketplace_source else {}
        ),
    )

    remove_plugin, remove_marketplace, notes = uninstaller._codex_plan("codex")

    assert remove_plugin is True
    assert remove_marketplace is (marketplace_source is not None)
    assert any("warning:" in note.lower() for note in notes)
    if plugin_source or marketplace_source:
        assert any(str(ROOT.resolve()) in note for note in notes)
        assert any(
            str(Path(plugin_source or marketplace_source).resolve()) in note
            for note in notes
        )


def test_pi_sources_remove_registered_packages_regardless_of_source(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(
        uninstaller.install_support,
        "_pi_goal_plus_entries",
        lambda: [
            {
                "source": str(ROOT),
                "resolved_path": ROOT.resolve(),
                "settings_path": settings,
            },
            {
                "source": "/tmp/other-goal-plus",
                "resolved_path": Path("/tmp/other-goal-plus"),
                "settings_path": settings,
            },
            {
                "source": "npm:goal-plus",
                "resolved_path": None,
                "settings_path": settings,
            },
            {
                "source": "/tmp/loose/goal-plus.ts",
                "resolved_path": Path("/tmp/loose/goal-plus.ts"),
                "settings_path": None,
            },
        ],
    )

    sources, notes = uninstaller._pi_sources("pi")

    assert sources == (str(ROOT.resolve()), "/tmp/other-goal-plus", "npm:goal-plus")
    assert any("Warning:" in note and "/tmp/other-goal-plus" in note for note in notes)
    assert any("Warning:" in note and "npm:goal-plus" in note for note in notes)
    assert "Preserved loose Pi resource: /tmp/loose/goal-plus.ts" in notes


@pytest.mark.parametrize("host", ["codex", "pi"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_main_removes_foreign_host_registration_without_confirmation(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    host: str,
    dry_run: bool,
) -> None:
    source = tmp_path / "other-checkout" / "goal-plus"
    source.mkdir(parents=True)
    monkeypatch.setattr(uninstaller.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        uninstaller.install_support,
        "_codex_inventory",
        lambda _codex: [
            {"pluginId": "goal-plus@goal-plus", "source": {"path": str(source)}}
        ],
    )
    monkeypatch.setattr(
        uninstaller.install_support,
        "_codex_marketplaces",
        lambda _codex: {"goal-plus": source},
    )
    monkeypatch.setattr(
        uninstaller.install_support,
        "_pi_goal_plus_entries",
        lambda: [{
            "source": str(source),
            "resolved_path": source,
            "settings_path": tmp_path / "settings.json",
        }],
    )
    monkeypatch.setattr(
        "builtins.input", lambda *_args: pytest.fail("must not ask for confirmation")
    )
    commands: list[list[str]] = []

    # Check that the warning is already printed before the first host mutation.
    def host_run(argv: list[str], **_kwargs: object) -> None:
        if not commands:
            assert "Warning:" in capsys.readouterr().out
        commands.append(argv)

    monkeypatch.setattr(uninstaller.subprocess, "run", host_run)
    args = [f"--{host}"] + (["--dry-run"] if dry_run else [])

    assert uninstaller.main(args) == 0

    expected = (
        [["codex", "plugin", "remove", "goal-plus@goal-plus"],
         ["codex", "plugin", "marketplace", "remove", "goal-plus"]]
        if host == "codex" else [["pi", "remove", str(source)]]
    )
    assert commands == ([] if dry_run else expected)
    if dry_run:
        assert "Warning:" in capsys.readouterr().out
    assert source.is_dir()


def test_pi_dev_plan_requires_owned_receipt(
    uninstaller: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dev_dir = tmp_path / "dev"
    dev_dir.mkdir()
    (dev_dir / "installation.json").write_text(
        json.dumps(
            {
                "schema": uninstaller.install_support.DEV_SCHEMA,
                "source_path": str(ROOT),
            }
        ),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    monkeypatch.setenv("GOAL_PLUS_INSTALL_STATE_DIR", str(state_dir))
    registry_path = uninstaller.install_support._pi_dev_registry_path(dev_dir)
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "schema": uninstaller.install_support.DEV_REGISTRY_SCHEMA,
                "dev_dir": str(dev_dir),
                "source_path": str(ROOT),
            }
        ),
        encoding="utf-8",
    )

    remove_dir, remove_registry, notes = uninstaller._pi_dev_plan(dev_dir)

    assert remove_dir == dev_dir.resolve()
    assert remove_registry == registry_path
    assert notes == []


def test_goal_plus_thinkthread_profile_plan_removes_only_current_checkout(
    uninstaller: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "goal-plus-pi.toml"
    path.write_text("managed\n", encoding="utf-8")
    monkeypatch.setattr(
        uninstaller.install_support,
        "_thinkthread_profile_path",
        lambda: path,
    )
    monkeypatch.setattr(
        uninstaller.install_support,
        "_managed_thinkthread_profile_source",
        lambda _path: ROOT.resolve(),
    )

    owned, notes = uninstaller._goal_plus_thinkthread_profile_plan(
        require_owned=True
    )

    assert owned == path
    assert notes == []


def test_goal_plus_thinkthread_profile_plan_refuses_another_checkout(
    uninstaller: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "goal-plus-pi.toml"
    path.write_text("managed\n", encoding="utf-8")
    monkeypatch.setattr(
        uninstaller.install_support,
        "_thinkthread_profile_path",
        lambda: path,
    )
    monkeypatch.setattr(
        uninstaller.install_support,
        "_managed_thinkthread_profile_source",
        lambda _path: tmp_path / "other-checkout",
    )

    with pytest.raises(uninstaller.UninstallError, match="refusing to remove"):
        uninstaller._goal_plus_thinkthread_profile_plan(require_owned=True)


def test_goal_plus_thinkthread_sdk_plan_removes_only_current_checkout(
    uninstaller: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "sdk"
    path.mkdir()
    monkeypatch.setattr(
        uninstaller.install_support,
        "_thinkthread_sdk_root",
        lambda: path,
    )
    monkeypatch.setattr(
        uninstaller.install_support,
        "_managed_thinkthread_sdk_source",
        lambda _path: ROOT.resolve(),
    )

    owned, notes = uninstaller._goal_plus_thinkthread_sdk_plan(require_owned=True)

    assert owned == path
    assert notes == []


class _FakeDistribution:
    def __init__(self, source: Path) -> None:
        self.source = source

    def read_text(self, name: str) -> str | None:
        assert name == "direct_url.json"
        return json.dumps({"url": self.source.as_uri()})


def test_distribution_source_reads_local_install_receipt(
    uninstaller: ModuleType,
) -> None:
    source = uninstaller._distribution_source(_FakeDistribution(ROOT))

    assert source == ROOT.resolve()


def test_runtime_plan_preserves_another_source(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    other_source = tmp_path / "other-goal-plus"
    monkeypatch.setattr(
        uninstaller.importlib.metadata,
        "distribution",
        lambda _name: _FakeDistribution(other_source),
    )

    remove_runtime, notes = uninstaller._runtime_plan()

    assert remove_runtime is False
    assert str(other_source) in notes[0]


def test_build_plan_codex_only_does_not_inspect_other_components(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(uninstaller.shutil, "which", lambda _name: "/opt/bin/codex")
    monkeypatch.setattr(
        uninstaller,
        "_codex_plan",
        lambda _codex: (True, True, []),
    )
    monkeypatch.setattr(
        uninstaller,
        "_pi_sources",
        lambda *_args, **_kwargs: pytest.fail("Pi should not be inspected"),
    )
    monkeypatch.setattr(
        uninstaller,
        "_pi_dev_plan",
        lambda _dev_dir: pytest.fail("Pi development should not be inspected"),
    )
    monkeypatch.setattr(
        uninstaller,
        "_runtime_plan",
        lambda: pytest.fail("runtime should not be inspected"),
    )

    plan = uninstaller._build_plan(
        tmp_path / "dev",
        components=frozenset({"codex"}),
    )

    assert plan.remove_codex_plugin is True
    assert plan.remove_codex_marketplace is True
    assert plan.pi_sources == ()
    assert plan.pi_dev_dir is None
    assert plan.remove_runtime is False


def test_build_plan_pi_dev_only_does_not_inspect_normal_hosts(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dev_dir = tmp_path / "dev"
    registry = tmp_path / "registry.json"
    monkeypatch.setattr(
        uninstaller,
        "_pi_dev_plan",
        lambda _dev_dir: (dev_dir, registry, []),
    )
    monkeypatch.setattr(
        uninstaller,
        "_codex_plan",
        lambda _codex: pytest.fail("Codex should not be inspected"),
    )
    monkeypatch.setattr(
        uninstaller,
        "_pi_sources",
        lambda *_args, **_kwargs: pytest.fail("normal Pi should not be inspected"),
    )
    monkeypatch.setattr(
        uninstaller,
        "_runtime_plan",
        lambda: pytest.fail("runtime should not be inspected"),
    )

    plan = uninstaller._build_plan(
        dev_dir,
        components=frozenset({"pi-dev"}),
    )

    assert plan.pi_dev_dir == dev_dir
    assert plan.pi_dev_registry_path == registry
    assert plan.remove_runtime is False


@pytest.mark.parametrize(
    ("owned_codex", "owned_pi", "expected_host"),
    [
        (True, False, "Codex"),
        (False, True, "Pi"),
        (True, True, "Codex and Pi"),
    ],
)
def test_runtime_removal_refuses_unselected_host_registrations(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    owned_codex: bool,
    owned_pi: bool,
    expected_host: str,
) -> None:
    monkeypatch.setattr(uninstaller.shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        uninstaller,
        "_codex_plan",
        lambda _codex: (owned_codex, False, []),
    )
    monkeypatch.setattr(
        uninstaller,
        "_pi_sources",
        lambda *_args, **_kwargs: ((str(ROOT),), []) if owned_pi else ((), []),
    )
    monkeypatch.setattr(uninstaller, "_runtime_plan", lambda: (True, []))

    with pytest.raises(uninstaller.UninstallError, match=expected_host):
        uninstaller._build_plan(
            tmp_path / "dev",
            components=frozenset({"runtime"}),
        )


def test_runtime_removal_allows_selected_host_registration(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(uninstaller.shutil, "which", lambda name: f"/opt/bin/{name}")
    monkeypatch.setattr(
        uninstaller,
        "_codex_plan",
        lambda _codex: (True, True, []),
    )
    monkeypatch.setattr(uninstaller, "_pi_sources", lambda *_args, **_kwargs: ((), []))
    monkeypatch.setattr(uninstaller, "_runtime_plan", lambda: (True, []))
    monkeypatch.setattr(
        uninstaller,
        "_goal_plus_thinkthread_profile_plan",
        lambda **_kwargs: (None, []),
    )

    plan = uninstaller._build_plan(
        tmp_path / "dev",
        components=frozenset({"codex", "runtime"}),
    )

    assert plan.remove_codex_plugin is True
    assert plan.remove_codex_marketplace is True
    assert plan.remove_runtime is True


def test_execute_removes_hosts_before_runtime(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(
        uninstaller,
        "_run",
        lambda argv, *, dry_run: commands.append(argv),
    )
    plan = uninstaller.UninstallPlan(
        codex_executable="/opt/bin/codex",
        remove_codex_plugin=True,
        remove_codex_marketplace=True,
        pi_executable="/opt/bin/pi",
        pi_sources=(str(ROOT),),
        pi_dev_dir=None,
        pi_dev_registry_path=None,
        remove_runtime=True,
        notes=(),
    )

    uninstaller._execute(plan, dry_run=False)

    assert commands[0][1:3] == ["plugin", "remove"]
    assert commands[1][1:4] == ["plugin", "marketplace", "remove"]
    assert commands[2] == ["/opt/bin/pi", "remove", str(ROOT)]
    assert commands[3] == [
        uninstaller.sys.executable,
        "-m",
        "pip",
        "uninstall",
        "-y",
        "goal-plus",
    ]


def test_dry_run_preserves_managed_pi_dev_directory(
    uninstaller: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dev_dir = tmp_path / "dev"
    dev_dir.mkdir()
    (dev_dir / "installation.json").write_text(
        json.dumps(
            {
                "schema": uninstaller.install_support.DEV_SCHEMA,
                "source_path": str(ROOT),
            }
        ),
        encoding="utf-8",
    )
    plan = uninstaller.UninstallPlan(
        codex_executable=None,
        remove_codex_plugin=False,
        remove_codex_marketplace=False,
        pi_executable=None,
        pi_sources=(),
        pi_dev_dir=dev_dir,
        pi_dev_registry_path=None,
        remove_runtime=False,
        notes=(),
    )

    uninstaller._execute(plan, dry_run=True)

    assert dev_dir.is_dir()
    assert "+ remove managed directory" in capsys.readouterr().out


def test_execute_removes_only_owned_goal_plus_thinkthread_profile(
    uninstaller: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "goal-plus-pi.toml"
    profile.write_text("managed\n", encoding="utf-8")
    monkeypatch.setattr(
        uninstaller.install_support,
        "_managed_thinkthread_profile_source",
        lambda _path: ROOT.resolve(),
    )
    plan = uninstaller.UninstallPlan(
        codex_executable=None,
        remove_codex_plugin=False,
        remove_codex_marketplace=False,
        pi_executable=None,
        pi_sources=(),
        pi_dev_dir=None,
        pi_dev_registry_path=None,
        remove_runtime=False,
        notes=(),
        goal_plus_thinkthread_profile_path=profile,
    )

    uninstaller._execute(plan, dry_run=False)

    assert not profile.exists()


def test_execute_removes_only_owned_agent_posix_sdk(
    uninstaller: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    (sdk / "owned.txt").write_text("owned\n", encoding="utf-8")
    monkeypatch.setattr(
        uninstaller.install_support,
        "_managed_thinkthread_sdk_source",
        lambda _path: ROOT.resolve(),
    )
    plan = uninstaller.UninstallPlan(
        codex_executable=None,
        remove_codex_plugin=False,
        remove_codex_marketplace=False,
        pi_executable=None,
        pi_sources=(),
        pi_dev_dir=None,
        pi_dev_registry_path=None,
        remove_runtime=False,
        notes=(),
        goal_plus_thinkthread_sdk_path=sdk,
    )

    uninstaller._execute(plan, dry_run=False)

    assert not sdk.exists()
