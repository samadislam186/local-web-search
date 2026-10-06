from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from search_engine.filesystem import DataDirectories, DataRootError, DiskSpaceError


def test_data_directories_own_only_configured_root_and_clean_temporary_work(tmp_path: Path) -> None:
    directories = DataDirectories(tmp_path / "data")
    directories.ensure_exists()
    abandoned = directories.temporary / "batch" / "partial.json"
    abandoned.parent.mkdir()
    abandoned.write_text("partial", encoding="utf-8")

    assert directories.resolve_owned_path(Path("artifacts/index.shard")) == directories.artifacts / "index.shard"
    assert directories.clean_interrupted_temporary_work(now=datetime.now(UTC)) == (abandoned,)
    assert not abandoned.exists()
    with pytest.raises(DataRootError):
        directories.resolve_owned_path(Path("..") / "outside")


def test_data_directories_enforce_reserve_and_validate_cleanup_age(tmp_path: Path) -> None:
    directories = DataDirectories(tmp_path / "data", minimum_free_bytes=10**30)
    with pytest.raises(DiskSpaceError, match="insufficient disk space"):
        directories.ensure_capacity(1)
    with pytest.raises(ValueError, match="older_than"):
        directories.clean_interrupted_temporary_work(older_than=timedelta(seconds=-1))
