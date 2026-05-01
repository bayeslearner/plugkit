"""Bus — invoke (request/response) + publish (events).

Locality-transparent: in-process by default, pluggable transport
for cross-process (JSON-RPC, Dapr, etc.).

The bus carries schemas alongside handlers (Phase 0a) and is observable
via a Signal tracking registered handler names (Phase 0b).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from signalpy.kernel.reactive import Signal

log = logging.getLogger(__name__)


@dataclass
class HandlerSchema:
    """Schema metadata for a bus handler — enables tool discovery."""
    name: str
    description: str = ""
    params_model: type | None = None
    return_type: type | None = None
    internal: bool = False
    destructive: bool = False
    requires_action: str = ""
    requires_role: str = ""
    provider: str = ""  # component that registered this handler

    @classmethod
    def from_runnable_def(cls, rd: Any, provider_name: str = "") -> HandlerSchema:
        """Build from a RunnableDef (kernel activation path)."""
        return cls(
            name=rd.name,
            description=rd.description,
            params_model=rd.params_model,
            return_type=rd.return_type,
            internal=rd.internal,
            destructive=rd.destructive,
            requires_action=rd.requires_action,
            requires_role=rd.requires_role,
            provider=provider_name,
        )


@dataclass
class BusTransport:
    """Pluggable transport for cross-process bus calls.

    The default transport is in-process (direct function call).
    Subclass for JSON-RPC, gRPC, etc.
    """
    name: str = "local"

    async def invoke(self, target: str, params: dict) -> Any:
        raise NotImplementedError(f"Transport {self.name} cannot invoke {target}")

    async def publish(self, event_type: str, data: Any) -> None:
        raise NotImplementedError(f"Transport {self.name} cannot publish {event_type}")


class Bus:
    """Cross-component communication bus.

    invoke(target, params) → result    Request/response (weak dependency)
    publish(event_type, data)          Fan-out to all subscribers
    subscribe(event_type, handler)     Register an event handler
    schemas()                          Discover all registered handlers + metadata

    The bus is observable: `self.handler_signal` is a Signal containing the
    frozenset of registered handler names. Any @effect that reads it will
    re-run when handlers are added or removed.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}    # target → async fn
        self._schemas: dict[str, HandlerSchema] = {}  # target → schema
        self._subscribers: dict[str, list[Callable]] = {}  # event_type → [handlers]
        self._transports: list[BusTransport] = []
        # Target routing: factory_name.runnable → {target → instance_name.runnable}
        self._target_routes: dict[str, dict[str, str]] = {}
        # Observable: Signal tracks registered handler names
        self.handler_signal: Signal[frozenset[str]] = Signal(frozenset())

    # ── Registration (called by kernel during activation) ───────────

    def register_handler(
        self,
        target: str,
        handler: Callable,
        schema: HandlerSchema | None = None,
    ) -> None:
        """Register an invocation handler for a target name.

        Optionally attach schema metadata for tool discovery.
        """
        if target in self._handlers:
            raise ValueError(f"Handler already registered for {target!r}")
        self._handlers[target] = handler
        if schema is not None:
            self._schemas[target] = schema
        self.handler_signal.set(frozenset(self._handlers.keys()))
        log.debug("Bus handler registered: %s", target)

    def unregister_handler(self, target: str) -> None:
        self._handlers.pop(target, None)
        self._schemas.pop(target, None)
        self.handler_signal.set(frozenset(self._handlers.keys()))

    def add_transport(self, transport: BusTransport) -> None:
        """Add a cross-process transport (for remote invocations)."""
        self._transports.append(transport)

    # ── Schema discovery ────────────────────────────────────────────

    def schemas(self, include_internal: bool = False) -> list[HandlerSchema]:
        """Return all registered handler schemas.

        This is the discovery mechanism — gateway, agent, and transports
        read from here to know what operations are available.
        """
        if include_internal:
            return list(self._schemas.values())
        return [s for s in self._schemas.values() if not s.internal]

    def get_schema(self, target: str) -> HandlerSchema | None:
        """Get schema for a specific handler."""
        return self._schemas.get(target)

    # ── Invoke (request/response) ───────────────────────────────────

    def register_target_route(
        self, factory_runnable: str, target: str, instance_runnable: str,
    ) -> None:
        """Register a target route: factory.runnable + target → instance.runnable.

        Used for L3 Targeted trait — multiple instances of the same factory,
        routed by a 'target' param.
        """
        self._target_routes.setdefault(factory_runnable, {})[target] = instance_runnable

    async def invoke(self, target: str, params: dict | None = None) -> Any:
        """Invoke a handler by target name.

        Routing order:
          1. Exact match on handler name
          2. Target routing: if params has 'target' and factory.runnable has routes
          3. Cross-process transports
        """
        p = params or {}
        handler = self._handlers.get(target)
        if handler is not None:
            return await handler(p)

        # L3 target routing: factory.runnable + target param → instance.runnable
        target_value = p.get("target")
        if target_value and target in self._target_routes:
            routes = self._target_routes[target]
            instance_target = routes.get(target_value)
            if instance_target:
                handler = self._handlers.get(instance_target)
                if handler:
                    return await handler(p)

        # Try cross-process transports
        for transport in self._transports:
            try:
                return await transport.invoke(target, p)
            except NotImplementedError:
                continue

        raise KeyError(f"No handler for bus target {target!r} (local or remote)")

    # ── Publish / Subscribe (events) ────────────────────────────────

    def subscribe(self, event_type: str, handler: Callable) -> None:
        """Subscribe to events of a given type."""
        self._subscribers.setdefault(event_type, []).append(handler)

    def unsubscribe(self, event_type: str, handler: Callable) -> None:
        handlers = self._subscribers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def publish(self, event_type: str, data: Any = None) -> None:
        """Publish an event to all local subscribers, then to transports.

        Supports topic wildcards: subscribing to "order.*" matches
        "order.created", "order.updated", etc. (fnmatch patterns).

        Handles both sync and async handlers.
        """
        import inspect as _inspect
        from fnmatch import fnmatch
        # Collect handlers: exact match + wildcard matches
        handlers = list(self._subscribers.get(event_type, []))
        for pattern, pattern_handlers in self._subscribers.items():
            if pattern != event_type and fnmatch(event_type, pattern):
                handlers.extend(pattern_handlers)
        for handler in handlers:
            try:
                if _inspect.iscoroutinefunction(handler):
                    await handler(event_type, data)
                else:
                    result = handler(event_type, data)
                    # Safety net: if a non-async handler returns a coroutine
                    if hasattr(result, "__await__"):
                        await result
            except Exception:
                log.exception("Event handler error for %s", event_type)

        # Also publish to remote transports
        for transport in self._transports:
            try:
                await transport.publish(event_type, data)
            except (NotImplementedError, Exception):
                pass

    # ── Inspection ──────────────────────────────────────────────────

    @property
    def handlers(self) -> list[str]:
        return list(self._handlers.keys())

    @property
    def event_types(self) -> list[str]:
        return list(self._subscribers.keys())

    def has_handler(self, target: str) -> bool:
        return target in self._handlers
