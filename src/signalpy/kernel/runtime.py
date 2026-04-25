"""Reactive Runtime — the single object a component touches.

Built per-component by the kernel. Every injected service is wrapped in
a Signal, so reading self.rt.config tracks the caller as a dependency.
When the underlying service changes, consumers are notified automatically.

The bus is integrated directly — rt.invoke(), rt.publish(), rt.on()
instead of rt.bus.invoke(). Components never see the raw Bus.
"""
from __future__ import annotations

import logging
from fnmatch import fnmatch
from dataclasses import dataclass, field
from typing import Any, Callable

from signalpy.kernel.bus import Bus
from signalpy.kernel.reactive import Signal

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    """Reactive platform-injected context for a component.

    This is the ONLY object a component interacts with.
    Every injected service is a Signal — reading tracks dependencies.

    self.rt.config      → Signal.get() → tracks caller if in Effect/Computed
    self.rt.invoke()    → bus call (policy-checked)
    self.rt.publish()   → bus event
    """
    component_name: str
    factory_name: str
    properties: dict[str, Any]
    _bus: Bus = field(repr=False)

    # Policy (set by kernel, not by component)
    _invoke_allow: list[str] = field(default_factory=lambda: ["*"])
    _invoke_deny: list[str] = field(default_factory=list)
    _publish_allow: list[str] = field(default_factory=lambda: ["*"])
    _audit: bool = False

    # Reactive service signals (populated from @requires)
    _signals: dict[str, Signal] = field(default_factory=dict)

    # Spawn callback (set by kernel — creates child components)
    _spawn: Callable | None = field(default=None, repr=False)

    # Direct injection fields (for self.X typed access)
    _direct_inject_fields: dict[str, Any] = field(default_factory=dict)

    # ── TAKE: access injected services (REACTIVE) ──────────────

    def __getattr__(self, name: str) -> Any:
        """Access an injected service. REACTIVE — tracks the caller.

        If this is called inside an Effect or Computed, the caller
        becomes a subscriber of the underlying Signal. When the
        service changes, the caller is notified.
        """
        signals = object.__getattribute__(self, "_signals")
        if name in signals:
            return signals[name].get()  # ← reactive read: tracks consumer
        raise AttributeError(
            f"Runtime for {self.component_name!r} has no service {name!r}. "
            f"Available: {list(signals.keys())}"
        )

    def inject(self, attr_name: str, service: Any) -> None:
        """Inject or update a service. If Signal exists, updates it (notifies).
        Called by kernel, not by apps.
        """
        if attr_name in self._signals:
            self._signals[attr_name].set(service)  # notifies all consumers
        else:
            self._signals[attr_name] = Signal(service)

    def peek(self, name: str) -> Any:
        """Read a service without tracking (no reactive subscription)."""
        signals = object.__getattribute__(self, "_signals")
        if name in signals:
            return signals[name].peek()
        raise AttributeError(f"No service {name!r}")

    @property
    def services(self) -> dict[str, Any]:
        """Snapshot of all injected services (non-reactive read)."""
        return {k: s.peek() for k, s in self._signals.items()}

    # ── BUS: invoke / publish / subscribe (integrated) ──────────

    async def invoke(self, target: str, params: dict | None = None) -> Any:
        """Invoke another component's runnable via the bus. Policy-checked."""
        if not self._check_permission(target, self._invoke_allow, self._invoke_deny):
            raise PermissionError(
                f"Component {self.component_name!r} cannot invoke {target!r}"
            )
        if self._audit:
            log.info('{"action":"invoke","caller":"%s","target":"%s"}',
                     self.component_name, target)
        return await self._bus.invoke(target, params)

    async def publish(self, event_type: str, data: Any = None) -> None:
        """Publish an event to the bus. Policy-checked."""
        if not self._check_permission(event_type, self._publish_allow, []):
            raise PermissionError(
                f"Component {self.component_name!r} cannot publish {event_type!r}"
            )
        if self._audit:
            log.info('{"action":"publish","caller":"%s","event":"%s"}',
                     self.component_name, event_type)
        await self._bus.publish(event_type, data)

    def on(self, event_type: str, handler: Callable) -> None:
        """Subscribe to bus events."""
        self._bus.subscribe(event_type, handler)

    # ── BUS INTROSPECTION ────────────────────────────────────────

    @property
    def bus_handlers(self) -> list[str]:
        return self._bus.handlers

    @property
    def bus(self) -> Bus:
        """Raw bus access. For platform/adapter components only."""
        return self._bus

    # ── SPAWN ────────────────────────────────────────────────────

    async def spawn(
        self,
        factory_name: str,
        instance_name: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> Any:
        """Spawn a child component — lifecycle-bound to this parent."""
        if self._spawn is None:
            raise RuntimeError("spawn not available (kernel did not provide it)")
        return await self._spawn(
            factory_name, instance_name, properties,
            parent_name=self.component_name,
        )

    @staticmethod
    def _check_permission(name: str, allow: list[str], deny: list[str]) -> bool:
        if any(fnmatch(name, d) for d in deny):
            return False
        return any(fnmatch(name, a) for a in allow)
