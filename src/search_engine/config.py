"""Small, validated TOML configuration loader."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .engine.errors import ConfigurationError
from .engine.filesystem import DataDirectories


@dataclass(frozen=True, slots=True)
class ShardConfig:
    target_size_mb: int
    max_active_shards: int
    max_active_bytes: int

    def __post_init__(self) -> None:
        if min(self.target_size_mb, self.max_active_shards, self.max_active_bytes) <= 0:
            raise ConfigurationError("shard limits must be positive")
        if self.target_size_mb * 1024 * 1024 > self.max_active_bytes:
            raise ConfigurationError("a target shard cannot exceed the active-byte budget")


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    sqlite_path: Path
    user_agent: str

    def __post_init__(self) -> None:
        if not self.user_agent.strip():
            raise ConfigurationError("crawl.user_agent must not be empty")


@dataclass(frozen=True, slots=True)
class StorageConfig:
    data_root: Path
    minimum_free_bytes: int
    temporary_retention_seconds: int

    def __post_init__(self) -> None:
        if self.minimum_free_bytes < 0 or self.temporary_retention_seconds < 0:
            raise ConfigurationError("storage limits must not be negative")

    def data_directories(self) -> DataDirectories:
        """Return the owned data-root boundary configured for this deployment."""
        return DataDirectories(self.data_root, minimum_free_bytes=self.minimum_free_bytes)

    @property
    def temporary_retention(self) -> timedelta:
        """Return the age at which interrupted owned work may be cleaned."""
        return timedelta(seconds=self.temporary_retention_seconds)


@dataclass(frozen=True, slots=True)
class AppConfig:
    profile: str
    shards: ShardConfig
    crawl: CrawlConfig
    storage: StorageConfig

    def prepare_storage(self) -> tuple[Path, ...]:
        """Create configured directories and recover only stale owned scratch files."""
        directories = self.storage.data_directories()
        directories.ensure_exists()
        return directories.clean_interrupted_temporary_work(
            older_than=self.storage.temporary_retention
        )


_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def load_config(default_path: Path, profile_path: Path | None = None) -> AppConfig:
    """Load defaults then shallow-merge named sections from an optional profile."""
    base = _read_toml(default_path)
    if profile_path is not None:
        override = _read_toml(profile_path)
        for section, values in override.items():
            if isinstance(values, dict) and isinstance(base.get(section), dict):
                base[section].update(values)
            else:
                base[section] = values
    try:
        return AppConfig(
            profile=str(base["runtime"]["profile"]),
            shards=ShardConfig(**base["shards"]),
            crawl=CrawlConfig(
                sqlite_path=_resolve_config_path(default_path.parent, str(base["crawl"]["sqlite_path"])),
                user_agent=str(base["crawl"]["user_agent"]),
            ),
            storage=StorageConfig(
                data_root=_resolve_config_path(default_path.parent, str(base["storage"]["data_root"])),
                minimum_free_bytes=int(base["storage"]["minimum_free_bytes"]),
                temporary_retention_seconds=int(base["storage"]["temporary_retention_seconds"]),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid configuration: {exc}") from exc


def load_profile(config_root: Path, profile: str) -> AppConfig:
    """Load the portable default configuration with a named safe profile overlay."""
    if not _PROFILE_NAME.fullmatch(profile):
        raise ConfigurationError(f"invalid configuration profile: {profile!r}")
    return load_config(config_root / "default.toml", config_root / "profiles" / f"{profile}.toml")


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc


def _resolve_config_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()
