"""ConfigProvider — general N-layer config with deferred resolution and interpolation.

Provides: IConfig, IConfigAdmin
Requires: nothing (leaf provider, activates first)

## Design

The ConfigProvider is a general mechanism. It knows nothing about "workspaces",
"apps", or deployment models. It provides:

1. **N ordered layers** — each has a name, a source, and optionally a validator.
   Later layers override earlier layers.
2. **Deferred layer sources** — a layer's source can be a callable that receives
   the merged state of all prior layers. This solves the bootstrap problem
   (e.g., layer 2's file path comes from layer 1's `current_workspace` value).
3. **Interpolation** — string values can contain `${dotted.path}` (resolved
   against merged config state) or `${env:VAR}` (resolved against environment).
4. **Per-key reactivity** — each dotted key gets a lazy Signal. Only consumers
   of that specific key re-run when it changes. Consumers of `.all()` subscribe
   to a version counter that bumps on any mutation.
5. **Writes target a specific layer** — the caller specifies which writable
   layer to write to (defaults to the highest-priority writable layer).
6. **Provenance** — every value can be traced to the layer that provided it.
7. **Branch updates** — subtree replacement with configurable merge strategies.

## Usage

The application configures layers at instantiation time via properties:

    kernel.instantiate("config", properties={
        "layers": [
            ConfigLayer("defaults", DictSource({"model": "claude-sonnet"})),
            ConfigLayer("workspace", lambda ctx: YAMLSource(Path(ctx["workspace_root"]) / "local.yaml")),
            ConfigLayer("runtime", lambda ctx: YAMLSource(Path(ctx["workspace_root"]) / "settings.yaml"), writable=True),
        ],
    })

Components read config reactively via self.rt.config.get(key).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from signalpy.kernel import component, provides, lifecycle, runnable, Signal

log = logging.getLogger(__name__)


# ── Source protocol ─────────────────────────────────────────────────

@runtime_checkable
class ConfigSource(Protocol):
    """A source that can load and optionally persist config data.

    The `context` parameter receives the merged state of all prior layers,
    enabling interpolation of paths and values during load.
    """
    def load(self, context: dict | None = None) -> dict: ...
    def persist(self, data: dict) -> None: ...


# ── Built-in sources ────────────────────────────────────────────────

class DictSource:
    """In-memory dict source. Useful for defaults and tests."""

    def __init__(self, data: dict | None = None):
        self._data = data or {}

    def load(self, context: dict | None = None) -> dict:
        return dict(self._data)

    def persist(self, data: dict) -> None:
        self._data = dict(data)


class YAMLSource:
    """Load/persist config from a YAML file."""

    def __init__(self, path: Path | str):
        self._path = Path(path)

    def load(self, context: dict | None = None) -> dict:
        if not self._path.is_file():
            return {}
        try:
            import yaml
            with open(self._path) as f:
                return yaml.safe_load(f) or {}
        except ImportError:
            log.warning("pyyaml not installed — cannot load %s", self._path)
            return {}
        except Exception:
            log.exception("Failed to load YAML config from %s", self._path)
            return {}

    def persist(self, data: dict) -> None:
        try:
            import yaml
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
            tmp.replace(self._path)
        except ImportError:
            log.warning("pyyaml not installed — cannot persist to %s", self._path)
        except Exception:
            log.exception("Failed to persist config to %s", self._path)


class JSONSource:
    """Load/persist config from a JSON file."""

    def __init__(self, path: Path | str):
        self._path = Path(path)

    def load(self, context: dict | None = None) -> dict:
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text())
        except Exception:
            log.exception("Failed to load JSON config from %s", self._path)
            return {}

    def persist(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, default=str))
            tmp.replace(self._path)
        except Exception:
            log.exception("Failed to persist config to %s", self._path)


class EnvSource:
    """Load config from environment variables with a prefix.

    MYAPP_FOO__BAR=value → {"foo": {"bar": "value"}}
    """

    def __init__(self, prefix: str = "KERNEL_CFG_"):
        self._prefix = prefix

    def load(self, context: dict | None = None) -> dict:
        data: dict = {}
        for key, value in os.environ.items():
            if key.startswith(self._prefix):
                dotted = key[len(self._prefix):].lower().replace("__", ".")
                _set_dotted(data, dotted, value)
        return data

    def persist(self, data: dict) -> None:
        pass  # env vars are read-only


# ── Layer definition ────────────────────────────────────────────────

class MergeStrategy(Enum):
    REPLACE = auto()   # new dict replaces old entirely (absent keys = deleted)
    PATCH = auto()     # only keys present in new dict are updated
    DEEP = auto()      # recursive deep merge (default for layer loading)


@dataclass
class ConfigLayer:
    """One layer in the config stack.

    Args:
        name: Human-readable layer name (e.g., "defaults", "workspace", "runtime")
        source: A ConfigSource instance, OR a callable(context) → ConfigSource
                for deferred resolution (source path depends on prior layers).
        writable: Whether this layer accepts runtime writes via set()/set_branch().
        validator: Optional Pydantic BaseModel class for validation.
    """
    name: str
    source: Any  # ConfigSource | Callable[[dict], ConfigSource]
    writable: bool = False
    validator: type | None = None  # Pydantic BaseModel subclass


# ── Interpolation engine ────────────────────────────────────────────

_INTERP_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate_value(value: str, context: dict, _depth: int = 0) -> str:
    """Resolve ${dotted.path} and ${env:VAR} in a string value.

    Supports:
      ${some.key}       — resolved against config context
      ${env:VAR}        — resolved against os.environ
      ${env:VAR:-default} — with default if VAR not set
    """
    if _depth > 10:
        raise ValueError(f"Circular interpolation detected in: {value}")

    def _replace(m: re.Match) -> str:
        expr = m.group(1)
        if expr.startswith("env:"):
            # ${env:VAR} or ${env:VAR:-default}
            env_part = expr[4:]
            if ":-" in env_part:
                var, default = env_part.split(":-", 1)
                return os.environ.get(var, default)
            return os.environ.get(env_part, "")
        # ${dotted.path} — resolve against context
        val = _read_dotted(context, expr)
        if val is None:
            return m.group(0)  # leave unresolved
        return str(val)

    result = _INTERP_RE.sub(_replace, value)
    # Recurse if the result still has interpolations (transitive references)
    if _INTERP_RE.search(result) and result != value:
        return _interpolate_value(result, context, _depth + 1)
    return result


def _interpolate_tree(data: Any, context: dict) -> Any:
    """Walk a data tree and interpolate all string values."""
    if isinstance(data, str):
        if "${" in data:
            return _interpolate_value(data, context)
        return data
    if isinstance(data, dict):
        return {k: _interpolate_tree(v, context) for k, v in data.items()}
    if isinstance(data, list):
        return [_interpolate_tree(item, context) for item in data]
    return data


# ── Helpers ─────────────────────────────────────────────────────────

def _read_dotted(data: dict, key: str) -> Any:
    """Read a dotted path from a dict. Non-reactive."""
    cur: Any = data
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _set_dotted(data: dict, key: str, value: Any) -> None:
    """Set a value at a dotted path."""
    parts = key.split(".")
    cur = data
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep merge overlay into base (mutates base). Returns base."""
    for k, v in overlay.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


# ── Pydantic models for bus runnables ───────────────────────────────

try:
    from pydantic import BaseModel
except ImportError:
    BaseModel = None  # type: ignore[assignment,misc]

@runtime_checkable
class IManagedService(Protocol):
    """A service that accepts configuration updates."""
    def updated(self, properties: dict[str, Any] | None) -> None: ...


@runtime_checkable
class IManagedServiceFactory(Protocol):
    """A factory that manages multiple configured instances."""
    def updated(self, pid: str, properties: dict[str, Any]) -> None: ...
    def deleted(self, pid: str) -> None: ...


if BaseModel is not None:
    class SetParams(BaseModel):
        key: str
        value: Any
        layer: str | None = None

    class SetBranchParams(BaseModel):
        key: str
        value: dict
        layer: str | None = None
        merge: str = "REPLACE"  # REPLACE, PATCH, DEEP

    class ResetParams(BaseModel):
        key: str
        layer: str | None = None

    class GetSourceParams(BaseModel):
        key: str

    class UpdateParams(BaseModel):
        pid: str
        properties: dict[str, Any] = {}

    class DeleteParams(BaseModel):
        pid: str

    class GetParams(BaseModel):
        pid: str
else:
    SetParams = SetBranchParams = ResetParams = GetSourceParams = None  # type: ignore[assignment,misc]
    UpdateParams = DeleteParams = GetParams = None  # type: ignore[assignment,misc]


# ── Provider ────────────────────────────────────────────────────────

@component("config", version="2.0")
@provides("IConfig", "IConfigAdmin")
class ConfigProvider:
    """General N-layer config provider with deferred resolution and interpolation.

    The kernel provides the mechanism. The application provides the policy
    (layer names, sources, hierarchy) via properties at instantiation time.
    """

    @lifecycle.activate
    def activate(self):
        layers_input = self.rt.properties.get("layers", [])

        # Accept either ConfigLayer objects or legacy dict/path properties
        if layers_input and isinstance(layers_input[0], ConfigLayer):
            self._layers = list(layers_input)
        else:
            # Legacy fallback: build layers from old-style properties
            self._layers = self._build_legacy_layers()

        # Load all layers in order, with deferred resolution
        self._layer_data: dict[str, dict] = {}  # layer_name → raw data from that layer
        self._resolved_sources: dict[str, ConfigSource] = {}  # layer_name → resolved source
        self._state: dict[str, Any] = self._load_all_layers()

        # Per-key reactive state
        self._key_signals: dict[str, Signal] = {}
        self._all_version: Signal[int] = Signal(0)
        self._key_lock = threading.RLock()

        # ConfigAdmin: managed service registrations + per-PID configs
        self._managed: dict[str, IManagedService] = {}
        self._factories: dict[str, IManagedServiceFactory] = {}
        self._pid_configs: dict[str, dict[str, Any]] = {}

    def _build_legacy_layers(self) -> list[ConfigLayer]:
        """Backwards-compatible: old-style properties → layer list."""
        layers = []
        defaults = self.rt.properties.get("defaults", {})
        if defaults:
            layers.append(ConfigLayer("defaults", DictSource(defaults)))
        config_path = self.rt.properties.get("config_path")
        if config_path:
            layers.append(ConfigLayer("file", YAMLSource(config_path)))
        # Env overrides
        layers.append(ConfigLayer("env", EnvSource()))
        # Persisted overrides or in-memory writable fallback
        persist_path = self.rt.properties.get("persist_path", "")
        if persist_path:
            layers.append(ConfigLayer("persisted", JSONSource(persist_path), writable=True))
        else:
            layers.append(ConfigLayer("runtime", DictSource(), writable=True))
        return layers

    def _load_all_layers(self) -> dict:
        """Load layers in order. Each layer can see prior layers' merged state."""
        merged: dict = {}
        for layer in self._layers:
            # Resolve source (may be deferred — callable receiving prior state)
            source = layer.source
            if callable(source) and not isinstance(source, ConfigSource):
                try:
                    source = source(merged)
                except Exception:
                    log.exception("Failed to resolve deferred source for layer %r", layer.name)
                    source = DictSource()
            self._resolved_sources[layer.name] = source

            # Load data from source
            try:
                raw = source.load(merged)
            except Exception:
                log.exception("Failed to load layer %r", layer.name)
                raw = {}

            # Interpolate values against current merged state
            raw = _interpolate_tree(raw, merged)

            # Validate if validator specified
            if layer.validator and raw:
                try:
                    model_cls = layer.validator
                    if hasattr(model_cls, "model_validate"):
                        model_cls.model_validate(raw)
                except Exception as exc:
                    log.warning("Validation failed for layer %r: %s", layer.name, exc)

            # Store per-layer data (for provenance and write-back)
            self._layer_data[layer.name] = dict(raw)

            # Merge into running state
            _deep_merge(merged, raw)

        return merged

    # ── IConfig (reactive reads) ─────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dotted path. REACTIVE — tracks the caller."""
        value = _read_dotted(self._state, key)
        sig = self._key_signal(key, value)
        sig.get()  # reactive read — registers dependency
        return value if value is not None else default

    def get_typed(self, section: str, model: type) -> Any:
        """Validate a config section against a Pydantic model. REACTIVE."""
        if section:
            raw = _read_dotted(self._state, section)
            # Track the section key reactively
            sig = self._key_signal(section, raw)
            sig.get()
        else:
            raw = self._state
            self._all_version.get()  # track full config
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if hasattr(model, "model_validate"):
            return model.model_validate(raw)
        return raw

    def all(self) -> dict[str, Any]:
        """Return the full config dict. REACTIVE — subscribes to ANY change."""
        self._all_version.get()
        return dict(self._state)

    # ── IConfigAdmin (writes) ────────────────────────────────────

    def set(self, key: str, value: Any, layer: str | None = None) -> None:
        """Set a single key. Writes to the specified writable layer
        (defaults to highest-priority writable layer)."""
        target_layer = self._resolve_write_layer(layer)
        _set_dotted(self._layer_data[target_layer.name], key, value)
        _set_dotted(self._state, key, value)
        self._persist_layer(target_layer)
        self._notify_key(key)

    def set_branch(
        self,
        key: str,
        value: dict,
        layer: str | None = None,
        merge: MergeStrategy = MergeStrategy.REPLACE,
    ) -> None:
        """Set an entire subtree. Merge strategy controls how the new
        value combines with existing data at that path."""
        target_layer = self._resolve_write_layer(layer)
        layer_data = self._layer_data[target_layer.name]

        if merge == MergeStrategy.REPLACE:
            _set_dotted(layer_data, key, value)
            _set_dotted(self._state, key, value)
        elif merge == MergeStrategy.PATCH:
            existing = _read_dotted(self._state, key)
            if isinstance(existing, dict):
                patched = dict(existing)
                patched.update(value)
                _set_dotted(layer_data, key, value)
                _set_dotted(self._state, key, patched)
            else:
                _set_dotted(layer_data, key, value)
                _set_dotted(self._state, key, value)
        elif merge == MergeStrategy.DEEP:
            existing = _read_dotted(self._state, key)
            if isinstance(existing, dict):
                merged = dict(existing)
                _deep_merge(merged, value)
                _set_dotted(layer_data, key, value)
                _set_dotted(self._state, key, merged)
            else:
                _set_dotted(layer_data, key, value)
                _set_dotted(self._state, key, value)

        self._persist_layer(target_layer)
        self._notify_key(key)

    def reset(self, key: str, layer: str | None = None) -> None:
        """Remove an override from a writable layer. The value falls back
        to whatever lower layers provide."""
        target_layer = self._resolve_write_layer(layer)
        layer_data = self._layer_data[target_layer.name]

        # Remove from the target layer
        parts = key.split(".")
        cur = layer_data
        for part in parts[:-1]:
            if not isinstance(cur, dict) or part not in cur:
                return  # nothing to remove
            cur = cur[part]
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)

        # Recompute merged state for this key from all layers
        self._state = self._recompute_merged()
        self._persist_layer(target_layer)
        self._notify_key(key)

    def get_with_source(self, key: str) -> tuple[Any, str]:
        """Get value + which layer provided it (provenance)."""
        # Walk layers in reverse (highest priority first)
        for layer in reversed(self._layers):
            val = _read_dotted(self._layer_data.get(layer.name, {}), key)
            if val is not None:
                return (val, layer.name)
        return (None, "")

    def layers(self) -> list[str]:
        """Return ordered layer names (lowest → highest priority)."""
        return [l.name for l in self._layers]

    def get_layer_data(self, layer_name: str) -> dict:
        """Return raw data for a specific layer (for inspection/debugging)."""
        return dict(self._layer_data.get(layer_name, {}))

    # ── Bus-exposed runnables ────────────────────────────────────

    # ── ConfigAdmin: Managed services (backwards compat) ────────

    def get_configuration(self, pid: str) -> dict[str, Any]:
        """Get the current configuration for a managed PID."""
        return dict(self._pid_configs.get(pid, {}))

    def update(self, pid: str, properties: dict[str, Any]) -> None:
        """Update configuration for a managed PID and notify services."""
        self._pid_configs[pid] = dict(properties)
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
        """Delete a managed configuration and notify services."""
        self._pid_configs.pop(pid, None)
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

    # Config operations are accessed via @requires(config=IConfig) or
    # @requires(config_admin=IConfigAdmin) + direct method calls.
    # No @runnable needed — these are not routable operations.

    @lifecycle.deactivate
    def deactivate(self):
        self._layer_data = {}
        self._resolved_sources = {}
        self._pid_configs = {}
        self._managed = {}
        self._factories = {}

    # ── Internal ─────────────────────────────────────────────────

    def _resolve_write_layer(self, layer_name: str | None) -> ConfigLayer:
        """Find the target writable layer."""
        if layer_name:
            for layer in self._layers:
                if layer.name == layer_name:
                    if not layer.writable:
                        raise ValueError(f"Layer {layer_name!r} is not writable")
                    return layer
            raise KeyError(f"No layer named {layer_name!r}")
        # Default: highest-priority writable layer
        for layer in reversed(self._layers):
            if layer.writable:
                return layer
        raise ValueError("No writable layer configured")

    def _persist_layer(self, layer: ConfigLayer) -> None:
        """Persist a layer's data to its source."""
        source = self._resolved_sources.get(layer.name)
        if source and layer.writable:
            try:
                source.persist(self._layer_data[layer.name])
            except Exception:
                log.exception("Failed to persist layer %r", layer.name)

    def _recompute_merged(self) -> dict:
        """Recompute full merged state from all layer data."""
        merged: dict = {}
        for layer in self._layers:
            data = self._layer_data.get(layer.name, {})
            _deep_merge(merged, data)
        return merged

    def _notify_key(self, key: str) -> None:
        """Notify reactive consumers of a key change."""
        with self._key_lock:
            if key in self._key_signals:
                self._key_signals[key].set(_read_dotted(self._state, key))
            # Notify descendants
            prefix = key + "."
            descendants = [k for k in self._key_signals if k.startswith(prefix)]
        for k in descendants:
            self._key_signals[k].set(_read_dotted(self._state, k))
        # Bump all-version for .all() consumers
        self._all_version.set(self._all_version.peek() + 1)

    def _key_signal(self, key: str, current_value: Any) -> Signal:
        """Lazily create the per-key Signal."""
        sig = self._key_signals.get(key)
        if sig is not None:
            return sig
        with self._key_lock:
            sig = self._key_signals.get(key)
            if sig is None:
                sig = Signal(current_value)
                self._key_signals[key] = sig
            return sig
