"""Static repository policies used by the deterministic local CI command."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path

from .errors import SearchEngineError

PROHIBITED_DESERIALIZER_MODULES = frozenset({"dill", "joblib", "marshal", "pickle", "shelve"})


class UnsafeSerializationPolicyError(SearchEngineError):
    """Raised when source code imports an executable deserialization module."""


def prohibited_deserializer_imports(paths: Iterable[Path]) -> tuple[str, ...]:
    """Return stable locations of prohibited imports below files or directories."""
    source_files = sorted(
        {
            file
            for path in paths
            for file in (path.rglob("*.py") if path.is_dir() else (path,))
            if file.suffix == ".py"
        },
        key=lambda file: str(file),
    )
    findings: list[str] = []
    for source_file in source_files:
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        for node in ast.walk(tree):
            module_names: tuple[str, ...]
            if isinstance(node, ast.Import):
                module_names = tuple(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                module_names = (node.module.split(".", 1)[0],)
            else:
                continue
            for module_name in module_names:
                if module_name in PROHIBITED_DESERIALIZER_MODULES:
                    findings.append(f"{source_file}:{node.lineno}: {module_name}")
    return tuple(findings)


def assert_safe_source_tree(root: Path) -> None:
    """Reject source trees that depend on executable artifact deserializers."""
    findings = prohibited_deserializer_imports((root,))
    if findings:
        raise UnsafeSerializationPolicyError(
            "Prohibited executable deserializer imports: " + "; ".join(findings)
        )
