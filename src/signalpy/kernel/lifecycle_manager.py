"""Lifecycle manager — state machine + dependency-ordered activation.

Every component transitions through:
  Discovered → Resolved → Activating → Active → Deactivating → Stopped

Errored is a terminal state reachable from Activating.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from signalpy.kernel.component import ComponentMeta, _finalize_meta, get_meta

log = logging.getLogger(__name__)


class State(Enum):
    DISCOVERED = auto()
    RESOLVED = auto()
    ACTIVATING = auto()
    ACTIVE = auto()
    DEACTIVATING = auto()
    STOPPED = auto()
    ERRORED = auto()


@dataclass
class ComponentInstance:
    """A live instance of a component — factory class + properties + state."""
    factory_class: type
    meta: ComponentMeta
    name: str                           # instance name (may differ from factory_name)
    properties: dict[str, Any] = field(default_factory=dict)
    state: State = State.DISCOVERED
    instance: Any = None                # the actual POPO instance
    error: Exception | None = None
    parent: str | None = None           # parent instance name (component tree)
    children: list[str] = field(default_factory=list)  # child instance names
    # Reactive disposables — Effects and Computeds created during activation
    _disposables: list = field(default_factory=list)


class LifecycleManager:
    """Manages component instances through their lifecycle."""

    def __init__(self) -> None:
        self._factories: dict[str, type] = {}               # factory_name → class
        self._instances: dict[str, ComponentInstance] = {}    # instance_name → instance
        self._activation_order: list[str] = []               # for reverse shutdown

    # ── Factory registration ────────────────────────────────────────

    def register_factory(self, cls: type) -> ComponentMeta:
        """Register a @component-decorated class as a factory."""
        meta = _finalize_meta(cls)
        if meta.factory_name in self._factories:
            raise ValueError(f"Factory {meta.factory_name!r} already registered")
        self._factories[meta.factory_name] = cls
        log.debug("Factory registered: %s", meta.factory_name)
        return meta

    def replace_factory(self, cls: type) -> ComponentMeta:
        """Replace an existing factory with a new class (same factory name)."""
        meta = _finalize_meta(cls)
        if meta.factory_name not in self._factories:
            raise ValueError(f"Factory {meta.factory_name!r} not registered — use register_factory")
        self._factories[meta.factory_name] = cls
        log.debug("Factory replaced: %s", meta.factory_name)
        return meta

    def remove_instance(self, name: str) -> None:
        """Remove an instance from tracking (after deactivation)."""
        self._instances.pop(name, None)
        if name in self._activation_order:
            self._activation_order.remove(name)

    # ── Instantiation ───────────────────────────────────────────────

    def instantiate(
        self,
        factory_name: str,
        instance_name: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> ComponentInstance:
        """Create a component instance from a registered factory."""
        cls = self._factories.get(factory_name)
        if cls is None:
            raise TypeError(f"Unknown factory {factory_name!r}")

        meta = get_meta(cls)
        name = instance_name or factory_name

        if name in self._instances:
            raise ValueError(f"Instance {name!r} already exists")

        ci = ComponentInstance(
            factory_class=cls,
            meta=meta,
            name=name,
            properties=properties or {},
            state=State.DISCOVERED,
        )
        self._instances[name] = ci
        log.debug("Instance created: %s (factory=%s)", name, factory_name)
        return ci

    # ── Dependency resolution ───────────────────────────────────────

    def resolve_all(self) -> list[str]:
        """Toposort all Discovered instances by dependencies.

        Returns activation order (list of instance names).
        """
        # Build adjacency: instance → [instances it depends on]
        dep_graph: dict[str, set[str]] = {}
        for name, ci in self._instances.items():
            deps: set[str] = set()
            for dep_factory in ci.meta.dependencies:
                # Find instances of that factory
                for other_name, other_ci in self._instances.items():
                    if other_ci.meta.factory_name == dep_factory:
                        deps.add(other_name)
            dep_graph[name] = deps

        # Kahn's algorithm on the dependency graph.
        # dep_graph[A] = {B, C} means A depends on B and C.
        # We want B and C to activate BEFORE A.
        # In-degree counts how many deps each node has (not how many depend on it).
        in_degree = {n: len(deps) for n, deps in dep_graph.items()}

        queue = deque(sorted(n for n, deg in in_degree.items() if deg == 0))
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            # For every other node that depends on this node, reduce its in-degree
            ready: list[str] = []
            for n, deps in dep_graph.items():
                if node in deps:
                    in_degree[n] -= 1
                    if in_degree[n] == 0:
                        ready.append(n)
            queue.extend(sorted(ready))

        if len(order) != len(dep_graph):
            missing = set(dep_graph) - set(order)
            raise RuntimeError(f"Circular dependency involving: {missing}")

        # Mark all as resolved
        for name in order:
            self._instances[name].state = State.RESOLVED
        self._activation_order = order
        return order

    # ── Activation ──────────────────────────────────────────────────

    async def activate(self, name: str, runtime_builder) -> None:
        """Activate a single component instance."""
        ci = self._instances[name]
        if ci.state != State.RESOLVED:
            raise RuntimeError(f"Cannot activate {name}: state is {ci.state.name}")

        ci.state = State.ACTIVATING
        try:
            # Create the POPO instance
            ci.instance = ci.factory_class()

            # Build the runtime object (injected services)
            rt = runtime_builder(ci)

            # Attach runtime — components access services via self.rt.*
            ci.instance.rt = rt

            # Direct self-injection: set annotated fields on the instance
            for field_name, service in getattr(rt, '_direct_inject_fields', {}).items():
                setattr(ci.instance, field_name, service)

            # Call activate callback — supports both sync and async,
            # and both (self, rt) and (self,) signatures.
            if ci.meta.activate_fn:
                import inspect
                sig = inspect.signature(ci.meta.activate_fn)
                if len(sig.parameters) >= 2:  # (self, rt)
                    result = ci.meta.activate_fn(ci.instance, rt)
                else:  # (self,)
                    result = ci.meta.activate_fn(ci.instance)
                if ci.meta.activate_is_async:
                    await result

            ci.state = State.ACTIVE
            log.info("Activated: %s", name)

        except Exception as exc:
            ci.state = State.ERRORED
            ci.error = exc
            log.exception("Activation failed: %s", name)
            raise

    async def activate_all(self, runtime_builder) -> None:
        """Activate all resolved instances in dependency order."""
        order = self.resolve_all()
        for name in order:
            await self.activate(name, runtime_builder)

    # ── Deactivation ────────────────────────────────────────────────

    async def deactivate(self, name: str, runtime_builder) -> None:
        """Deactivate a component instance — children first, then self."""
        ci = self._instances.get(name)
        if ci is None or ci.state != State.ACTIVE:
            return

        # Deactivate children first (reverse order)
        for child_name in reversed(list(ci.children)):
            await self.deactivate(child_name, runtime_builder)

        ci.state = State.DEACTIVATING

        # Dispose all reactive Effects and Computeds
        for disposable in ci._disposables:
            if hasattr(disposable, 'dispose'):
                disposable.dispose()
        ci._disposables.clear()

        try:
            if ci.meta.deactivate_fn and ci.instance:
                rt = runtime_builder(ci)
                import inspect
                sig = inspect.signature(ci.meta.deactivate_fn)
                if len(sig.parameters) >= 2:  # (self, rt)
                    result = ci.meta.deactivate_fn(ci.instance, rt)
                else:  # (self,)
                    result = ci.meta.deactivate_fn(ci.instance)
                if ci.meta.deactivate_is_async:
                    await result
        except Exception:
            log.exception("Deactivation error: %s", name)
        finally:
            ci.state = State.STOPPED
            log.info("Deactivated: %s", name)

    async def shutdown(self, runtime_builder) -> None:
        """Deactivate all active instances in reverse activation order."""
        for name in reversed(self._activation_order):
            await self.deactivate(name, runtime_builder)

    # ── Retry errored ────────────────────────────────────────────────

    async def retry_erroneous(self, name: str, runtime_builder) -> None:
        """Retry activation of an ERRORED component.

        iPOPO equivalent: retry_erroneous()
        Resets the component to RESOLVED and attempts activation again.
        """
        ci = self._instances.get(name)
        if ci is None:
            raise KeyError(f"No instance named {name!r}")
        if ci.state != State.ERRORED:
            raise RuntimeError(
                f"Cannot retry {name}: state is {ci.state.name}, not ERRORED"
            )
        ci.state = State.RESOLVED
        ci.error = None
        ci.instance = None
        await self.activate(name, runtime_builder)

    # ── Inspection ──────────────────────────────────────────────────

    def get_instance(self, name: str) -> ComponentInstance | None:
        return self._instances.get(name)

    def all_instances(self) -> list[ComponentInstance]:
        return list(self._instances.values())

    def active_instances(self) -> list[ComponentInstance]:
        return [ci for ci in self._instances.values() if ci.state == State.ACTIVE]

    @property
    def factories(self) -> dict[str, type]:
        return dict(self._factories)
