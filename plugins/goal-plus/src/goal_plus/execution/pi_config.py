"""Copy Pi configuration into a sandbox-owned home, separate from session files.

Pi startup migrates loose JSONL histories out of its configuration home.
"""

import shutil
from pathlib import Path


def prepare_pi_home(directory: Path, environment: dict[str, str]) -> None:
    source = environment.get("PI_CODING_AGENT_DIR")
    source_dir = Path(source).expanduser() if source else Path.home() / ".pi/agent"
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "models.json", "models-store.json", "settings.json"):
        source_file = source_dir / name
        target_file = directory / name
        if source_file.is_file() and not target_file.exists():
            shutil.copyfile(source_file, target_file)
            target_file.chmod(0o600)
    environment["PI_CODING_AGENT_DIR"] = str(directory)
