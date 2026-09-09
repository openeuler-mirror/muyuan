from goal_plus.models import ArtifactRef
from goal_plus.infrastructure.artifacts.reader import thinkthread_artifact_view


def test_thinkthread_artifact_view_is_an_enforced_snapshot_handle() -> None:
    access = thinkthread_artifact_view(
        ArtifactRef(
            kind="fs_snapshot",
            id="fsnap-evidence-1",
            provider="thinkthread",
        ),
    )

    assert access.kind == "provider_tools"
    assert access.root is None
    assert access.readonly_contract == "enforced"
    assert access.provider_handle["snapshot_id"] == "fsnap-evidence-1"
    assert "fs.snapshot.readdir" in access.provider_handle["operations"]
    assert "fs.snapshot.ls" not in access.provider_handle["operations"]
    assert "fs.snapshot.pread" in access.provider_handle["operations"]
    assert "fs.run.discard_writes" in access.provider_handle["operations"]
