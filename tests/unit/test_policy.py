from pathlib import Path

import pytest

from search_engine.engine.policy import (
    UnsafeSerializationPolicyError,
    assert_safe_source_tree,
    prohibited_deserializer_imports,
)


def test_safe_source_tree_allows_ordinary_python(tmp_path: Path) -> None:
    source = tmp_path / "safe.py"
    source.write_text("import json\n", encoding="utf-8")

    assert prohibited_deserializer_imports((tmp_path,)) == ()
    assert_safe_source_tree(tmp_path)


@pytest.mark.parametrize("statement", ("import pickle", "from joblib import load"))
def test_safe_source_tree_rejects_executable_deserializers(tmp_path: Path, statement: str) -> None:
    source = tmp_path / "unsafe.py"
    source.write_text(f"{statement}\n", encoding="utf-8")

    with pytest.raises(UnsafeSerializationPolicyError, match="Prohibited executable"):
        assert_safe_source_tree(tmp_path)


def test_project_source_tree_satisfies_serialization_policy() -> None:
    assert_safe_source_tree(Path("src/search_engine"))
