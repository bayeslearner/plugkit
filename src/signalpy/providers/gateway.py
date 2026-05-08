"""API Gateway — the single external face of the system.

Components declare transport config on @component and per-operation
visibility on @runnable(transports=[...]). The gateway collects them
into unified surfaces per transport using kernel.runnables_by_component().

Provides: IGateway
Requires: IConfig, ILogger

Spec 011: No @api, no APIDef. Uses kernel.runnables_by_component().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)


@dataclass
class APIEntry:
    """A single entry in a composed API surface."""
    runnable_name: str          # qualified: splunk.query
    short_name: str             # as exposed: query (within group)
    group: str                  # logical group: splunk
    description: str
    params_model: type
    return_type: type | None
    source_component: str       # who contributed this
    handler: Any = None         # direct callable reference


@dataclass
class APISurface:
    """A composed API surface for one transport — the organic whole."""
    transport: str              # "rest", "mcp", "cli"
    entries: list[APIEntry] = field(default_factory=list)
    version: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    def groups(self) -> dict[str, list[APIEntry]]:
        """Group entries by their group name."""
        result: dict[str, list[APIEntry]] = {}
        for entry in self.entries:
            result.setdefault(entry.group, []).append(entry)
        return result


@component("gateway", version="0.2")
@provides("IGateway")
@requires(config="IConfig", logger="ILogger")
class APIGateway:
    """Collects transport config + runnable schemas, composes unified surfaces.

    On activation / rebuild:
      1. Calls kernel.runnables_by_component() for each transport
      2. Builds an APISurface per transport
      3. Transport adapters call get_surface("rest") to get the composed API
    """

    @lifecycle.activate
    def activate(self, rt):
        self._surfaces: dict[str, APISurface] = {}
        self._kernel = None  # set by kernel after boot via set_kernel()
        self._rt = rt

    def set_kernel(self, kernel) -> None:
        """Called by kernel after boot to give gateway access to runnable registry."""
        self._kernel = kernel
        self._rebuild()

    # Back-compat alias
    def set_lifecycle(self, lifecycle_mgr) -> None:
        """Legacy — use set_kernel() instead."""
        pass

    def _rebuild(self) -> None:
        """Rebuild all API surfaces from kernel.runnables_by_component()."""
        self._surfaces.clear()
        if self._kernel is None:
            return

        for transport in ["rest", "mcp", "cli"]:
            grouped = self._kernel.runnables_by_component(transport=transport)
            if not grouped:
                continue

            surface = APISurface(transport=transport)

            for comp_name, info in grouped.items():
                tc = info["transport_config"]
                group = tc.get("prefix", "").strip("/") or comp_name
                if tc.get("version") and not surface.version:
                    surface.version = tc["version"]

                for schema in info["schemas"]:
                    surface.entries.append(APIEntry(
                        runnable_name=f"{schema.provider}.{schema.name}",
                        short_name=schema.name,
                        group=group,
                        description=schema.description or "",
                        params_model=schema.params_model,
                        return_type=schema.return_type,
                        source_component=comp_name,
                        handler=schema.handler,
                    ))

            if surface.entries:
                self._surfaces[transport] = surface

    # ── Public API ──────────────────────────────────────────────

    def get_surface(self, transport: str) -> APISurface | None:
        """Get the composed API surface for a transport."""
        return self._surfaces.get(transport)

    def surfaces(self) -> dict[str, APISurface]:
        """All composed surfaces."""
        return dict(self._surfaces)

    @lifecycle.deactivate
    def deactivate(self, rt):
        self._surfaces.clear()
