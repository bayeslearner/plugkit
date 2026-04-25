"""ConfigProvider — layered config with Signal-backed state and runtime admin.

Provides: IConfig, IConfigAdmin
Requires: nothing (leaf provider, activates first)

Config is layered:
  defaults (manifest) → app config.yaml → workspace overlay → env overrides

State is stored in a Signal. Reading config inside @effect or @computed
tracks the dependency automatically. When config changes (set, update),
the Signal notifies the reactive graph — no manual callbacks needed.

Also provides ConfigAdmin capabilities: push config updates by PID to
managed services, optional JSON persistence.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from signalpy.kernel import component, provides, lifecycle, runnable, Signal

log = logging.getLogger(__name__)


# ── Managed service protocols ────────────────────────────────────

@runtime_checkable
class IManagedService(Protocol):
    """A service that accepts configuration updates."""
    def updated(self, properties: dict[str, Any] | None) -> None: ...


@runtime_checkable
class IManagedServiceFactory(Protocol):
    """A factory that manages multiple configured instances."""
    def updated(self, pid: str, properties: dict[str, Any]) -> None: ...
    def deleted(self, pid: str) -> None: ...


# ── Pydantic models for bus runnables ────────────────────────────

try:
    from pydantic import BaseModel
except ImportError:
    BaseModel = None  # type: ignore[assignment,misc]

if BaseModel is not None:
    class UpdateParams(BaseModel):
        pid: str
        properties: dict[str, Any] = {}

    class DeleteParams(BaseModel):
        pid: str

    class GetParams(BaseModel):
        pid: str

    class SetParams(BaseModel):
        key: str
        value: Any
else:
    UpdateParams = DeleteParams = GetParams = SetParams = None  # type: ignore[assignment,misc]


# ── Provider ─────────────────────────────────────────────────────

@component("config", version="0.2")
@provides("IConfig", "IConfigAdmin")
class ConfigProvider:
    """Unified config: layered loading + Signal-backed reactivity + admin.

    On activation, loads config from:
      1. defaults dict (passed as property)
      2. config_path YAML file (if exists)
      3. Environment variable overrides (KERNEL_CFG_<KEY>=value)
      4. Persisted JSON overrides (if persist_path configured)

    State is a Signal. Every `get()` inside @effect or @computed tracks
    the dependency. `set()` or `update()` creates a new dict → Signal
    notifies → all consumers re-evaluate automatically.

    ConfigAdmin usage (push config to managed services):
        await rt.invoke("config.update", {
            "pid": "pretty.printer",
            "properties": {"line_length": 80}
        })
    """

    @lifecycle.activate
    def activate(self):
        defaults = self.rt.properties.get("defaults", {})
        config_path = self.rt.properties.get("config_path")
        self._persist_path = self.rt.properties.get("persist_path", "")

        data: dict[str, Any] = dict(defaults)

        # Layer 2: YAML file
        if config_path:
            p = Path(config_path)
            if p.is_file():
                try:
                    import yaml
                    with open(p) as f:
                        file_cfg = yaml.safe_load(f) or {}
                    self._merge(data, file_cfg)
                except ImportError:
                    log.warning("pyyaml not installed — skipping YAML config")

        # Layer 3: Env overrides (KERNEL_CFG_FOO__BAR → foo.bar)
        prefix = "KERNEL_CFG_"
        for key, value in os.environ.items():
            if key.startswith(prefix):
                dotted = key[len(prefix):].lower().replace("__", ".")
                self._set_in(data, dotted, value)

        # Layer 4: Persisted overrides
        persisted = self._load_persisted()
        self._merge(data, persisted)

        # Signal-backed state — this IS the reactivity
        self._state: Signal[dict[str, Any]] = Signal(data)

        # ConfigAdmin: managed service registrations + per-PID configs
        self._managed: dict[str, IManagedService] = {}
        self._factories: dict[str, IManagedServiceFactory] = {}
        self._pid_configs: dict[str, dict[str, Any]] = {}

    # ── IConfig ──────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dotted path. REACTIVE — tracks the caller."""
        data = self._state.get()  # reactive read: tracks consumer
        parts = key.split(".")
        cur: Any = data
        for part in parts:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(part)
            if cur is None:
                return default
        return cur

    def set(self, key: str, value: Any) -> None:
        """Set a config value at runtime. Triggers reactive propagation."""
        data = dict(self._state.peek())  # copy — new identity
        self._set_in(data, key, value)
        self._state.set(data)  # Signal.set → notifies all consumers

    def all(self) -> dict[str, Any]:
        """Return the full config dict. REACTIVE."""
        return dict(self._state.get())

    # ── IConfigAdmin ─────────────────────────────────────────────

    def get_configuration(self, pid: str) -> dict[str, Any]:
        """Get the current configuration for a PID."""
        return dict(self._pid_configs.get(pid, {}))

    def update(self, pid: str, properties: dict[str, Any]) -> None:
        """Update configuration for a PID and notify managed services."""
        self._pid_configs[pid] = dict(properties)
        self._persist()

        if pid in self._managed:
            try:
                self._managed[pid].updated(properties)
            except Exception:
                log.exception("Managed service update failed: %s", pid)

        for factory_pid, factory in self._factories.items():
            if pid.startswith(factory_pid):
                try:
                    factory.updated(pid, properties)
                except Exception:
                    log.exception("Factory update failed: %s → %s", factory_pid, pid)

    def delete(self, pid: str) -> None:
        """Delete a configuration and notify managed services."""
        self._pid_configs.pop(pid, None)
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

    def register_managed(self, pid: str, service: IManagedService) -> None:
        """Register a managed service to receive config for a PID."""
        self._managed[pid] = service
        if pid in self._pid_configs:
            service.updated(self._pid_configs[pid])

    def register_factory(self, pid: str, factory: IManagedServiceFactory) -> None:
        """Register a managed service factory for a PID."""
        self._factories[pid] = factory

    # ── Persistence ──────────────────────────────────────────────

    def _load_persisted(self) -> dict[str, Any]:
        if not self._persist_path:
            return {}
        try:
            p = Path(self._persist_path)
            if p.exists():
                data = json.loads(p.read_text())
                log.info("Config loaded %d persisted entries from %s", len(data), p)
                return data
        except Exception:
            log.exception("Config failed to load from %s", self._persist_path)
        return {}

    def _persist(self) -> None:
        if not self._persist_path:
            return
        try:
            p = Path(self._persist_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # Persist both the main config and PID configs
            payload = {
                "config": self._state.peek(),
                "pid_configs": self._pid_configs,
            }
            p.write_text(json.dumps(payload, indent=2, default=str))
        except Exception:
            log.exception("Config failed to persist to %s", self._persist_path)

    # ── Bus-exposed runnables ────────────────────────────────────

    @runnable("set", params=SetParams, description="Set a config key at runtime")
    async def set_config(self, params):
        self.set(params.key, params.value)
        return {"key": params.key, "updated": True}

    @runnable("update", params=UpdateParams, description="Update a managed config by PID")
    async def update_config(self, params):
        self.update(params.pid, params.properties)
        return {"pid": params.pid, "updated": True}

    @runnable("delete", params=DeleteParams, description="Delete a managed config by PID")
    async def delete_config(self, params):
        self.delete(params.pid)
        return {"pid": params.pid, "deleted": True}

    @runnable("get", params=GetParams, description="Get a managed config by PID")
    async def get_config(self, params):
        return self.get_configuration(params.pid)

    @lifecycle.deactivate
    def deactivate(self):
        self._pid_configs = {}
        self._managed = {}
        self._factories = {}

    # ── Internal helpers ─────────────────────────────────────────

    @staticmethod
    def _set_in(data: dict, key: str, value: Any) -> None:
        """Set a value at a dotted path in a dict."""
        parts = key.split(".")
        cur = data
        for part in parts[:-1]:
            if part not in cur or not isinstance(cur[part], dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value

    @staticmethod
    def _merge(base: dict, overlay: dict) -> None:
        """Deep merge overlay into base."""
        for k, v in overlay.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                ConfigProvider._merge(base[k], v)
            else:
                base[k] = v
