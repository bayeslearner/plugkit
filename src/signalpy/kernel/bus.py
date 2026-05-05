"""Bus — invoke (request/response) + publish (events).

Locality-transparent: in-process by default, pluggable transport
for cross-process (JSON-RPC, Dapr, etc.).

The bus carries schemas alongside handlers (Phase 0a) and is observable
via a Signal tracking registered handler names (Phase 0b).

Reliability features (spec 010):
  - invoke() supports optional timeout (asyncio.TimeoutError on expiry)
  - invoke_nowait() for fire-and-forget invocation
  - Dead letter channel (__dead_letter__) for failed dispatches
"""
from __future__ import annotations

import asyncio
import logging
import time
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
class _CallStats:
    """Runtime statistics for a single bus target."""
    count: int = 0
    total_ms: float = 0.0
    errors: int = 0
    last_called: float = 0.0  # timestamp

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


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

    # Suffixes LLMs commonly hallucinate onto tool names.
    # Configurable: bus.junk_suffixes.add("_v2") if your deployment sees new ones.
    DEFAULT_JUNK_SUFFIXES = frozenset({
        "_ide", "_helper", "_fn", "_tool", "_func", "_api",
        "_action", "_command", "_op", "_handler", "_service",
    })

    DEAD_LETTER_CHANNEL = "__dead_letter__"

    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}    # target → async fn
        self._schemas: dict[str, HandlerSchema] = {}  # target → schema
        self._subscribers: dict[str, list[Callable]] = {}  # event_type → [handlers]
        self._transports: list[BusTransport] = []
        # Target routing: factory_name.runnable → {target → instance_name.runnable}
        self._target_routes: dict[str, dict[str, str]] = {}
        # Observable: Signal tracks registered handler names
        self.handler_signal: Signal[frozenset[str]] = Signal(frozenset())
        # Name resolution config (hallucination hardening)
        self.junk_suffixes: set[str] = set(self.DEFAULT_JUNK_SUFFIXES)
        self.fuzzy_max_distance: int = 2
        # Runtime call graph: target → stats
        self._call_stats: dict[str, _CallStats] = {}
        self.record_call_graph: bool = True  # disable for zero-overhead prod

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

    # ── Name resolution (hallucination hardening) ────────────────────

    def resolve_handler_name(self, name: str) -> str | None:
        """Try to resolve a hallucinated handler name to a real one.

        Resolution order:
          1. Strip known junk suffixes → exact match
          2. Flip hyphens ↔ underscores → exact match
          3. Fuzzy Levenshtein ≤ max_distance → unique best match only

        Returns the resolved name, or None if no match found.
        """
        # 1. Strip junk suffixes
        for suffix in self.junk_suffixes:
            if name.endswith(suffix):
                candidate = name[:-len(suffix)]
                if candidate in self._handlers:
                    log.debug("Resolved %r → %r (stripped %r)", name, candidate, suffix)
                    return candidate

        # 2. Flip hyphens ↔ underscores
        if "-" in name:
            flipped = name.replace("-", "_")
            if flipped in self._handlers:
                log.debug("Resolved %r → %r (hyphen→underscore)", name, flipped)
                return flipped
        if "_" in name:
            flipped = name.replace("_", "-")
            if flipped in self._handlers:
                log.debug("Resolved %r → %r (underscore→hyphen)", name, flipped)
                return flipped

        # 3. Fuzzy Levenshtein (unique best match only)
        if self.fuzzy_max_distance > 0:
            best_name: str | None = None
            best_dist = self.fuzzy_max_distance + 1
            ambiguous = False
            for registered in self._handlers:
                d = _levenshtein(name, registered)
                if d < best_dist:
                    best_dist = d
                    best_name = registered
                    ambiguous = False
                elif d == best_dist:
                    ambiguous = True
            if best_name and best_dist <= self.fuzzy_max_distance and not ambiguous:
                log.debug("Resolved %r → %r (fuzzy, distance=%d)", name, best_name, best_dist)
                return best_name

        return None

    # ── Invoke (request/response) ───────────────────────────────────

    def register_target_route(
        self, factory_runnable: str, target: str, instance_runnable: str,
    ) -> None:
        """Register a target route: factory.runnable + target → instance.runnable.

        Used for L3 Targeted trait — multiple instances of the same factory,
        routed by a 'target' param.
        """
        self._target_routes.setdefault(factory_runnable, {})[target] = instance_runnable

    async def invoke(self, target: str, params: dict | None = None,
                     *, timeout: float | None = None) -> Any:
        """Invoke a handler by target name.

        Args:
            target: Handler name (e.g. "my-app.search").
            params: Parameters dict passed to the handler.
            timeout: Optional timeout in seconds. Raises asyncio.TimeoutError
                     if the handler doesn't complete within the deadline.

        Routing order:
          1. Exact match on handler name
          2. Target routing: if params has 'target' and factory.runnable has routes
          3. Name resolution (hallucination hardening)
          4. Cross-process transports
        """
        p = params or {}
        handler = self._resolve_handler(target, p)

        if handler is not None:
            t0 = time.time() if self.record_call_graph else 0
            try:
                if timeout is not None:
                    result = await asyncio.wait_for(handler(p), timeout=timeout)
                else:
                    result = await handler(p)
                if self.record_call_graph:
                    self._record_call(target, time.time() - t0, error=False)
                return result
            except asyncio.TimeoutError:
                if self.record_call_graph:
                    self._record_call(target, time.time() - t0, error=True)
                self._dead_letter(target, p, reason="timeout")
                raise
            except Exception:
                if self.record_call_graph:
                    self._record_call(target, time.time() - t0, error=True)
                raise

        # Try cross-process transports
        for transport in self._transports:
            try:
                coro = transport.invoke(target, p)
                if timeout is not None:
                    return await asyncio.wait_for(coro, timeout=timeout)
                return await coro
            except NotImplementedError:
                continue
            except asyncio.TimeoutError:
                self._dead_letter(target, p, reason="timeout")
                raise

        self._dead_letter(target, p, reason="no_handler")
        raise KeyError(f"No handler for bus target {target!r} (local or remote)")

    def invoke_nowait(self, target: str, params: dict | None = None) -> None:
        """Schedule a handler invocation on the event loop. Fire-and-forget.

        Errors in the handler are logged and sent to the dead letter channel.
        Policy checks are the caller's responsibility (Runtime.invoke_nowait
        checks policy before calling this).
        """
        p = params or {}
        handler = self._resolve_handler(target, p)

        if handler is None:
            self._dead_letter(target, p, reason="no_handler")
            return

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._invoke_nowait_wrapper(target, handler, p))
        except RuntimeError:
            log.warning("invoke_nowait(%s): no running event loop", target)

    async def _invoke_nowait_wrapper(self, target: str, handler: Callable,
                                     params: dict) -> None:
        """Wrapper that catches and logs errors from fire-and-forget invocations."""
        try:
            await handler(params)
        except Exception as exc:
            log.exception("invoke_nowait handler error: %s", target)
            self._dead_letter(target, params, reason="handler_error", error=exc)

    def _resolve_handler(self, target: str, params: dict) -> Callable | None:
        """Resolve a target name to a handler function.

        Checks: exact match → L3 target routing → name resolution.
        Returns None if no local handler found (caller may try transports).
        """
        handler = self._handlers.get(target)
        if handler is not None:
            return handler

        # L3 target routing: factory.runnable + target param → instance.runnable
        target_value = params.get("target") if isinstance(params, dict) else None
        if target_value and target in self._target_routes:
            routes = self._target_routes[target]
            instance_target = routes.get(target_value)
            if instance_target:
                handler = self._handlers.get(instance_target)
                if handler:
                    return handler

        # Try name resolution (hallucination hardening)
        resolved = self.resolve_handler_name(target)
        if resolved:
            handler = self._handlers.get(resolved)
            if handler:
                return handler

        return None

    # ── Runtime call graph ─────────────────────────────────────────────

    def _record_call(self, target: str, elapsed: float, error: bool) -> None:
        """Record a completed invocation for the runtime call graph."""
        stats = self._call_stats.get(target)
        if stats is None:
            stats = _CallStats()
            self._call_stats[target] = stats
        stats.count += 1
        stats.total_ms += elapsed * 1000
        stats.last_called = time.time()
        if error:
            stats.errors += 1

    @property
    def call_graph(self) -> dict[str, dict[str, Any]]:
        """Return the runtime call graph — actual invocations with stats.

        Returns dict of target → {count, total_ms, avg_ms, errors, last_called}.
        """
        return {
            target: {
                "count": s.count,
                "total_ms": round(s.total_ms, 2),
                "avg_ms": round(s.avg_ms, 2),
                "errors": s.errors,
                "last_called": s.last_called,
            }
            for target, s in self._call_stats.items()
        }

    def reset_call_graph(self) -> None:
        """Reset all runtime call statistics."""
        self._call_stats.clear()

    # ── Dead letter channel ────────────────────────────────────────────

    def _dead_letter(self, target: str, params: dict | None,
                     reason: str, error: Exception | None = None) -> None:
        """Record a failed dispatch to the dead letter channel."""
        envelope = {
            "target": target,
            "params": params,
            "reason": reason,
            "error": str(error) if error else None,
            "timestamp": time.time(),
        }
        log.warning("Dead letter: %s → %s", target, reason)
        for handler in self._subscribers.get(self.DEAD_LETTER_CHANNEL, []):
            try:
                handler(self.DEAD_LETTER_CHANNEL, envelope)
            except Exception:
                pass  # dead letter handler must not throw

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


# ── Levenshtein distance (no external deps) ────────────────────────

def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,      # deletion
                curr[j] + 1,           # insertion
                prev[j] + (ca != cb),  # substitution
            ))
        prev = curr
    return prev[-1]
