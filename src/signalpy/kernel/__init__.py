"""SignalPy — Reactive Microkernel v2.

A Signal-based reactive component kernel for backend services.
The reactivity engine IS the foundation — components, traits, and
services are built on Signal/Computed/Effect primitives.

Usage:

    from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle, computed, effect

    @component("my-app", version="1.0")
    @requires(config=IConfig)
    class MyApp:
        @lifecycle.activate
        def activate(self):
            pass

        @computed
        def base_url(self):
            return self.rt.config.get("url", "http://localhost")

        @effect
        async def on_config_change(self):
            await self._reconnect(self.base_url)

        @runnable("greet", params=GreetParams, description="Say hello")
        async def greet(self, params):
            return {"url": self.base_url}

    kernel = Kernel()
    kernel.discover([MyApp])
    await kernel.boot()
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from enum import Enum, auto
from typing import Any, Callable

from signalpy.kernel.bus import Bus
from signalpy.kernel.component import (
    APIDef,
    ComponentMeta,
    ComputedDef,
    EffectDef,
    KindDef,
    PropertyDef,
    RequirementDef,
    RunnableDef,
    SkillDef,
    SubscribeDef,
    _contract_name,
    _finalize_meta,
    _is_contract_type,
    api,
    component,
    computed,
    effect,
    get_meta,
    has_meta,
    kind,
    lifecycle,
    prop,
    provides,
    requires,
    runnable,
    skill,
    subscribe,
)
from signalpy.kernel.lifecycle_manager import ComponentInstance, LifecycleManager, State
from signalpy.kernel.reactive import (
    Computed as ReactiveComputed,
    Effect as ReactiveEffect,
    Signal,
    batch,
    is_stale,
)
from signalpy.kernel.registry import ServiceRegistry
from signalpy.kernel.runtime import Runtime
from signalpy.kernel.traits import Level, TraitRegistry

log = logging.getLogger(__name__)


class Params(dict):
    """Dict subclass with attribute access for runnable params."""
    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(f"Params has no field {name!r}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class KernelState(Enum):
    CREATED = auto()
    BOOTING = auto()
    HEALTHY = auto()
    DRAINING = auto()
    STOPPED = auto()


class Kernel:
    """The reactive microkernel — trait runtime, lifecycle, registry, bus.

    v2: reactivity is the foundation. Services are wrapped in Signals.
    @computed and @effect declarators auto-track dependencies.
    Changes propagate through the reactive graph automatically.
    """

    def __init__(self, policies: dict[str, dict] | None = None) -> None:
        self.traits = TraitRegistry()
        self.registry = ServiceRegistry()
        self.bus = Bus()
        self.lifecycle = LifecycleManager()
        self.kinds: dict[str, KindDef] = {}
        self.skills: dict[str, SkillDef] = {}
        self._policies: dict[str, dict] = policies or {}
        self._state = KernelState.CREATED
        self._inflight = 0
        self._drain_event: asyncio.Event | None = None
        self._register_builtin_traits()

    @property
    def state(self) -> KernelState:
        return self._state

    @property
    def healthy(self) -> bool:
        return self._state == KernelState.HEALTHY

    def set_policy(self, component_name: str, policy: dict) -> None:
        self._policies[component_name] = policy

    def _setup_reactive_propagation(self) -> None:
        """Set up a global registry listener that propagates service changes
        to all consumer Runtimes.

        When a service is provided or removed, find all active components
        that require that contract and update their Runtime Signals.
        This is what makes the reactive graph work end-to-end.
        """
        def _on_global_service_change(event: str, entry):
            contract = entry.contract
            # Find all active components that require this contract
            for ci in self.lifecycle.active_instances():
                if ci.instance is None or not hasattr(ci.instance, "rt"):
                    continue
                for req in ci.meta.requirements:
                    if req.contract != contract:
                        continue
                    # Re-inject the service into this consumer's Runtime
                    rt = ci.instance.rt
                    if req.key:
                        rt.inject(req.attr_name, self.registry.require_map(contract, req.key))
                    elif req.aggregate:
                        rt.inject(req.attr_name, self.registry.require_all(contract))
                    else:
                        svc = self.registry.require_optional(contract)
                        if svc is not None:
                            # Structural scoping
                            if contract == "ILogger" and hasattr(svc, "get_logger"):
                                svc = svc.get_logger(ci.name, ci.meta.factory_name)
                            if contract == "ICredentials" and hasattr(svc, "get"):
                                svc = _ScopedCredentials(svc, ci.meta.qualified_name)
                            if contract == "IStorage" and hasattr(svc, "put"):
                                svc = _ScopedStorage(svc, ci.meta.qualified_name)
                            rt.inject(req.attr_name, svc)

        self.registry.on_change(_on_global_service_change)

    def _register_builtin_traits(self) -> None:
        from signalpy.kernel.traits import (
            IDENTIFIABLE, LIFECYCLE, DEPENDABLE,
            REGISTRABLE, INSPECTABLE, FACTORYABLE,
        )
        for name in [IDENTIFIABLE, LIFECYCLE, DEPENDABLE,
                     REGISTRABLE, INSPECTABLE, FACTORYABLE]:
            self.traits.define(name, Level.KERNEL, description=f"L0: {name}")

    # ── Discovery ───────────────────────────────────────────────

    def discover(self, classes: list[type]) -> None:
        for cls in classes:
            if not has_meta(cls):
                log.warning("Skipping %s — not a @component", cls.__name__)
                continue
            meta = self.lifecycle.register_factory(cls)
            log.info("Discovered: %s (provides=%s, requires=%s)",
                     meta.factory_name, meta.provides, list(meta.requires.values()))

    def instantiate(self, factory_name: str, instance_name: str | None = None,
                    properties: dict[str, Any] | None = None) -> ComponentInstance:
        return self.lifecycle.instantiate(factory_name, instance_name, properties)

    # ── Runtime builder (REACTIVE) ──────────────────────────────

    def _build_runtime(self, ci: ComponentInstance) -> Runtime:
        """Build a reactive Runtime for a component instance.

        Each injected service is a Signal. Reading self.rt.config
        tracks the caller. When the service changes, consumers
        are notified automatically.
        """
        policy = self._policies.get(ci.meta.factory_name, {})

        rt = Runtime(
            component_name=ci.name,
            factory_name=ci.meta.factory_name,
            properties=ci.properties,
            _bus=self.bus,
            _spawn=self._spawn_child,
            _invoke_allow=policy.get("invoke_allow", ["*"]),
            _invoke_deny=policy.get("invoke_deny", []),
            _publish_allow=policy.get("publish_allow", ["*"]),
            _audit=policy.get("audit", False),
        )

        req_by_attr = {r.attr_name: r for r in ci.meta.requirements}

        for attr_name, contract in ci.meta.requires.items():
            req = req_by_attr.get(attr_name)

            if req and req.key:
                service = self.registry.require_map(contract, req.key)
                rt.inject(attr_name, service)
                continue

            if req and req.aggregate:
                services = self.registry.require_all(contract)
                rt.inject(attr_name, services)
                continue

            # Standard single-service injection (highest-ranked)
            try:
                service = self.registry.require_for(contract, ci.name)
            except KeyError:
                service = None

            if service is None:
                if req and req.optional:
                    rt.inject(attr_name, None)
                    continue
                log.warning("%s requires %s (%s) but no provider found",
                            ci.name, contract, attr_name)
                continue

            # Structural scoping
            if contract == "ILogger" and hasattr(service, "get_logger"):
                service = service.get_logger(ci.name, ci.meta.factory_name)
            if contract == "ICredentials" and hasattr(service, "get"):
                service = _ScopedCredentials(service, ci.meta.qualified_name)
            if contract == "IStorage" and hasattr(service, "put"):
                service = _ScopedStorage(service, ci.meta.qualified_name)

            rt.inject(attr_name, service)

            # Ref counting
            self.registry.acquire(contract, ci.name)

        # Direct self-injection for annotated fields
        rt._direct_inject_fields = {}
        for req in ci.meta.requirements:
            if req.contract_type is not None or req.attr_name in getattr(ci.factory_class, '__annotations__', {}):
                service = rt._signals.get(req.attr_name)
                if service is not None:
                    rt._direct_inject_fields[req.attr_name] = service.peek()

        return rt

    # ── Bus handler factory ─────────────────────────────────────

    def _make_bus_handler(self, runnable_def, instance, ci):
        sig = inspect.signature(runnable_def.fn)
        wants_rt = len(sig.parameters) - 1 >= 2
        needs_auth = bool(runnable_def.requires_action or runnable_def.requires_role)

        async def handler(params: dict) -> Any:
            if needs_auth:
                auth_svc = self.registry.require_optional("IAuth")
                if auth_svc is not None:
                    token = params.pop("__auth_token__", None) if isinstance(params, dict) else None
                    if token is None:
                        raise PermissionError(
                            f"Runnable {ci.name}.{runnable_def.name} requires authentication")
                    identity = auth_svc.authenticate(token)
                    if runnable_def.requires_action:
                        if not auth_svc.authorize(identity, runnable_def.requires_action):
                            raise PermissionError(
                                f"Not authorized for action {runnable_def.requires_action!r}")
                    if runnable_def.requires_role:
                        if runnable_def.requires_role not in identity.get("roles", []):
                            raise PermissionError(
                                f"Role {runnable_def.requires_role!r} required")

            validated = Params(params)
            if runnable_def.params_model is not None:
                try:
                    model = runnable_def.params_model
                    if hasattr(model, "model_validate"):
                        obj = model.model_validate(params)
                        validated = Params(obj.model_dump())
                except Exception:
                    pass

            if wants_rt:
                rt = self._build_runtime(ci)
                if runnable_def.is_async:
                    return await runnable_def.fn(instance, rt, validated)
                else:
                    return runnable_def.fn(instance, rt, validated)
            else:
                if runnable_def.is_async:
                    return await runnable_def.fn(instance, validated)
                else:
                    return runnable_def.fn(instance, validated)

        return handler

    def _register_component_bus(self, ci: ComponentInstance) -> None:
        """Register bus handlers, subscriptions, kinds, skills, bind/unbind."""
        if ci.instance is None:
            return

        # Properties from @prop
        svc_properties: dict[str, Any] = {}
        for pd in ci.meta.property_defs:
            svc_properties[pd.prop_name] = pd.default
            if ci.instance is not None:
                setattr(ci.instance, pd.attr_name, pd.default)
        svc_properties.update(ci.properties)

        for contract in ci.meta.provides:
            self.registry.provide(contract, ci.instance, ci.name, svc_properties)

        # Register bus handlers for runnables
        for rd in ci.meta.runnables:
            handler_name = f"{ci.name}.{rd.name}"
            self.bus.register_handler(handler_name, self._make_bus_handler(rd, ci.instance, ci))
            target_value = ci.properties.get("target")
            if target_value and ci.name != ci.meta.factory_name:
                factory_handler = f"{ci.meta.factory_name}.{rd.name}"
                self.bus.register_target_route(factory_handler, target_value, handler_name)

        # Register subscriptions
        for sd in ci.meta.subscriptions:
            instance = ci.instance

            def _make_sub_handler(sub_def=sd, inst=instance):
                if sub_def.is_async:
                    async def sub_handler(event_type, data):
                        await sub_def.fn(inst, event_type, data)
                else:
                    def sub_handler(event_type, data):
                        sub_def.fn(inst, event_type, data)
                return sub_handler

            self.bus.subscribe(sd.event_type, _make_sub_handler())

        # Register kinds and skills
        for kd in ci.meta.kinds:
            self.kinds[kd.name] = kd
        for sk in ci.meta.skills:
            self.skills[sk.name] = sk

        # ── Set up reactive Effects and Computeds ──────────────
        self._setup_reactive(ci)

    def _setup_reactive(self, ci: ComponentInstance) -> None:
        """Create Effect/Computed wrappers for @effect and @computed methods."""
        if ci.instance is None:
            return

        instance = ci.instance

        # Computed properties: create ReactiveComputed, set as property on instance
        for cd in ci.meta.computed_defs:
            fn = cd.fn

            def _make_computed(f=fn):
                return ReactiveComputed(lambda f=f: f(instance))

            rc = _make_computed()
            ci._disposables.append(rc)

            # Make accessible as self.property_name (the method name)
            prop_name = fn.__name__
            # Store the ReactiveComputed so the component can read it
            setattr(instance, prop_name, property(lambda self, _rc=rc: _rc.get()))
            # Since property() on instance doesn't work, use a descriptor approach:
            # Actually, just store the computed and let users call self.prop_name
            # which will be a ReactiveComputed.get()
            setattr(instance, f"_computed_{prop_name}", rc)
            # Override the method to return computed value
            setattr(instance, prop_name, rc.get)

        # Effects: create ReactiveEffect wrappers
        for ed in ci.meta.effect_defs:
            fn = ed.fn

            if ed.is_async:
                # Async effect: wrapper must be async for iscoroutinefunction detection
                async def _async_wrapper(f=fn, inst=instance):
                    await f(inst)
                re = ReactiveEffect(
                    _async_wrapper,
                    lazy=False,
                    cancel_on_supersede=ed.cancel_on_supersede,
                )
            else:
                def _sync_wrapper(f=fn, inst=instance):
                    f(inst)
                re = ReactiveEffect(_sync_wrapper, lazy=False)

            ci._disposables.append(re)

    # ── Bind/Unbind wiring ──────────────────────────────────────

    def _update_injection(self, ci: ComponentInstance, attr_name: str, contract: str) -> None:
        """Re-inject a requirement after a service change. REACTIVE — Signal.set notifies."""
        if ci.instance is None or not hasattr(ci.instance, "rt"):
            return
        req_by_attr = {r.attr_name: r for r in ci.meta.requirements}
        req = req_by_attr.get(attr_name)
        if req is None:
            return
        rt = ci.instance.rt
        if req.key:
            rt.inject(attr_name, self.registry.require_map(contract, req.key))
        elif req.aggregate:
            rt.inject(attr_name, self.registry.require_all(contract))

    # ── Spawn ───────────────────────────────────────────────────

    async def _spawn_child(self, factory_name, instance_name=None,
                           properties=None, parent_name=None):
        ci = self.lifecycle.instantiate(factory_name, instance_name, properties)
        ci.parent = parent_name
        ci.state = State.RESOLVED
        if parent_name:
            parent_ci = self.lifecycle.get_instance(parent_name)
            if parent_ci:
                parent_ci.children.append(ci.name)
        await self.lifecycle.activate(ci.name, self._build_runtime)
        self._register_component_bus(ci)
        log.info("Spawned child: %s (parent=%s)", ci.name, parent_name)
        return ci.instance

    # ── Boot ────────────────────────────────────────────────────

    async def boot(self) -> None:
        self._state = KernelState.BOOTING

        # Set up reactive propagation — service changes update consumer Runtimes
        self._setup_reactive_propagation()

        for factory_name in list(self.lifecycle.factories):
            if not any(ci.meta.factory_name == factory_name
                       for ci in self.lifecycle.all_instances()):
                self.instantiate(factory_name)

        order = self.lifecycle.resolve_all()
        log.info("Activation order: %s", order)

        transport_names: list[str] = []

        for name in order:
            ci = self.lifecycle.get_instance(name)
            if ci is None:
                continue
            is_transport = "IGateway" in ci.meta.requires.values()
            try:
                await self.lifecycle.activate(name, self._build_runtime)
            except Exception as exc:
                log.error("Failed to activate %s: %s — skipping", name, exc)
                continue
            self._register_component_bus(ci)
            if is_transport:
                transport_names.append(name)

        # Phase 2: gateway rebuild + transport re-activation
        gw = self.registry.require_optional("IGateway")
        if gw and hasattr(gw, "set_lifecycle"):
            gw.set_lifecycle(self.lifecycle)
            log.info("Gateway rebuilt with %d bus handlers", len(self.bus.handlers))
            for tname in transport_names:
                ci = self.lifecycle.get_instance(tname)
                if ci and ci.instance and ci.meta.activate_fn:
                    rt = self._build_runtime(ci)
                    try:
                        result = ci.meta.activate_fn(ci.instance, rt)
                        if ci.meta.activate_is_async:
                            await result
                        log.info("Transport re-activated: %s", tname)
                    except Exception as exc:
                        log.error("Transport re-activation failed: %s: %s", tname, exc)

        self._state = KernelState.HEALTHY
        log.info("Kernel booted: %d components, %d services, %d bus handlers",
                 len(self.lifecycle.active_instances()),
                 len(self.registry), len(self.bus.handlers))

    # ── Drain ───────────────────────────────────────────────────

    async def drain(self, timeout_s: float = 30.0) -> None:
        self._state = KernelState.DRAINING
        self._drain_event = asyncio.Event()
        if self._inflight == 0:
            self._drain_event.set()
        log.info("Draining: %d in-flight invocations", self._inflight)
        try:
            await asyncio.wait_for(self._drain_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            log.warning("Drain timed out with %d in-flight", self._inflight)

    # ── Hot add/remove ──────────────────────────────────────────

    async def hot_add(self, cls: type, instance_name: str | None = None,
                      properties: dict[str, Any] | None = None) -> ComponentInstance:
        if not has_meta(cls):
            raise TypeError(f"{cls.__name__} is not a @component")
        meta = self.lifecycle.register_factory(cls)
        ci = self.lifecycle.instantiate(meta.factory_name, instance_name, properties)
        ci.state = State.RESOLVED
        await self.lifecycle.activate(ci.name, self._build_runtime)
        self._register_component_bus(ci)
        gw = self.registry.require_optional("IGateway")
        if gw and hasattr(gw, "_rebuild"):
            gw._rebuild()
        log.info("Hot-added: %s", ci.name)
        return ci

    async def hot_remove(self, name: str) -> None:
        ci = self.lifecycle.get_instance(name)
        if ci is None:
            raise KeyError(f"No instance named {name!r}")
        for rd in ci.meta.runnables:
            self.bus.unregister_handler(f"{ci.name}.{rd.name}")
        for entry in self.registry.query():
            if entry.provider_name == ci.name:
                self.registry.unprovide(entry)
        # Release ref counts
        for attr_name, contract in ci.meta.requires.items():
            self.registry.release(contract, ci.name)
        await self.lifecycle.deactivate(name, self._build_runtime)
        gw = self.registry.require_optional("IGateway")
        if gw and hasattr(gw, "_rebuild"):
            gw._rebuild()
        log.info("Hot-removed: %s", name)

    # ── Hot update (blue-green) ───────────────────────────────────

    async def hot_update(self, new_cls: type) -> list[ComponentInstance]:
        """Replace a running component with a new version, preserving state.

        1. Snapshot state from each running instance (via @lifecycle.snapshot
           or __dict__ fallback)
        2. Tear down old instances (unregister bus, unprovide, deactivate)
        3. Replace the factory class
        4. Create new instances with same names and properties
        5. Activate and restore state (via @lifecycle.restore or __dict__)
        6. Re-register bus handlers, gateway rebuild

        Returns the new ComponentInstance objects.
        """
        if not has_meta(new_cls):
            raise TypeError(f"{new_cls.__name__} is not a @component")

        new_meta = get_meta(new_cls)
        factory_name = new_meta.factory_name

        if factory_name not in self.lifecycle.factories:
            raise KeyError(f"No factory {factory_name!r} to update — use hot_add for new components")

        # Find all running instances of this factory
        old_instances = [
            ci for ci in self.lifecycle.all_instances()
            if ci.meta.factory_name == factory_name and ci.state == State.ACTIVE
        ]
        if not old_instances:
            raise KeyError(f"No active instances of {factory_name!r}")

        # ── Phase 1: Snapshot ──────────────────────────────────
        snapshots: dict[str, dict] = {}
        for ci in old_instances:
            if ci.instance is None:
                continue
            if ci.meta.snapshot_fn:
                result = ci.meta.snapshot_fn(ci.instance)
                if ci.meta.snapshot_is_async:
                    result = await result
                snapshots[ci.name] = result
            else:
                # Fallback: capture instance __dict__ minus kernel internals
                snapshots[ci.name] = {
                    k: v for k, v in ci.instance.__dict__.items()
                    if k != "rt" and not k.startswith("_computed_")
                }
            log.info("Snapshot: %s (%d keys)", ci.name, len(snapshots[ci.name]))

        # Save instance metadata before teardown
        instance_specs = [
            (ci.name, dict(ci.properties))
            for ci in old_instances
        ]

        # ── Phase 2: Tear down old instances ───────────────────
        for ci in old_instances:
            for rd in ci.meta.runnables:
                self.bus.unregister_handler(f"{ci.name}.{rd.name}")
            for entry in self.registry.query():
                if entry.provider_name == ci.name:
                    self.registry.unprovide(entry)
            for attr_name, contract in ci.meta.requires.items():
                self.registry.release(contract, ci.name)
            await self.lifecycle.deactivate(ci.name, self._build_runtime)
            self.lifecycle.remove_instance(ci.name)

        # ── Phase 3: Replace factory ───────────────────────────
        self.lifecycle.replace_factory(new_cls)
        log.info("Factory replaced: %s → %s", factory_name, new_cls.__name__)

        # ── Phase 4: Create + activate + restore ───────────────
        new_instances = []
        for inst_name, props in instance_specs:
            ci = self.lifecycle.instantiate(factory_name, inst_name, props)
            ci.state = State.RESOLVED
            await self.lifecycle.activate(ci.name, self._build_runtime)
            self._register_component_bus(ci)

            # Restore state
            if ci.instance is not None and inst_name in snapshots:
                state = snapshots[inst_name]
                if ci.meta.restore_fn:
                    result = ci.meta.restore_fn(ci.instance, state)
                    if ci.meta.restore_is_async:
                        await result
                else:
                    # Fallback: merge into __dict__
                    for k, v in state.items():
                        if k != "rt" and not k.startswith("_computed_"):
                            setattr(ci.instance, k, v)
                log.info("Restored: %s (%d keys)", ci.name, len(state))

            new_instances.append(ci)

        # Gateway rebuild
        gw = self.registry.require_optional("IGateway")
        if gw and hasattr(gw, "_rebuild"):
            gw._rebuild()

        log.info("Hot-updated: %s (%d instances)", factory_name, len(new_instances))
        return new_instances

    # ── Retry errored ───────────────────────────────────────────

    async def retry_erroneous(self, name: str) -> ComponentInstance:
        await self.lifecycle.retry_erroneous(name, self._build_runtime)
        ci = self.lifecycle.get_instance(name)
        if ci and ci.state == State.ACTIVE:
            self._register_component_bus(ci)
        return ci

    # ── Shutdown ────────────────────────────────────────────────

    async def shutdown(self) -> None:
        if self._state not in (KernelState.DRAINING, KernelState.STOPPED):
            self._state = KernelState.DRAINING
        await self.lifecycle.shutdown(self._build_runtime)
        self._state = KernelState.STOPPED
        log.info("Kernel shut down")

    # ── Status ──────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state.name,
            "components": [
                {
                    "name": ci.name,
                    "factory": ci.meta.factory_name,
                    "state": ci.state.name,
                    "provides": ci.meta.provides,
                    "runnables": [r.name for r in ci.meta.runnables],
                    "subscriptions": [s.event_type for s in ci.meta.subscriptions],
                    "kinds": [k.name for k in ci.meta.kinds],
                    "skills": [s.name for s in ci.meta.skills],
                    "traits": self.traits.compute(ci.meta),
                    "reactive": {
                        "computed": [cd.fn.__name__ for cd in ci.meta.computed_defs],
                        "effects": [ed.fn.__name__ for ed in ci.meta.effect_defs],
                    },
                }
                for ci in self.lifecycle.all_instances()
            ],
            "services": len(self.registry),
            "bus_handlers": self.bus.handlers,
            "kinds": list(self.kinds.keys()),
            "skills": list(self.skills.keys()),
        }


# ── Structural scoping wrappers ────────────────────────────────

class _ScopedCredentials:
    def __init__(self, inner: Any, app_name: str) -> None:
        self._inner = inner
        self._app = app_name

    def get(self, key: str, *, target: str | None = None) -> str:
        return self._inner.get(key, target=target, app=self._app)

    def for_target(self, target: str) -> dict[str, str]:
        return self._inner.for_target(target, app=self._app)

    def list_targets(self) -> list[str]:
        return self._inner.list_targets(app=self._app)


class _ScopedStorage:
    def __init__(self, inner: Any, app_name: str) -> None:
        self._inner = inner
        self._prefix = app_name

    async def put(self, key: str, data: bytes) -> None:
        await self._inner.put(key, data, prefix=self._prefix)

    async def get(self, key: str) -> bytes | None:
        return await self._inner.get(key, prefix=self._prefix)

    async def list(self, prefix: str = "") -> list[str]:
        return await self._inner.list(prefix=f"{self._prefix}/{prefix}" if prefix else self._prefix)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key, prefix=self._prefix)


# ── Public API ──────────────────────────────────────────────────

__all__ = [
    # Kernel
    "Kernel", "KernelState",
    # Reactive primitives
    "Signal", "batch", "is_stale",
    # Core decorators (12 total)
    "component", "provides", "requires",          # core
    "computed", "effect",                          # reactive
    "lifecycle",                                   # lifecycle (.activate, .deactivate, .health)
    "runnable", "api",                             # surface
    "prop", "kind", "skill",                       # metadata
    "subscribe",                                   # events
    # Infrastructure
    "Bus", "Level", "Runtime", "ServiceRegistry", "TraitRegistry",
]
