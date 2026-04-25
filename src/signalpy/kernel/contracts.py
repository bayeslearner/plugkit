"""Contracts — Protocol interfaces for platform services.

These are just Protocols (structural typing).  Platform components
provide implementations.  Apps @require them by contract name.

Contracts are NOT the kernel — they're the vocabulary.  But they
ship with the kernel so there's a common language.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IConfig(Protocol):
    """Layered configuration."""
    def get(self, key: str, default: Any = None) -> Any: ...


@runtime_checkable
class ICredentials(Protocol):
    """Per-component scoped credential resolver."""
    def get(self, key: str, *, target: str | None = None) -> str: ...
    def for_target(self, target: str) -> dict[str, str]: ...
    def list_targets(self) -> list[str]: ...


@runtime_checkable
class IStorage(Protocol):
    """Object storage (S3-compatible interface)."""
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes | None: ...
    async def list(self, prefix: str = "") -> list[str]: ...
    async def delete(self, key: str) -> None: ...


@runtime_checkable
class IAuth(Protocol):
    """Authentication and authorization."""
    def authenticate(self, token: str) -> dict[str, Any]: ...
    def authorize(self, identity: dict, action: str) -> bool: ...


@runtime_checkable
class ILogger(Protocol):
    """Structured logger."""
    def info(self, msg: str, **kwargs: Any) -> None: ...
    def warning(self, msg: str, **kwargs: Any) -> None: ...
    def error(self, msg: str, **kwargs: Any) -> None: ...
    def debug(self, msg: str, **kwargs: Any) -> None: ...


@runtime_checkable
class ITracer(Protocol):
    """Distributed tracing."""
    def span(self, name: str, **attributes: Any) -> Any: ...


@runtime_checkable
class IWorkspace(Protocol):
    """Workspace paths and settings."""
    @property
    def root(self) -> Path: ...
    @property
    def settings(self) -> dict[str, Any]: ...


@runtime_checkable
class IConfigAdmin(Protocol):
    """Configuration administration — push updates to managed services."""
    def get_configuration(self, pid: str) -> dict[str, Any]: ...
    def update(self, pid: str, properties: dict[str, Any]) -> None: ...
    def delete(self, pid: str) -> None: ...


@runtime_checkable
class IManagedService(Protocol):
    """A service that accepts configuration updates."""
    def updated(self, properties: dict[str, Any] | None) -> None: ...


@runtime_checkable
class IManagedServiceFactory(Protocol):
    """A factory that manages multiple configured instances."""
    def updated(self, pid: str, properties: dict[str, Any]) -> None: ...
    def deleted(self, pid: str) -> None: ...
