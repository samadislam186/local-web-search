import os
from pathlib import Path

import pytest
from search_engine.errors import ConfigurationError

from search_engine.config import load_config, load_profile


def test_named_profiles_override_defaults_and_resolve_data_path() -> None:
    root = Path("config").resolve()
    tiny = load_profile(root, "tiny")
    laptop = load_profile(root, "laptop")
    server = load_profile(root, "server")
    assert (tiny.profile, tiny.shards.max_active_shards) == ("tiny", 2)
    assert laptop.shards.max_active_shards == 10
    assert server.shards.max_active_bytes > laptop.shards.max_active_bytes
    assert tiny.crawl.sqlite_path == root / "data/mutable/crawl.sqlite3"
    assert tiny.storage.data_root == root / "data"
    assert tiny.storage.temporary_retention.total_seconds() == 86400


def test_invalid_profile_and_invalid_limits_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="profile"):
        load_profile(tmp_path, "../server")
    default = tmp_path / "default.toml"
    default.write_text(
        "[runtime]\nprofile='bad'\n[shards]\ntarget_size_mb=2\nmax_active_shards=1\nmax_active_bytes=1\n[crawl]\nsqlite_path='state.db'\nuser_agent='bot'\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="target shard"):
        load_config(default)


def test_prepare_storage_creates_owned_paths_and_recovers_only_expired_work(tmp_path: Path) -> None:
    default = tmp_path / "default.toml"
    default.write_text(
        "[runtime]\nprofile='test'\n"
        "[storage]\ndata_root='data'\nminimum_free_bytes=0\ntemporary_retention_seconds=60\n"
        "[shards]\ntarget_size_mb=1\nmax_active_shards=1\nmax_active_bytes=1048576\n"
        "[crawl]\nsqlite_path='data/mutable/state.sqlite3'\nuser_agent='bot'\n",
        encoding="utf-8",
    )
    config = load_config(default)
    stale = config.storage.data_directories().temporary / "stale.tmp"
    stale.parent.mkdir(parents=True)
    stale.write_text("interrupted", encoding="utf-8")
    os.utime(stale, (0, 0))

    assert config.prepare_storage() == (stale,)
    directories = config.storage.data_directories()
    assert directories.mutable.is_dir()
    assert directories.artifacts.is_dir()
    assert not stale.exists()
