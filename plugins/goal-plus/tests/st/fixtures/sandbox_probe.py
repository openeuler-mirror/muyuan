"""Run from a real candidate shell against disposable ST-owned outside paths."""

import argparse
import errno
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outside", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    scratch = Path.cwd() / ".tmp"
    scratch.mkdir(exist_ok=True)
    inside = scratch / "sandbox-inside.txt"
    inside.write_text("ok")
    with tempfile.TemporaryFile() as temporary:
        temporary.write(b"ok")
        temporary.seek(0)
        assert temporary.read() == b"ok"
    link = scratch / "sandbox-link"
    link.symlink_to(args.outside)
    denied = {}
    try:
        for name, path in (("outside", args.outside), ("source", args.source), ("symlink", link)):
            try:
                with path.open("ab") as stream:
                    stream.write(b"# sandbox probe escaped\n")
            except OSError as exc:
                denied[name] = exc.errno
            else:
                raise AssertionError(f"sandbox allowed {name} write")
        code = (
            "import sys\n"
            "try:\n"
            "    with open(sys.argv[1], 'ab') as f: f.write(b'escaped child')\n"
            "except OSError as e: print(e.errno)\n"
            "else: raise SystemExit('child write escaped')\n"
        )
        child = subprocess.run([sys.executable, "-c", code, str(args.outside)],
                               text=True, capture_output=True, check=True, timeout=10)
        denied["child"] = int(child.stdout)
        assert all(value in {errno.EROFS, errno.EACCES, errno.EPERM} for value in denied.values()), denied
        receipt = {
            "workspace": str(Path.cwd()), "inside_write": inside.read_text(), "denied": denied,
            "temporary_write": "ok", "temporary_directory": tempfile.gettempdir(),
        }
        (scratch / "sandbox-probe.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps(receipt))
    finally:
        link.unlink()


if __name__ == "__main__":
    main()
