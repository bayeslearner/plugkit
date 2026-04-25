"""Reactive Service Registry — the give/take hub.

Components provide services (implementations of contracts) and require
services from other components. The registry wires them together.

Reactive: each contract slot is a Signal. When a service is provided
or removed, the Signal fires and all consumers are notified.

Ref counting: tracks who is using each service. Hot-remove is blocked
while a service has active consumers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from signalpy.kernel.reactive import Signal

log = logging.getLogger(__name__)


@dataclass
class ServiceEntry:
    """A registered service."""
    contract: str
    implementation: Any
    provider_name: str
    properties: dict[str, Any] = field(default_factory=dict)
    factory: bool = False


class ServiceRegistry:
    """Reactive provide / require / query services.

    Each contract has a Signal[list[ServiceEntry]]. When services are
    added or removed, the Signal fires. ReactiveRuntime reads from
    these Signals, so components automatically see changes.
    """

    def __init__(self) -> None:
        self._services: dict[str, list[ServiceEntry]] = {}
        self._listeners: list[Callable] = []
        # Ref counting: (contract, consumer_name) → count
        self._ref_counts: dict[tuple[str, str], int] = {}
        # Service Factory cache: (contract, consumer_name) → instance
        self._factory_cache: dict[tuple[str, str], Any] = {}

    # ── Provide / Unprovide ────────────────────────────────────

    def provide(
        self,
        contract: str,
        implementation: Any,
        provider_name: str,
        properties: dict[str, Any] | None = None,
        factory: bool = False,
    ) -> ServiceEntry:
        """Register a service implementation for a contract."""
        entry = ServiceEntry(
            contract=contract,
            implementation=implementation,
            provider_name=provider_name,
            properties=properties or {},
            factory=factory,
        )
        self._services.setdefault(contract, []).append(entry)
        log.debug("Service provided: %s by %s", contract, provider_name)
        self._notify("provide", entry)
        return entry

    def unprovide(self, entry: ServiceEntry) -> None:
        """Remove a service registration."""
        entries = self._services.get(entry.contract, [])
        if entry in entries:
            entries.remove(entry)
            log.debug("Service removed: %s by %s", entry.contract, entry.provider_name)
            self._notify("unprovide", entry)

    # ── Require ────────────────────────────────────────────────

    def require(self, contract: str, **filter_props: Any) -> Any:
        """Get a single service (highest-ranked). Raises KeyError if none."""
        entries = self._services.get(contract, [])
        matches = [
            e for e in entries
            if all(e.properties.get(k) == v for k, v in filter_props.items())
        ]
        if not matches:
            raise KeyError(
                f"No service provides {contract!r}"
                + (f" with {filter_props}" if filter_props else "")
            )
        matches.sort(key=lambda e: e.properties.get("service.ranking", 0))
        return matches[0].implementation

    def require_optional(self, contract: str, **filter_props: Any) -> Any | None:
        try:
            return self.require(contract, **filter_props)
        except KeyError:
            return None

    def require_all(self, contract: str) -> list[Any]:
        """All implementations, sorted by ranking."""
        entries = self._services.get(contract, [])
        sorted_entries = sorted(entries, key=lambda e: e.properties.get("service.ranking", 0))
        return [e.implementation for e in sorted_entries]

    def require_best(self, contract: str) -> Any:
        return self.require(contract)

    def require_map(self, contract: str, key_prop: str) -> dict[str, Any]:
        """All services as a dict keyed by a property value."""
        entries = self._services.get(contract, [])
        result = {}
        for entry in entries:
            key_val = entry.properties.get(key_prop)
            if key_val is not None:
                result[key_val] = entry.implementation
        return result

    def require_for(self, contract: str, consumer_name: str) -> Any:
        """Factory-aware: calls get_service(consumer) if factory."""
        entries = self._services.get(contract, [])
        if not entries:
            raise KeyError(f"No service provides {contract!r}")
        sorted_entries = sorted(entries, key=lambda e: e.properties.get("service.ranking", 0))
        entry = sorted_entries[0]
        if entry.factory:
            cache_key = (contract, consumer_name)
            if cache_key not in self._factory_cache:
                if hasattr(entry.implementation, "get_service"):
                    self._factory_cache[cache_key] = entry.implementation.get_service(consumer_name)
                else:
                    return entry.implementation
            return self._factory_cache[cache_key]
        return entry.implementation

    def unget_service(self, contract: str, consumer_name: str) -> None:
        """Release a factory-produced service for a consumer."""
        cache_key = (contract, consumer_name)
        instance = self._factory_cache.pop(cache_key, None)
        if instance is not None:
            entries = self._services.get(contract, [])
            for entry in entries:
                if entry.factory and hasattr(entry.implementation, "unget_service"):
                    try:
                        entry.implementation.unget_service(consumer_name, instance)
                    except Exception:
                        log.exception("unget_service error for %s", contract)

    def entries_for(self, contract: str) -> list[ServiceEntry]:
        return list(self._services.get(contract, []))

    # ── Ref counting ───────────────────────────────────────────

    def acquire(self, contract: str, consumer: str) -> None:
        """Increment ref count for a service consumer."""
        key = (contract, consumer)
        self._ref_counts[key] = self._ref_counts.get(key, 0) + 1

    def release(self, contract: str, consumer: str) -> None:
        """Decrement ref count."""
        key = (contract, consumer)
        count = self._ref_counts.get(key, 0)
        if count > 1:
            self._ref_counts[key] = count - 1
        else:
            self._ref_counts.pop(key, None)

    def ref_count(self, contract: str) -> int:
        """Total ref count across all consumers for a contract."""
        return sum(v for (c, _), v in self._ref_counts.items() if c == contract)

    # ── Query ──────────────────────────────────────────────────

    def query(self, contract: str | None = None) -> list[ServiceEntry]:
        if contract:
            return list(self._services.get(contract, []))
        return [e for entries in self._services.values() for e in entries]

    def has(self, contract: str) -> bool:
        return bool(self._services.get(contract))

    # ── Listeners ──────────────────────────────────────────────

    def on_change(self, listener: Callable) -> None:
        """Register a listener for provide/unprovide events."""
        self._listeners.append(listener)

    def _notify(self, event: str, entry: ServiceEntry) -> None:
        for listener in self._listeners:
            try:
                listener(event, entry)
            except Exception:
                log.exception("Listener error on %s %s", event, entry.contract)

    def __len__(self) -> int:
        return sum(len(v) for v in self._services.values())
