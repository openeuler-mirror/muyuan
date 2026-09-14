"""Emit an apply_patch payload for ordinary Torch helpers from a trusted checkout."""

import argparse
import ast
import contextlib
import dataclasses
import dis
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import shlex
import subprocess
import sys
import textwrap
import types


def code_names(code):
    yield from (item.argval for item in dis.get_instructions(code) if item.opname == "LOAD_GLOBAL")
    for item in code.co_consts:
        if isinstance(item, types.CodeType):
            yield from code_names(item)


def literal(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = dataclasses.asdict(value)
        for name, prop in vars(type(value)).items():
            if isinstance(prop, property):
                fields[name] = getattr(value, name)
        return f"__import__('types').SimpleNamespace(**{literal(fields)})"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{literal(k)}: {literal(v)}" for k, v in value.items()) + "}"
    expression = repr(value)
    ast.literal_eval(expression)
    return expression


class Extractor:
    def __init__(self, source_dir):
        self.source_dir = source_dir
        self.parts = {}
        self.seen = set()

    def is_local(self, module):
        return bool(getattr(module, "__file__", None)) and Path(module.__file__).resolve().parent == self.source_dir

    def add(self, module, name):
        key = module.__name__, name
        if key in self.seen:
            return
        self.seen.add(key)
        parts = self.parts.setdefault(module.__name__, [])
        value = getattr(module, name)
        if inspect.isfunction(value):
            owner = importlib.import_module(value.__module__)
            if owner is not module:
                if self.is_local(owner):
                    self.add(owner, value.__name__)
                prefix = "." if self.is_local(owner) else ""
                parts.append(f"from {prefix}{owner.__name__} import {value.__name__} as {name}")
                return
            node = ast.parse(textwrap.dedent(inspect.getsource(value))).body[0]
            if node.decorator_list:
                raise ValueError(f"Review decorated reference before extraction: {key}")
            signature_names = {item.id for item in ast.walk(node.args) if isinstance(item, ast.Name)}
            if node.returns is not None:
                signature_names.update(item.id for item in ast.walk(node.returns) if isinstance(item, ast.Name))
            for dependency in sorted(set(code_names(value.__code__)) | signature_names):
                if dependency in value.__globals__ and dependency != name:
                    self.add(module, dependency)
            # Local imports remain local to the extracted oracle package.
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module and (self.source_dir / f"{child.module}.py").is_file():
                    if child.level or any(alias.name == "*" for alias in child.names):
                        raise ValueError(f"Review relative/star import in {key}")
                    imported = importlib.import_module(child.module)
                    for alias in child.names:
                        self.add(imported, alias.name)
                    child.level = 1
                elif isinstance(child, ast.Import):
                    for alias in child.names:
                        if (self.source_dir / f"{alias.name}.py").is_file():
                            raise ValueError(f"Review local module import in {key}: {alias.name}")
            parts.append(ast.unparse(node))
        elif isinstance(value, types.ModuleType):
            if self.is_local(value):
                raise ValueError(f"Review module-valued dependency: {key}")
            parts.append(f"import {value.__name__} as {name}")
        elif inspect.isclass(value) and not self.is_local(importlib.import_module(value.__module__)):
            parts.append(f"from {value.__module__} import {value.__name__} as {name}")
        elif getattr(value, "__module__", None) == "typing":
            parts.append(f"from typing import {getattr(value, '_name', None) or value.__name__} as {name}")
        else:
            try:
                parts.append(f"{name} = {literal(value)}")
            except (ValueError, SyntaxError, TypeError) as exc:
                raise ValueError(f"Unsupported dependency {key}: {type(value).__name__}") from exc

    def files(self):
        result = {"oracle/__init__.py": ""}
        for module, parts in self.parts.items():
            source = (self.source_dir / f"{module}.py").read_text()
            header = []
            for line in source.splitlines():
                if not line.startswith("#"):
                    break
                header.append(line)
            result[f"oracle/{module}.py"] = "\n".join(header) + "\n\n" + "\n\n".join(parts) + "\n"
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "reference", "inputs", "entry", "output", "license"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--specialize", default="")
    parser.add_argument("--source-url")
    args = parser.parse_args()
    source, output = Path(args.source).resolve(), Path(args.output).resolve()
    if output.exists():
        parser.error("output must be a new directory")
    sys.path.insert(0, str(source.parent))
    sys.argv = [str(source), *shlex.split(args.specialize)]
    with contextlib.redirect_stdout(sys.stderr):
        module = importlib.import_module(source.stem)
    from pypto.language.typing.dynamic import DynVar
    extractor = Extractor(source.parent)
    for name in (args.reference, args.inputs):
        extractor.add(module, name)
    files = extractor.files()
    tree = ast.parse(source.read_text())
    entry = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == args.entry)
    config = ["import pypto.language as pl"]
    for name in sorted({node.id for node in ast.walk(entry.args) if isinstance(node, ast.Name)} - {"pl"}):
        value = getattr(module, name)
        expression = f"pl.dynamic({value.name!r})" if isinstance(value, DynVar) else literal(value)
        config.append(f"{name} = {expression}")
    entry.body = [ast.Raise(exc=ast.Call(func=ast.Name(id="NotImplementedError", ctx=ast.Load()), args=[ast.Constant("Implement this kernel in PyPTO")], keywords=[]))]
    files["kernel.py"] = "import pypto.language as pl\nfrom config import *\n\n" + ast.unparse(ast.fix_missing_locations(entry)) + "\n"
    files["config.py"] = "\n".join(config) + "\n"
    files["reference.py"] = f"from oracle.{source.stem} import {args.reference}\n"
    files["inputs.py"] = f"from oracle.{source.stem} import {args.inputs}\n"
    files["LICENSE"] = Path(args.license).read_text()
    repo = Path(subprocess.check_output(["git", "-C", str(source.parent), "rev-parse", "--show-toplevel"], text=True).strip())
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    source_files = sorted({Path(item.__file__).resolve() for item in tuple(sys.modules.values()) if item is not None and extractor.is_local(item)})
    files["provenance.json"] = json.dumps({
        "source_url": args.source_url,
        "commit": revision, "source": str(source.relative_to(repo)),
        "specialize": shlex.split(args.specialize), "entry": args.entry,
        "reference": args.reference, "inputs": args.inputs,
        "source_files": {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_files},
        "symbols": sorted(f"{mod}.{name}" for mod, name in extractor.seen),
    }, indent=2) + "\n"
    print("*** Begin Patch")
    for name, content in files.items():
        print(f"*** Add File: {output / name}")
        for line in content.splitlines():
            print("+" + line)
    print("*** End Patch")


if __name__ == "__main__":
    main()
