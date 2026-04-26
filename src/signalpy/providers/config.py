"""ConfigProvider — layered config with per-key reactive state and runtime admin.

Provides: IConfig, IConfigAdmin
Requires: nothing (leaf provider, activates first)

Config is layered:
  defaults (manifest) → app config.yaml → workspace overlay → env overrides

**Per-key reactivity.** State is a plain dict; reactivity lives on a lazily
populated map `dotted-key → Signal`. `config.get("a.b")` registers the active
@effect/@computed against that specific dotted key. `config.set("a.b", v)`
only notifies consumers of that key (and its descendants, so a parent-replace
correctly invalidates child reads). Consumers of `config.all()` subscribe to
a single `_all_version` Signal that bumps on every mutation.

Versus the old "one master `Signal[dict]`" design: a write to one key no longer
re-runs every effect that ever read any key. Effects only re-run if their own
read keys actually changed.

Also provides ConfigAdmin capabilities: push config updates by PID to
managed services, optional JSON persistence.
"""
from __future__ import annotations

import json
import logging
import os
import threading
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

        # Per-key reactive state.
        #   _state         — plain dict (the source of truth)
        #   _key_signals   — lazy: dotted-key → Signal[value]; populated on first
        #                    reactive read of that key. Drives per-key fan-out.
        #   _all_version   — version counter; .all() consumers subscribe here so
        #                    they re-run on any mutation, not just one key's.
        #   _key_lock      — guards _key_signals dict mutation (lazy create).
        #                    The kernel's RLock guards each Signal's value/notify;
        #                    this one only guards our own dict layer.
        self._state: dict[str, Any] = data
        self._key_signals: dict[str, Signal] = {}
        self._all_version: Signal[int] = Signal(0)
        self._key_lock = threading.RLock()

        # ConfigAdmin: managed service registrations + per-PID configs
        self._managed: dict[str, IManagedService] = {}
        self._factories: dict[str, IManagedServiceFactory] = {}
        self._pid_configs: dict[str, dict[str, Any]] = {}

    # ── IConfig ──────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dotted path. REACTIVE — tracks the caller
        on this specific key only (not on the whole config dict)."""
        value = self._read_path(key)
        # Lazy-create the per-key Signal even if value is None, so a later
        # set(key, real_value) correctly notifies this consumer.
        sig = self._key_signal(key, value)
        # Reactive read: registers active @effect/@computed against this key.
        # We discard the returned value because peek would skip dotted-path
        # traversal — value above already walked the path.
        sig.get()
        return value if value is not None else default

    def set(self, key: str, value: Any) -> None:
        """Set a config value at runtime. Triggers per-key reactive propagation:
        only consumers of this exact key (and its dotted descendants — so
        replacing a parent correctly invalidates child reads) are notified.
        Consumers of `.all()` are also notified via _all_version."""
        self._set_in(self._state, key, value)

        with self._key_lock:
            # Notify the exact key, if anyone has read it
            if key in self._key_signals:
                self._key_signals[key].set(self._read_path(key))
            # Notify dotted descendants — replacing a parent invalidates them
            prefix = key + "."
            descendants = [k for k in self._key_signals if k.startswith(prefix)]
        for k in descendants:
            self._key_signals[k].set(self._read_path(k))

        # Bump the all() version so .all() consumers re-run
        self._all_version.set(self._all_version.peek() + 1)

    def all(self) -> dict[str, Any]:
        """Return the full config dict. REACTIVE — subscribes to ANY change."""
        self._all_version.get()  # reactive: track on the version counter
        return dict(self._state)

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
                "config": dict(self._state),
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

    def _read_path(self, key: str) -> Any:
        """Walk the dotted path through self._state. Returns None if missing.
        Non-reactive — does not register dependencies."""
        cur: Any = self._state
        for part in key.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
            if cur is None:
                return None
        return cur

    def _key_signal(self, key: str, current_value: Any) -> Signal:
        """Lazily create-and-return the per-key Signal, seeded with the
        current value. Thread-safe via _key_lock (the kernel's own RLock
        protects each Signal's internals separately)."""
        sig = self._key_signals.get(key)
        if sig is not None:
            return sig
        with self._key_lock:
            sig = self._key_signals.get(key)
            if sig is None:
                sig = Signal(current_value)
                self._key_signals[key] = sig
            return sig

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
