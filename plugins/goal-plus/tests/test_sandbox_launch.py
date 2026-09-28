"""Focused tests for the shared native-process sandbox resolver."""

import errno
import json
import os
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import replace
import subprocess
import sys
from pathlib import Path

import pytest

from goal_plus.execution.sandbox import SandboxUnavailable, resolve_sandbox_command
from goal_plus.ports.sandbox import ProcessSandbox



@pytest.fixture
def outer_backend():
    if sys.platform == "darwin":
        backend = "/usr/bin/sandbox-exec"
    elif sys.platform.startswith("linux"):
        backend = "bwrap"
    else:
        pytest.skip("requires macOS or Linux")
    if shutil.which(backend) is None:
        pytest.skip(f"{backend} is not installed")
    return backend


@pytest.fixture
def sandbox_root(outer_backend):
    # Keep source sentinels outside Linux's private /tmp: a write to a private
    # namespace copy is permitted and must not be confused with a host write.
    base = Path(__file__).resolve().parents[1] / ".tmp"
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sandbox-test-", dir=base) as directory:
        yield Path(directory)


def _sandbox(tmp_path: Path, *, mode: str = "workspace-write", backend: str = "outer") -> ProcessSandbox:
    return ProcessSandbox(
        mode=mode,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        workspace=tmp_path / "workspace",
        writable_roots=(tmp_path / "runtime",),
    )


def test_linux_outer_sandbox_mounts_workspace_and_keeps_json_command_intact(tmp_path: Path) -> None:
    command = resolve_sandbox_command(
        ("pi", "--mode", "rpc"),
        cwd=tmp_path / "workspace",
        sandbox=_sandbox(tmp_path),
        platform="linux",
        which=lambda name: f"/usr/bin/{name}",
    )
    assert command[:2] == ("/usr/bin/bwrap", "--die-with-parent")
    assert command[command.index("--ro-bind"):command.index("--ro-bind") + 3] == ("--ro-bind", "/", "/")
    bind_index = command.index("--bind", command.index("--ro-bind") + 1)
    assert command[bind_index:bind_index + 3] == ("--bind", str(tmp_path / "workspace"), str(tmp_path / "workspace"))
    assert command[-4:] == ("--", "pi", "--mode", "rpc")


def test_macos_outer_sandbox_denies_writes_outside_declared_roots(tmp_path: Path) -> None:
    command = resolve_sandbox_command(
        ("pi", "--mode", "rpc"),
        cwd=tmp_path / "workspace",
        sandbox=_sandbox(tmp_path),
        platform="darwin",
        which=lambda name: "/usr/bin/sandbox-exec",
    )
    assert command[:2] == ("/usr/bin/sandbox-exec", "-p")
    profile = command[2]
    assert "deny file-write*" in profile
    assert str((tmp_path / "workspace").resolve()) in profile
    assert command[-3:] == ("pi", "--mode", "rpc")


def test_native_backend_does_not_wrap_the_agent_command(tmp_path: Path) -> None:
    command = ("codex", "app-server", "--stdio")
    assert resolve_sandbox_command(
        command,
        cwd=tmp_path / "workspace",
        sandbox=_sandbox(tmp_path, backend="native"),
        platform="linux",
        which=lambda name: None,
    ) == command


@pytest.mark.parametrize(("platform", "backend"), [("linux", "bubblewrap"), ("darwin", "sandbox-exec")])
def test_required_outer_backend_fails_closed_when_unavailable(tmp_path: Path, platform, backend) -> None:
    with pytest.raises(SandboxUnavailable, match=backend):
        resolve_sandbox_command(
            ("pi", "--mode", "rpc"),
            cwd=tmp_path / "workspace",
            sandbox=_sandbox(tmp_path),
            platform=platform,
            which=lambda name: None,
        )


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_outer_sandbox_requires_cwd_in_a_declared_root(tmp_path: Path, platform) -> None:
    with pytest.raises(ValueError, match="outside its declared roots"):
        resolve_sandbox_command(
            ("pi", "--mode", "rpc"),
            cwd=tmp_path / "outside",
            sandbox=_sandbox(tmp_path),
            platform=platform,
            which=lambda name: "/usr/bin/bwrap",
        )


@pytest.mark.host_process
def test_outer_sandbox_keeps_outside_files_read_only(sandbox_root: Path) -> None:
    tmp_path = sandbox_root
    workspace = tmp_path / "workspace"
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside"
    workspace.mkdir()
    runtime.mkdir()
    outside.mkdir()
    target = outside / "probe.txt"
    target.write_text("before", encoding="utf-8")
    sandbox = ProcessSandbox(
        mode="workspace-write", backend="outer", workspace=workspace,
        writable_roots=(runtime,),
    )
    outside_command = resolve_sandbox_command(
        (sys.executable, "-c", f"from pathlib import Path; Path({str(target)!r}).write_text('after')"),
        cwd=workspace, sandbox=sandbox,
    )
    outside_result = subprocess.run(
        outside_command, cwd=workspace, capture_output=True, text=True, check=False,
    )
    assert outside_result.returncode != 0
    assert target.read_text(encoding="utf-8") == "before"

    allowed = workspace / "allowed.txt"
    allowed_command = resolve_sandbox_command(
        (sys.executable, "-c", f"from pathlib import Path; Path({str(allowed)!r}).write_text('ok')"),
        cwd=workspace, sandbox=sandbox,
    )
    allowed_result = subprocess.run(
        allowed_command, cwd=workspace, capture_output=True, text=True, check=False,
    )
    assert allowed_result.returncode == 0, allowed_result.stderr
    assert allowed.read_text(encoding="utf-8") == "ok"


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
@pytest.mark.parametrize('mode', ['read-only', 'workspace-write'])
def test_outer_policy_rejects_root_and_resolves_symlink_cwd(tmp_path, platform, mode):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(workspace, target_is_directory=True)
    sandbox = ProcessSandbox(mode=mode, backend='outer', workspace=workspace)
    resolve_sandbox_command(('true',), cwd=alias, sandbox=sandbox, platform=platform,
                            which=lambda name: name)
    with pytest.raises(ValueError, match='filesystem root'):
        resolve_sandbox_command(('true',), cwd=workspace,
                                sandbox=replace(sandbox, writable_roots=(Path('/'),)),
                                platform=platform, which=lambda name: name)


@pytest.mark.parametrize('fallback', [False, True])
@pytest.mark.host_process
def test_outer_scratch_write_boundary_and_cleanup(sandbox_root, monkeypatch, fallback):
    tmp_path = sandbox_root
    from goal_plus.execution import sandbox as module

    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('before')
    # Keep the host sentinel in /tmp too: neither backend should authorize all of it.
    with tempfile.TemporaryDirectory(prefix='gp-outside-', dir='/tmp') as host_tmp:
        host_target = Path(host_tmp) / 'outside'
        host_target.write_text('before')
        if fallback:
            original = module.tempfile.TemporaryDirectory
            def create(*args, **kwargs):
                if kwargs.get('dir') == '/tmp':
                    raise PermissionError(errno.EACCES, 'test: system tmp unavailable')
                return original(*args, **kwargs)
            monkeypatch.setattr(module.tempfile, 'TemporaryDirectory', create)
        environment = dict(os.environ, TMPDIR='/nonexistent/inherited-temp')
        policy = ProcessSandbox(mode='read-only', backend='outer', workspace=workspace,
                                writable_roots=(runtime,))
        with ExitStack() as cleanup:
            policy = module.prepare_sandbox_environment(policy, cwd=workspace,
                                                        environment=environment, cleanup=cleanup)
            scratch = Path(environment['TMPDIR'])
            assert {environment[k] for k in ('TMPDIR', 'TMP', 'TEMP')} == {str(scratch)}
            assert scratch.parent == ((workspace / '.gp/.tmp') if fallback else Path('/tmp').resolve())
            script = '''import json,sys,tempfile
from pathlib import Path
with tempfile.TemporaryFile() as f: f.write(b'ok')
r={}
for name, path in zip(('workspace','outside','runtime'), sys.argv[1:]):
 try: Path(path).write_text('after'); r[name]='writable'
 except OSError as e: r[name]=e.errno
print(json.dumps(r))
'''
            command = resolve_sandbox_command(
                (sys.executable, '-c', script, str(workspace/'readonly'), str(outside), str(runtime/'allowed')),
                cwd=workspace, sandbox=policy)
            result = subprocess.run(command, cwd=workspace, env=environment, capture_output=True,
                                    text=True, timeout=15)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {'workspace': errno.EPERM if sys.platform == 'darwin' else errno.EROFS,
                                                'outside': errno.EPERM if sys.platform == 'darwin' else errno.EROFS,
                                                'runtime': 'writable'}
            # Global host tmp is denied on macOS and hidden on Linux.
            command = resolve_sandbox_command((sys.executable, '-c',
                'import sys; from pathlib import Path; Path(sys.argv[1]).write_text("escaped")', str(host_target)),
                cwd=workspace, sandbox=policy)
            result = subprocess.run(command, cwd=workspace, env=environment, capture_output=True,
                                    text=True, timeout=15)
            assert result.returncode != 0
            assert host_target.read_text() == 'before'
        assert not scratch.exists()
        assert outside.read_text() == 'before'


@pytest.mark.host_process
def test_outer_process_denies_symlink_and_child_writes(sandbox_root):
    from goal_plus.execution.sandbox import prepare_sandbox_environment
    tmp_path = sandbox_root
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    outside = tmp_path / 'outside'
    source = tmp_path / 'source'
    outside.write_text('before')
    source.write_text('before')
    probe = Path(__file__).parent / 'st/fixtures/sandbox_probe.py'
    policy = ProcessSandbox(mode='workspace-write', backend='outer', workspace=workspace)
    environment = dict(os.environ)
    with ExitStack() as cleanup:
        policy = prepare_sandbox_environment(policy, cwd=workspace, environment=environment, cleanup=cleanup)
        command = resolve_sandbox_command((sys.executable, str(probe), '--outside', str(outside),
                                           '--source', str(source)), cwd=workspace, sandbox=policy)
        result = subprocess.run(command, cwd=workspace, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert outside.read_text() == source.read_text() == 'before'


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_network_denial_is_explicit_and_system_seatbelt_is_used(tmp_path, platform):
    sandbox = replace(_sandbox(tmp_path), network='deny')
    lookups = []
    def which(name):
        lookups.append(name)
        return name
    command = resolve_sandbox_command(('true',), cwd=sandbox.workspace, sandbox=sandbox,
                                       platform=platform, which=which)
    assert lookups == (['/usr/bin/sandbox-exec'] if platform == 'darwin' else ['bwrap'])
    assert ('(deny network*)' in command[2]) if platform == 'darwin' else ('--unshare-net' in command)


def test_scratch_unexpected_io_error_does_not_fallback(tmp_path, monkeypatch):
    from goal_plus.execution import sandbox as module
    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, 'test: no disk space')
    monkeypatch.setattr(module.tempfile, 'TemporaryDirectory', fail)
    with ExitStack() as cleanup, pytest.raises(OSError) as error:
        module.prepare_sandbox_environment(_sandbox(tmp_path), cwd=tmp_path/'workspace',
                                           environment={}, cleanup=cleanup)
    assert error.value.errno == errno.ENOSPC
    assert not (tmp_path/'workspace/.gp').exists()


@pytest.mark.host_process
@pytest.mark.parametrize('reject_registration', [False, True])
def test_rpc_sandbox_scratch_lasts_until_close_and_cleans_failed_launch(tmp_path, outer_backend, monkeypatch, reject_registration):
    from goal_plus.execution import session_rpc
    from goal_plus.ports.process import ProcessRegistration
    from goal_plus.ports.session import SessionProcessInput
    scratch_paths = []
    original = session_rpc.prepare_sandbox_environment
    def prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        scratch_paths.append(Path(kwargs['environment']['TMPDIR']))
        return result
    monkeypatch.setattr(session_rpc, 'prepare_sandbox_environment', prepare)
    finished = []
    def register(pid):
        if reject_registration:
            raise PermissionError('scope closed')
        return 'token'
    script = '''import json,os,sys,tempfile
for line in sys.stdin:
 request=json.loads(line)
 with tempfile.TemporaryFile() as f: f.write(b'ok')
 print(json.dumps({'id':request['id'],'result':os.environ['TMPDIR']}),flush=True)
'''
    environment = dict(os.environ)
    process = SessionProcessInput((sys.executable, '-c', script), tmp_path, environment,
                                  ProcessSandbox(mode='read-only', backend='outer', workspace=tmp_path))
    registration = ProcessRegistration(register, finished.append)
    if reject_registration:
        with pytest.raises(PermissionError, match='scope closed'):
            session_rpc.SessionRpcProcess(process, registration, lambda _: None)
    else:
        rpc = session_rpc.SessionRpcProcess(process, registration, lambda _: None)
        try:
            result = rpc.request({'method':'temporary'}, timeout=5)
            assert result['result'] == str(scratch_paths[0])
            assert scratch_paths[0].is_dir()
        finally:
            rpc.close()
        assert finished == ['token']
    assert scratch_paths and not scratch_paths[0].exists()
    assert environment == dict(os.environ)


def test_linux_private_tmp_preserves_read_only_workspace(tmp_path):
    policy = ProcessSandbox(mode='read-only', backend='outer', workspace=tmp_path)
    command = resolve_sandbox_command(('true',), cwd=tmp_path, sandbox=policy,
                                       platform='linux', which=lambda name: name)
    tmpfs_index = command.index('--tmpfs')
    workspace_bind = command.index('--ro-bind', tmpfs_index)
    assert command[workspace_bind:workspace_bind + 3] == ('--ro-bind', str(tmp_path), str(tmp_path))
