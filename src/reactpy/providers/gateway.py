"""API Gateway — the single external face of the system.

Components contribute runnables + @api declarations.
The gateway collects them into one organic whole per transport.
Transport adapters consume from the gateway, not from raw bus handlers.

Provides: IGateway
Requires: IConfig, ILogger

This is the composition layer between "apps provide capabilities"
and "transports expose them."
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from reactpy.kernel import component, provides, requires, lifecycle
from reactpy.kernel.component import APIDef, ComponentMeta, RunnableDef

log = logging.getLogger(__name__)


@dataclass
class APIEntry:
    """A single entry in a composed API surface."""
    runnable_name: str          # qualified: acme.splunk.query
    short_name: str             # as exposed: query (within group)
    group: str                  # logical group: splunk
    description: str
    params_model: type
    return_type: type | None
    source_component: str       # who contributed this
    api_def: APIDef             # the @api declaration it came from


@dataclass
class APISurface:
    """A composed API surface for one transport — the organic whole."""
    transport: str              # "rest", "mcp", "cli"
    entries: list[APIEntry] = field(default_factory=list)
    auth_required: bool = False
    version: str = ""
    rate_limit: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    def groups(self) -> dict[str, list[APIEntry]]:
        """Group entries by their group name."""
        result: dict[str, list[APIEntry]] = {}
        for entry in self.entries:
            result.setdefault(entry.group, []).append(entry)
        return result


@component("gateway", version="0.1", depends=["config", "logging"])
@provides("IGateway")
@requires(config="IConfig", logger="ILogger")
class APIGateway:
    """Collects @api declarations from all components, composes unified surfaces.

    On activation:
      1. Scans all active components for @api declarations
      2. For each transport type found, builds an APISurface
      3. Applies cross-cutting policy (auth, rate limits)
      4. Transport adapters call get_surface("rest") to get the composed API

    Components WITHOUT @api declarations get a fallback:
      all non-internal runnables exposed on all transports.
    """

    @lifecycle.activate
    def activate(self, rt):
        self._bus = rt.bus
        self._surfaces: dict[str, APISurface] = {}
        self._rt = rt

        # Scan all active components via lifecycle
        # We access the bus handlers + component metadata
        self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild all API surfaces from current component state."""
        self._surfaces.clear()

        # Get all component instances from the lifecycle manager
        # We need to access this through the kernel — but the gateway
        # is activated by the kernel, so we inspect bus handlers + registry
        from reactpy.kernel.component import get_meta, has_meta
        from reactpy.kernel.lifecycle_manager import LifecycleManager

        # Collect all registered runnables and their component metadata
        for handler_name in self._bus.handlers:
            parts = handler_name.rsplit(".", 1)
            if len(parts) != 2:
                continue
            component_qn, runnable_name = parts[0], parts[1]

            # Find the component meta from factories
            # We extract it by matching qualified_name
            meta = self._find_meta(component_qn)
            if meta is None:
                # Fallback: expose on all transports with no grouping
                self._add_fallback_entry(handler_name, component_qn, runnable_name)
                continue

            if not meta.apis:
                # No @api declarations — fallback: expose everywhere
                self._add_fallback_entry(handler_name, component_qn, runnable_name, meta)
                continue

            # Component has explicit @api declarations
            for api_def in meta.apis:
                # Check if this runnable should be included
                rd = next((r for r in meta.runnables if r.name == runnable_name), None)
                if rd and rd.internal:
                    continue
                if api_def.include and runnable_name not in api_def.include:
                    continue
                if api_def.exclude and runnable_name in api_def.exclude:
                    continue

                surface = self._get_surface(api_def.transport)
                group = api_def.prefix.strip("/") or meta.factory_name

                surface.entries.append(APIEntry(
                    runnable_name=handler_name,
                    short_name=runnable_name,
                    group=group,
                    description=rd.description if rd else "",
                    params_model=rd.params_model if rd else object,
                    return_type=rd.return_type if rd else None,
                    source_component=component_qn,
                    api_def=api_def,
                ))

                # Apply API-level policy to surface
                if api_def.auth:
                    surface.auth_required = True
                if api_def.version and not surface.version:
                    surface.version = api_def.version
                if api_def.rate_limit and not surface.rate_limit:
                    surface.rate_limit = api_def.rate_limit

    def _find_meta(self, qualified_name: str) -> ComponentMeta | None:
        """Find ComponentMeta by qualified name (namespace.factory or factory)."""
        # Walk registered factories in the lifecycle manager
        # The gateway has access to the bus, which has the kernel reference
        # For now, we scan all known factories
        try:
            from reactpy.kernel.lifecycle_manager import LifecycleManager
            # We store a ref to lifecycle during activation
            if hasattr(self, "_lifecycle"):
                for ci in self._lifecycle.all_instances():
                    if ci.meta.qualified_name == qualified_name:
                        return ci.meta
        except Exception:
            pass
        return None

    def _get_surface(self, transport: str) -> APISurface:
        if transport not in self._surfaces:
            self._surfaces[transport] = APISurface(transport=transport)
        return self._surfaces[transport]

    def _add_fallback_entry(
        self, handler_name: str, component_qn: str,
        runnable_name: str, meta: ComponentMeta | None = None,
    ) -> None:
        """Add a runnable to ALL surfaces (fallback when no @api declared)."""
        rd = None
        if meta:
            rd = next((r for r in meta.runnables if r.name == runnable_name), None)
            if rd and rd.internal:
                return

        for transport in ["rest", "mcp", "cli"]:
            surface = self._get_surface(transport)
            surface.entries.append(APIEntry(
                runnable_name=handler_name,
                short_name=runnable_name,
                group=component_qn,
                description=rd.description if rd else "",
                params_model=rd.params_model if rd else object,
                return_type=rd.return_type if rd else None,
                source_component=component_qn,
                api_def=APIDef(transport=transport),
            ))

    # ── Public API ──────────────────────────────────────────────

    def get_surface(self, transport: str) -> APISurface | None:
        """Get the composed API surface for a transport."""
        return self._surfaces.get(transport)

    def surfaces(self) -> dict[str, APISurface]:
        """All composed surfaces."""
        return dict(self._surfaces)

    def set_lifecycle(self, lifecycle_mgr) -> None:
        """Called by kernel after boot to give gateway component introspection."""
        self._lifecycle = lifecycle_mgr
        self._rebuild()

    @lifecycle.deactivate
    def deactivate(self, rt):
        self._surfaces.clear()
