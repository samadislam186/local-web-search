"""Configured data-root ownership, capacity checks, and temporary-work recovery."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .errors import SearchEngineError


class DiskSpaceError(SearchEngineError):
    """Raised before a configured data root would run out of free capacity."""


class DataRootError(SearchEngineError):
    """Raised for attempts to write outside an owned configured data root."""


@dataclass(frozen=True, slots=True)
class DataDirectories:
    """Owned paths below one configured data root.

    Temporary work belongs only in ``temporary`` and can therefore be safely
    removed after a process interruption without touching published artifacts.
    """

    root: Path
    minimum_free_bytes: int = 0

    def __post_init__(self) -> None:
        if self.minimum_free_bytes < 0:
            raise ValueError("minimum_free_bytes must not be negative")

    @property
    def mutable(self) -> Path:
        return self.root / "mutable"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def temporary(self) -> Path:
        return self.root / "temporary"

    def ensure_exists(self) -> None:
        for directory in (self.mutable, self.artifacts, self.temporary):
            directory.mkdir(parents=True, exist_ok=True)

    def resolve_owned_path(self, relative_path: Path) -> Path:
        """Resolve a relative path and reject traversal or native absolute paths."""
        if relative_path.is_absolute():
            raise DataRootError("data path must be relative to the configured root")
        root = self.root.resolve()
        candidate = (root / relative_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise DataRootError("data path escapes the configured root") from exc
        return candidate

    def available_bytes(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.root).free

    def ensure_capacity(self, required_bytes: int) -> None:
        if required_bytes < 0:
            raise ValueError("required_bytes must not be negative")
        available = self.available_bytes()
        if available - required_bytes < self.minimum_free_bytes:
            raise DiskSpaceError(
                f"insufficient disk space: available={available}, required={required_bytes}, "
                f"reserve={self.minimum_free_bytes}"
            )

    def clean_interrupted_temporary_work(
        self, *, older_than: timedelta = timedelta(0), now: datetime | None = None
    ) -> tuple[Path, ...]:
        """Remove owned temporary files old enough to have survived an interruption."""
        if older_than < timedelta(0):
            raise ValueError("older_than must not be negative")
        if not self.temporary.exists():
            return ()
        timestamp = now or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        threshold = timestamp.timestamp() - older_than.total_seconds()
        removed: list[Path] = []
        for path in sorted(self.temporary.rglob("*"), key=lambda item: (len(item.parts), str(item)), reverse=True):
            if path.is_symlink() or not path.is_file() or path.stat().st_mtime > threshold:
                continue
            path.unlink()
            removed.append(path)
        for path in sorted(self.temporary.rglob("*"), key=lambda item: (len(item.parts), str(item)), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        return tuple(removed)
