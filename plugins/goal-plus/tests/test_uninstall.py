from __future__ import annotations

import importlib.util
import json
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


def test_codex_plan_removes_only_current_checkout(
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


def test_codex_plan_refuses_registration_from_another_checkout(
    uninstaller: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        uninstaller.install_support,
        "_codex_inventory",
        lambda _codex: [
            {
                "pluginId": "goal-plus@goal-plus",
                "source": {"path": "/tmp/other-goal-plus"},
            }
        ],
    )
    monkeypatch.setattr(
        uninstaller.install_support,
        "_codex_marketplaces",
        lambda _codex: {},
    )

    with pytest.raises(uninstaller.UninstallError, match="another checkout|belongs"):
        uninstaller._codex_plan("codex")


def test_pi_sources_remove_only_current_checkout(
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
        ],
    )

    sources, notes = uninstaller._pi_sources("pi")

    assert sources == (str(ROOT),)
    assert notes == [
        "Preserved Pi registration from another source: /tmp/other-goal-plus"
    ]


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
