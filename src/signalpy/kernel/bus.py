"""Event Bus — publish/subscribe for loose-coupled events.

Fan-out event delivery with wildcard matching (fnmatch).
Pluggable transports for cross-process event relay.
Dead letter channel for failed event delivery.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable


log = logging.getLogger(__name__)


@dataclass
class HandlerSchema:
    """Schema metadata for a runnable — enables tool discovery.

    The handler field carries a direct reference to the bound method,
    populated at component activation time. Consumers call
    schema.handler(params) directly.
    """
    name: str
    description: str = ""
    params_model: type | None = None
    return_type: type | None = None
    internal: bool = False
    destructive: bool = False
    transports: list[str] | None = None  # None = all; ["native"] = direct only
    requires_action: str = ""
    requires_role: str = ""
    provider: str = ""  # component that provides this runnable
    handler: Callable | None = None  # bound method ref, set at activation

    def visible_on(self, transport: str) -> bool:
        """Check if this schema should be exposed on a given transport."""
        if self.internal and self.transports is None:
            return transport == "native"
        if self.transports is None:
            return True
        return transport in self.transports

    @classmethod
    def from_runnable_def(cls, rd: Any, provider_name: str = "",
                          handler: Callable | None = None) -> "HandlerSchema":
        """Build from a RunnableDef (kernel activation path)."""
        return cls(
            name=rd.name,
            description=rd.description,
            params_model=rd.params_model,
            return_type=rd.return_type,
            internal=rd.internal,
            destructive=rd.destructive,
            transports=rd.transports,
            requires_action=rd.requires_action,
            requires_role=rd.requires_role,
            provider=provider_name,
            handler=handler,
        )


@dataclass
class BusTransport:
    """Pluggable transport for cross-process event relay.

    Subclass for NATS, Redis, etc.
    """
    name: str = "local"

    async def publish(self, event_type: str, data: Any) -> None:
        raise NotImplementedError(f"Transport {self.name} cannot publish {event_type}")


class Bus:
    """Event bus — publish/subscribe with wildcard matching.

    publish(event_type, data)          Fan-out to all subscribers
    subscribe(event_type, handler)     Register an event handler
    unsubscribe(event_type, handler)   Remove an event handler
    """

    DEAD_LETTER_CHANNEL = "__dead_letter__"

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable]] = {}
        self._transports: list[BusTransport] = []

    def add_transport(self, transport: BusTransport) -> None:
        """Add a cross-process transport for event relay."""
        self._transports.append(transport)

    # ── Publish / Subscribe ────────────────────────────────────────

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

    # ── Dead letter channel ────────────────────────────────────────

    def _dead_letter(self, event_type: str, data: Any = None,
                     reason: str = "", error: Exception | None = None) -> None:
        """Record a failed event delivery to the dead letter channel."""
        envelope = {
            "event_type": event_type,
            "data": data,
            "reason": reason,
            "error": str(error) if error else None,
            "timestamp": time.time(),
        }
        log.warning("Dead letter: %s → %s", event_type, reason)
        for handler in self._subscribers.get(self.DEAD_LETTER_CHANNEL, []):
            try:
                handler(self.DEAD_LETTER_CHANNEL, envelope)
            except Exception:
                pass

    # ── Inspection ──────────────────────────────────────────────────

    @property
    def event_types(self) -> list[str]:
        return list(self._subscribers.keys())
