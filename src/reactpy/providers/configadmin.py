"""ConfigAdmin — push configuration updates to running components.

iPOPO equivalent: pelix.services.configadmin

Provides: IConfigAdmin
Requires: IConfig

Allows external callers to update the configuration of managed
services at runtime.  Components that want to receive config
updates provide the "managed" protocol and register a PID.

Managed Service Factories can receive multiple configs and
spawn/update/delete instances accordingly.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Protocol, runtime_checkable

from reactpy.kernel import component, provides, requires, lifecycle, runnable
from pydantic import BaseModel

log = logging.getLogger(__name__)


# ── Contracts ──────────────────────────────────────────────────────

@runtime_checkable
class IConfigAdmin(Protocol):
    """Configuration administration service."""
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


# ── Provider ───────────────────────────────────────────────────────

class UpdateParams(BaseModel):
    pid: str
    properties: dict[str, Any] = {}


class DeleteParams(BaseModel):
    pid: str


class GetParams(BaseModel):
    pid: str


@component("configadmin", version="0.1", depends=["config"])
@provides("IConfigAdmin")
@requires(config="IConfig")
class ConfigAdminProvider:
    """Configuration Admin service.

    Manages persistent configurations and distributes them to
    managed services registered with matching PIDs.

    Usage:
        # From any component with rt.invoke:
        await rt.invoke("configadmin.update", {
            "pid": "pretty.printer",
            "properties": {"line_length": 80}
        })
    """

    @lifecycle.activate
    def activate(self):
        self._managed: dict[str, IManagedService] = {}
        self._factories: dict[str, IManagedServiceFactory] = {}
        # Persistence: load from JSON file if configured
        self._persist_path = self.rt.config.get("configadmin.persist_path", "")
        self._configs: dict[str, dict[str, Any]] = self._load_persisted()

    def register_managed(self, pid: str, service: IManagedService) -> None:
        """Register a managed service to receive config for a PID."""
        self._managed[pid] = service
        # Push existing config if any
        if pid in self._configs:
            service.updated(self._configs[pid])

    def register_factory(self, pid: str, factory: IManagedServiceFactory) -> None:
        """Register a managed service factory for a PID."""
        self._factories[pid] = factory

    def get_configuration(self, pid: str) -> dict[str, Any]:
        """Get the current configuration for a PID."""
        return dict(self._configs.get(pid, {}))

    def update(self, pid: str, properties: dict[str, Any]) -> None:
        """Update configuration for a PID and notify managed services."""
        self._configs[pid] = dict(properties)
        self._persist()

        # Notify managed service
        if pid in self._managed:
            try:
                self._managed[pid].updated(properties)
            except Exception:
                log.exception("Managed service update failed: %s", pid)

        # Notify factories — check if pid starts with a factory pid
        for factory_pid, factory in self._factories.items():
            if pid.startswith(factory_pid):
                try:
                    factory.updated(pid, properties)
                except Exception:
                    log.exception("Factory update failed: %s → %s", factory_pid, pid)

    def delete(self, pid: str) -> None:
        """Delete a configuration and notify managed services."""
        self._configs.pop(pid, None)
        self._persist()

        if pid in self._managed:
            try:
                self._managed[pid].updated(None)
            except Exception:
                log.exception("Managed service delete-notify failed: %s", pid)

        for factory_pid, factory in self._factories.items():
            if pid.startswith(factory_pid):
                try:
                    factory.deleted(pid)
                except Exception:
                    log.exception("Factory delete failed: %s → %s", factory_pid, pid)

    # ── Persistence ────────────────────────────────────────────────

    def _load_persisted(self) -> dict[str, dict[str, Any]]:
        """Load configs from JSON file if persist_path is configured."""
        if not self._persist_path:
            return {}
        try:
            import json
            from pathlib import Path
            p = Path(self._persist_path)
            if p.exists():
                data = json.loads(p.read_text())
                log.info("ConfigAdmin loaded %d configs from %s", len(data), p)
                return data
        except Exception:
            log.exception("ConfigAdmin failed to load from %s", self._persist_path)
        return {}

    def _persist(self) -> None:
        """Save configs to JSON file if persist_path is configured."""
        if not self._persist_path:
            return
        try:
            import json
            from pathlib import Path
            p = Path(self._persist_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self._configs, indent=2, default=str))
        except Exception:
            log.exception("ConfigAdmin failed to persist to %s", self._persist_path)

    # ── Bus-exposed runnables ──────────────────────────────────────

    @runnable("update", params=UpdateParams, description="Update a managed config")
    async def update_config(self, params):
        self.update(params.pid, params.properties)
        return {"pid": params.pid, "updated": True}

    @runnable("delete", params=DeleteParams, description="Delete a managed config")
    async def delete_config(self, params):
        self.delete(params.pid)
        return {"pid": params.pid, "deleted": True}

    @runnable("get", params=GetParams, description="Get a managed config")
    async def get_config(self, params):
        return self.get_configuration(params.pid)

    @lifecycle.deactivate
    def deactivate(self):
        self._configs = {}
        self._managed = {}
        self._factories = {}
