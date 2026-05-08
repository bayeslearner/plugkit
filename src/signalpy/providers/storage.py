"""StorageProvider — pluggable object storage as a component.

Provides: IStorage
Requires: IConfig

Default: local filesystem under workspace/storage/.
Swap for MinIO/S3 by providing a different IStorage component.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle


@component("storage", version="0.1")
@provides("IStorage")
@requires(config="IConfig")
class StorageProvider:
    """Local filesystem object storage.

    Objects stored at: <root>/<prefix>/<key>
    Each component gets its own prefix (scoped by kernel).
    """

    @lifecycle.activate
    def activate(self, rt):
        root = rt.config.get("storage.root", ".storage")
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _safe_path(self, key: str, prefix: str = "") -> Path:
        """Resolve a storage path, rejecting traversal attempts."""
        path = (self._root / prefix / key).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError(f"Path traversal rejected: {key!r}")
        return path

    async def put(self, key: str, data: bytes, prefix: str = "") -> None:
        path = self._safe_path(key, prefix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str, prefix: str = "") -> bytes | None:
        path = self._safe_path(key, prefix)
        if path.is_file():
            return path.read_bytes()
        return None

    async def list(self, prefix: str = "") -> list[str]:
        base = self._safe_path("", prefix)
        if not base.is_dir():
            return []
        return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())

    async def delete(self, key: str, prefix: str = "") -> None:
        path = self._safe_path(key, prefix)
        if path.is_file():
            path.unlink()

    @lifecycle.deactivate
    def deactivate(self, rt):
        pass  # flush/close if needed
