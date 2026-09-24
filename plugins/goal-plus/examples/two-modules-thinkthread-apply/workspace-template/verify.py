import json
from pathlib import Path


def read_value(path: str) -> int:
    text = Path(path).read_text(encoding="utf-8").strip()
    prefix = "VALUE ="
    if not text.startswith(prefix):
        raise ValueError(f"{path} must define VALUE")
    return int(text[len(prefix) :].strip())


module_a = read_value("modules/module_a.py")
module_b = read_value("modules/module_b.py")
valid = module_a == 7 and module_b == 11
print(
    json.dumps(
        {
            "module_score": int(module_a == 7) + int(module_b == 11),
            "module_a": module_a,
            "module_b": module_b,
            "outcome": "valid" if valid else "invalid",
            "valid": valid,
        },
        sort_keys=True,
    )
)
