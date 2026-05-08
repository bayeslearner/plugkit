"""Lifecycle manager — state machine + dependency-ordered activation.

Every component transitions through:
  Discovered → Resolved → Activating → Active → Deactivating → Stopped

Errored is reachable from Activating. With supervision, a supervised
component transitions: Errored → Restarting → Resolved → Activating
(with backoff delay between attempts).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from signalpy.kernel.component import ComponentMeta, SupervisionDef, _finalize_meta, get_meta

log = logging.getLogger(__name__)


class State(Enum):
    DISCOVERED = auto()
    RESOLVED = auto()
    ACTIVATING = auto()
    ACTIVE = auto()
    DEACTIVATING = auto()
    STOPPED = auto()
    ERRORED = auto()
    RESTARTING = auto()  # supervised retry in progress (with backoff)


class SupervisionEscalation(Exception):
    """Raised by a supervision callback to escalate failure to the parent supervisor."""
    def __init__(self, child_name: str, original_error: Exception) -> None:
        self.child_name = child_name
        self.original_error = original_error
        super().__init__(f"Supervision escalation: {child_name}: {original_error}")


@dataclass
class RestartTracker:
    """Tracks restart attempts within a sliding window."""
    timestamps: list[float] = field(default_factory=list)

    def record(self, now: float) -> None:
        self.timestamps.append(now)

    def count_within(self, now: float, window_seconds: float) -> int:
        cutoff = now - window_seconds
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        return len(self.timestamps)


@dataclass
class SupervisionContext:
    """Passed to supervision callbacks so they can inspect and influence restarts."""
    child_name: str
    child_factory: str
    error: Exception
    attempt: int
    restarts_in_window: int
    strategy: str
    _properties_override: dict | None = None

    def update_properties(self, props: dict) -> None:
        """Override child properties for the restart attempt."""
        self._properties_override = props


def _compute_delay(base: float, attempt: int, strategy: str) -> float:
    """Compute backoff delay for a given attempt."""
    if strategy == "constant":
        return base
    elif strategy == "linear":
        return base * attempt
    elif strategy == "exponential":
        return base * (2 ** (attempt - 1))
    return base


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
    # Supervision restart tracking
    _restart_tracker: RestartTracker = field(default_factory=RestartTracker)


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

    def unregister_factory(self, factory_name: str) -> None:
        """Unregister a factory so it can be re-added later."""
        self._factories.pop(factory_name, None)
        log.debug("Factory unregistered: %s", factory_name)

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
        """Toposort all Discovered instances by @requires contracts.

        For each scalar non-optional non-aggregate `@requires(x=IFoo)`
        we add an edge to every component that `@provides("IFoo")`.
        Aggregate (`list[X]`) and optional requirements are excluded:
        both are reactive paths that boot empty / None and get filled
        in by the registry's change listener as providers come online.

        `@requires` is the single source of truth for boot order.
        There is no separate `depends=` — if you need ordering you
        need a contract.

        Returns activation order (list of instance names).
        """
        # Map contract name → list of provider instance names
        providers_by_contract: dict[str, list[str]] = {}
        for name, ci in self._instances.items():
            for contract in ci.meta.provides:
                providers_by_contract.setdefault(contract, []).append(name)

        # Build adjacency: instance → [instances it depends on]
        dep_graph: dict[str, set[str]] = {}
        for name, ci in self._instances.items():
            deps: set[str] = set()
            for req in ci.meta.requirements:
                if req.aggregate or req.optional:
                    continue  # reactive paths — don't block boot
                providers = providers_by_contract.get(req.contract, [])
                # Don't self-edge if a component @provides AND @requires
                # the same contract (rare but legal — e.g. decorator pattern).
                providers = [p for p in providers if p != name]
                deps.update(providers)
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

    # ── Supervision ─────────────────────────────────────────────────

    def _find_supervisor(self, ci: ComponentInstance) -> ComponentInstance | None:
        """Walk up the parent chain to find the nearest ancestor with a supervision_def."""
        current = ci
        while current.parent:
            parent = self._instances.get(current.parent)
            if parent is None:
                break
            if parent.meta.supervision_def is not None:
                return parent
            current = parent
        return None

    async def activate_supervised(self, name: str, runtime_builder,
                                  register_bus: Any = None) -> None:
        """Activate a component with supervision support.

        If activation fails and the component has a supervisor parent,
        the supervisor's strategy handles retry/restart.
        If no supervisor, behaves identically to activate() (error propagates).
        If supervision escalates (supervisor itself fails), raises
        SupervisionEscalation so the caller knows.

        Args:
            register_bus: Optional callable(ci) to register bus handlers after
                          successful activation. Passed from kernel.boot().
        """
        ci = self._instances.get(name)
        if ci is None:
            raise KeyError(f"No instance named {name!r}")

        try:
            await self.activate(name, runtime_builder)
            if register_bus and ci.state == State.ACTIVE:
                register_bus(ci)
        except Exception as exc:
            # Check for supervisor
            supervisor = self._find_supervisor(ci)
            if supervisor is None:
                raise
            await self._handle_activation_failure(
                ci, exc, supervisor, runtime_builder, register_bus
            )
            # If escalation marked the supervisor as ERRORED, propagate
            if supervisor.state == State.ERRORED:
                raise SupervisionEscalation(
                    ci.name, exc
                ) from exc

    async def _handle_activation_failure(
        self,
        ci: ComponentInstance,
        error: Exception,
        supervisor_ci: ComponentInstance,
        runtime_builder,
        register_bus: Any = None,
    ) -> None:
        """Handle activation failure with supervision."""
        sup_def = supervisor_ci.meta.supervision_def
        if sup_def is None:
            return

        now = time.monotonic()
        ci._restart_tracker.record(now)
        restarts = ci._restart_tracker.count_within(now, sup_def.within_seconds)

        if restarts > sup_def.max_restarts:
            log.error(
                "Supervisor %s: max restarts (%d) exceeded for %s within %.0fs",
                supervisor_ci.name, sup_def.max_restarts, ci.name,
                sup_def.within_seconds,
            )
            await self._escalate_to_parent(supervisor_ci, ci.name, error, runtime_builder)
            return

        attempt = restarts
        context = SupervisionContext(
            child_name=ci.name,
            child_factory=ci.meta.factory_name,
            error=error,
            attempt=attempt,
            restarts_in_window=restarts,
            strategy=sup_def.strategy,
        )

        # Call the supervisor's callback
        try:
            should_restart = sup_def.fn(supervisor_ci.instance, ci.name,
                                        error, attempt, context)
            if sup_def.is_async:
                should_restart = await should_restart
        except SupervisionEscalation:
            await self._escalate_to_parent(supervisor_ci, ci.name, error, runtime_builder)
            return
        except Exception:
            log.exception("Supervision callback error for %s", ci.name)
            return

        if not should_restart:
            log.info("Supervisor %s declined restart for %s", supervisor_ci.name, ci.name)
            return

        # Apply strategy
        ci.state = State.RESTARTING
        delay = _compute_delay(sup_def.base_delay, attempt, sup_def.backoff)
        delay = min(delay, 60.0)

        log.info(
            "Supervisor %s: restarting %s in %.1fs (attempt %d/%d, strategy=%s)",
            supervisor_ci.name, ci.name, delay, attempt, sup_def.max_restarts,
            sup_def.strategy,
        )

        await asyncio.sleep(delay)

        if sup_def.strategy == "one_for_one":
            await self._restart_one(ci, context, runtime_builder, register_bus)
        elif sup_def.strategy == "one_for_all":
            await self._restart_all_children(
                supervisor_ci, runtime_builder, register_bus
            )
        elif sup_def.strategy == "rest_for_one":
            await self._restart_from(
                supervisor_ci, ci, runtime_builder, register_bus
            )

    async def _restart_one(self, ci: ComponentInstance, context: SupervisionContext,
                           runtime_builder, register_bus=None) -> None:
        """Restart a single child component (one_for_one)."""
        ci.state = State.RESOLVED
        ci.error = None
        ci.instance = None
        if context._properties_override:
            ci.properties.update(context._properties_override)
        try:
            await self.activate(ci.name, runtime_builder)
            if register_bus and ci.state == State.ACTIVE:
                register_bus(ci)
        except Exception as exc:
            ci.state = State.ERRORED
            ci.error = exc
            # Recursive: try supervision again
            supervisor = self._find_supervisor(ci)
            if supervisor:
                await self._handle_activation_failure(
                    ci, exc, supervisor, runtime_builder, register_bus
                )

    async def _restart_all_children(self, supervisor_ci: ComponentInstance,
                                    runtime_builder, register_bus=None) -> None:
        """Restart ALL children of the supervisor (one_for_all)."""
        # Deactivate all active children in reverse order
        for child_name in reversed(list(supervisor_ci.children)):
            child_ci = self._instances.get(child_name)
            if child_ci and child_ci.state == State.ACTIVE:
                await self.deactivate(child_name, runtime_builder)

        # Re-activate all in original order
        for child_name in supervisor_ci.children:
            child_ci = self._instances.get(child_name)
            if child_ci:
                child_ci.state = State.RESOLVED
                child_ci.error = None
                child_ci.instance = None
                try:
                    await self.activate(child_name, runtime_builder)
                    if register_bus and child_ci.state == State.ACTIVE:
                        register_bus(child_ci)
                except Exception as exc:
                    log.error("Failed to restart child %s: %s", child_name, exc)
                    child_ci.state = State.ERRORED
                    child_ci.error = exc

    async def _restart_from(self, supervisor_ci: ComponentInstance,
                            failed_ci: ComponentInstance,
                            runtime_builder, register_bus=None) -> None:
        """Restart the failed child + everything started after it (rest_for_one)."""
        children = list(supervisor_ci.children)
        idx = children.index(failed_ci.name) if failed_ci.name in children else 0
        to_restart = children[idx:]

        # Deactivate in reverse
        for child_name in reversed(to_restart):
            child_ci = self._instances.get(child_name)
            if child_ci and child_ci.state in (
                State.ACTIVE, State.ERRORED, State.RESTARTING
            ):
                if child_ci.state == State.ACTIVE:
                    await self.deactivate(child_name, runtime_builder)
                child_ci.state = State.RESOLVED
                child_ci.error = None
                child_ci.instance = None

        # Re-activate in order
        for child_name in to_restart:
            child_ci = self._instances.get(child_name)
            if child_ci:
                try:
                    await self.activate(child_name, runtime_builder)
                    if register_bus and child_ci.state == State.ACTIVE:
                        register_bus(child_ci)
                except Exception as exc:
                    log.error("Failed to restart child %s: %s", child_name, exc)
                    child_ci.state = State.ERRORED
                    child_ci.error = exc

    async def _escalate_to_parent(self, supervisor_ci: ComponentInstance,
                                  child_name: str, error: Exception,
                                  runtime_builder) -> None:
        """Supervisor gives up — escalate to its own supervisor."""
        escalation = SupervisionEscalation(child_name, error)
        supervisor_ci.state = State.ERRORED
        supervisor_ci.error = escalation
        log.error(
            "Supervisor %s escalated failure from %s",
            supervisor_ci.name, child_name,
        )
        # Recursive: check if this supervisor itself has a supervisor
        parent_supervisor = self._find_supervisor(supervisor_ci)
        if parent_supervisor:
            await self._handle_activation_failure(
                supervisor_ci, escalation, parent_supervisor, runtime_builder
            )

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
