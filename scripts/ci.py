"""Run the deterministic, CPU-only local quality gate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from search_engine.engine.policy import assert_safe_source_tree  # noqa: E402


def main() -> int:
    """Check serialization policy, formatting/lint, and the full test suite."""
    assert_safe_source_tree(ROOT / "src")
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    commands = (
        (sys.executable, "-m", "ruff", "check", "src", "tests"),
        (sys.executable, "-m", "pytest", "--basetemp", ".pytest-tmp-ci"),
    )
    for command in commands:
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
