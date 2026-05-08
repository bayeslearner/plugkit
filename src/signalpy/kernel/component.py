"""Component model — decorators that turn POPOs into kernel-managed components.

A plain Python class becomes a component when decorated with @component.
Additional decorators declare traits: @provides, @requires, @runnable, etc.

The decorators don't execute anything — they attach metadata that the kernel
reads at discovery time.  The class itself stays a normal class.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, get_type_hints


# ── Contract name resolution ──────────────────────────────────────

# Marker for types that are kernel contracts (set by _register_contract)
_KERNEL_CONTRACT = "__kernel_contract__"

# Registry of known contract types (type → name)
_CONTRACT_TYPES: dict[type, str] = {}


def _contract_name(spec) -> str:
    """Resolve a contract specifier to a string name.

    Accepts:
        "IConfig"       → "IConfig"    (string passthrough)
        IConfig         → "IConfig"    (type → class name)
    """
    if isinstance(spec, str):
        return spec
    if isinstance(spec, type):
        name = spec.__name__
        # Register this type so annotation scanning can find it
        if spec not in _CONTRACT_TYPES:
            _CONTRACT_TYPES[spec] = name
        return name
    raise TypeError(f"Contract must be str or type, got {type(spec).__name__}")


def _is_contract_type(tp) -> bool:
    """Check if a type is a known contract (Protocol with @runtime_checkable, or registered)."""
    if not isinstance(tp, type):
        return False
    if tp in _CONTRACT_TYPES:
        return True
    # Check if it's a runtime_checkable Protocol (our contracts.py pattern)
    if (isinstance(tp, type) and issubclass(tp, Protocol)
            and getattr(tp, "_is_runtime_protocol", False)):
        return True
    return False


# ── Metadata attached to decorated classes ──────────────────────────

KERNEL_META = "__kernel_meta__"


@dataclass
class RequirementDef:
    """A declared service dependency — rich metadata for injection."""
    attr_name: str           # field name on the component (e.g. "config")
    contract: str            # contract name (e.g. "IConfig")
    contract_type: type | None = None  # original type (for IDE support)
    aggregate: bool = False  # True �� inject list of all matching services
    optional: bool = False   # True → component valid without this dep
    key: str = ""            # for map injection: property name to key by


@dataclass
class PropertyDef:
    """A declared mutable component property."""
    attr_name: str           # field name on the component
    prop_name: str           # external property name (for registry/config)
    default: Any = None


@dataclass
class RunnableDef:
    """A callable with a typed schema — the atomic unit of capability.

    Schema-only: declares name, params, description for tool discovery.
    Transport adapters read schemas via kernel.runnables() and call
    schema.handler directly. No bus handler registration.

    transports controls per-operation visibility:
        None          → all transports the component declared + native
        ["native"]    → direct calls only (replaces internal=True)
        ["mcp"]       → MCP only
        ["rest","mcp"] → REST + MCP
    """
    name: str
    params_model: type
    return_type: type | None
    fn: Callable
    description: str = ""
    timeout_s: float | None = None
    destructive: bool = False
    internal: bool = False   # DEPRECATED: use transports=["native"] instead
    transports: list[str] | None = None  # None = all; ["native"] = direct only
    requires_action: str = ""  # auth action required (e.g. "docs.write")
    requires_role: str = ""    # auth role required (e.g. "admin")
    is_async: bool = True    # detected at decoration time (default True for backward compat)

    def visible_on(self, transport: str) -> bool:
        """Check if this runnable should be exposed on a given transport."""
        # Legacy: internal=True means native-only
        if self.internal and self.transports is None:
            return transport == "native"
        if self.transports is None:
            return True  # all transports
        return transport in self.transports


@dataclass
class SubscribeDef:
    """A declared event subscription — the reactive counterpart to RunnableDef."""
    event_type: str
    fn: Callable
    description: str = ""
    is_async: bool = False   # detected at decoration time


@dataclass
class ComputedDef:
    """A reactive computed property — recomputes when tracked deps change."""
    fn: Callable
    is_async: bool = False

@dataclass
class EffectDef:
    """A reactive side effect — re-runs when tracked deps change."""
    fn: Callable
    is_async: bool = False
    cancel_on_supersede: bool = False

@dataclass
class SupervisionDef:
    """Supervision strategy for child components — Erlang/OTP style."""
    fn: Callable
    is_async: bool = False
    strategy: str = "one_for_one"   # one_for_one | one_for_all | rest_for_one
    max_restarts: int = 3
    within_seconds: float = 60.0
    backoff: str = "exponential"    # constant | linear | exponential
    base_delay: float = 1.0


@dataclass
class KindDef:
    """A declared data schema — a named Pydantic model contributed by a component."""
    name: str
    model: type           # Pydantic BaseModel subclass
    description: str = ""


@dataclass
class SkillDef:
    """A declared knowledge bundle — AI-native trait for agent context."""
    name: str
    content: str          # markdown knowledge
    triggers: list[str] = field(default_factory=list)  # when to activate this skill
    description: str = ""


@dataclass
class ComponentMeta:
    """Everything the kernel needs to know about a component class."""
    factory_name: str
    namespace: str = ""                                      # L0: Identifiable
    version: str = "0.0.0"
    provides: list[str] = field(default_factory=list)
    requires: dict[str, str] = field(default_factory=dict)  # attr_name → contract (simple form)
    requirements: list[RequirementDef] = field(default_factory=list)  # rich form
    property_defs: list[PropertyDef] = field(default_factory=list)  # mutable properties
    runnables: list[RunnableDef] = field(default_factory=list)
    subscriptions: list[SubscribeDef] = field(default_factory=list)
    computed_defs: list[ComputedDef] = field(default_factory=list)   # reactive computed
    effect_defs: list[EffectDef] = field(default_factory=list)       # reactive effects
    kinds: list[KindDef] = field(default_factory=list)
    skills: list[SkillDef] = field(default_factory=list)
    # Transport config — per-transport settings on @component (spec 011)
    transport_config: dict[str, dict[str, Any]] = field(default_factory=dict)
    activate_fn: Callable | None = None
    activate_is_async: bool = False
    deactivate_fn: Callable | None = None
    deactivate_is_async: bool = False
    health_fn: Callable | None = None
    snapshot_fn: Callable | None = None
    snapshot_is_async: bool = False
    restore_fn: Callable | None = None
    restore_is_async: bool = False
    supervision_def: SupervisionDef | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    # Static call graph edges (potential invoke/publish targets found in source)
    invoke_targets: list[str] = field(default_factory=list)   # e.g. ["email.send", "search.query"]
    publish_events: list[str] = field(default_factory=list)   # e.g. ["orders.placed", "data.updated"]

    @property
    def qualified_name(self) -> str:
        """Namespace-qualified factory name: 'ns.factory' or just 'factory'."""
        if self.namespace:
            return f"{self.namespace}.{self.factory_name}"
        return self.factory_name

    def api_runnables(self, transport: str) -> list[RunnableDef]:
        """Return runnables that should be exposed for a given transport.

        Uses per-runnable transports visibility (spec 011).
        """
        if transport not in self.transport_config:
            return []
        return [r for r in self.runnables if r.visible_on(transport)]

    def has_api(self, transport: str | None = None) -> bool:
        """Check if this component declares any transport config."""
        if transport:
            return transport in self.transport_config
        return bool(self.transport_config)


def _get_meta(cls: type) -> ComponentMeta:
    """Get or create the ComponentMeta for a class."""
    meta = getattr(cls, KERNEL_META, None)
    if meta is None:
        meta = ComponentMeta(factory_name=cls.__name__)
        setattr(cls, KERNEL_META, meta)
    return meta


def has_meta(cls: type) -> bool:
    """Check if a class has been decorated as a component."""
    return hasattr(cls, KERNEL_META)


def get_meta(cls: type) -> ComponentMeta:
    """Read the ComponentMeta from a decorated class.  Raises if not a component."""
    meta = getattr(cls, KERNEL_META, None)
    if meta is None:
        raise TypeError(f"{cls.__name__} is not a @component")
    return meta


# ── Decorators ──────────────────────────────────────────────────────


def component(
    name: str,
    *,
    namespace: str = "",
    version: str = "0.0.0",
    rest: dict[str, Any] | None = None,
    mcp: dict[str, Any] | None = None,
    cli: dict[str, Any] | None = None,
    **extra_transports: dict[str, Any],
):
    """Mark a class as a component factory.

    This is the equivalent of iPOPO's @ComponentFactory — it says
    "this POPO is now factoryable by the kernel."

    Namespace scopes service registry, storage prefix, and credential
    paths.  Defaults to "" (root namespace).

    Transport config (replaces @api):
        @component("my-app", version="1.0",
                   rest={"prefix": "/my-app", "version": "v1"},
                   mcp={"name": "my-tools"})

    Examples:
        @component("greeter")                          → greeter.greet
        @component("greeter", namespace="acme")        → acme.greeter.greet
        @component("greeter", namespace="acme.tools")  → acme.tools.greeter.greet
    """
    def decorator(cls: type) -> type:
        meta = _get_meta(cls)
        meta.factory_name = name
        meta.namespace = namespace
        meta.version = version
        # Transport config
        if rest is not None:
            meta.transport_config["rest"] = rest
        if mcp is not None:
            meta.transport_config["mcp"] = mcp
        if cli is not None:
            meta.transport_config["cli"] = cli
        for transport_name, config in extra_transports.items():
            if isinstance(config, dict):
                meta.transport_config[transport_name] = config
        return cls
    return decorator


def provides(*contracts):
    """Declare services this component provides into the registry.

    Accepts types or strings:
        @provides(IDictionary)          # type → "IDictionary"
        @provides("IDictionary")        # string passthrough
        @provides(IDictionary, ICache)  # multiple
    """
    def decorator(cls: type) -> type:
        meta = _get_meta(cls)
        for c in contracts:
            meta.provides.append(_contract_name(c))
        return cls
    return decorator


def requires(*, optional: bool = False, key: str = "", **deps):
    """Declare services this component requires from the registry.

    The unified injection decorator. Type hints tell the kernel what you need:

        @requires(config=IConfig)                      # single (highest-ranked)
        @requires(dicts=list[IDictionary])              # aggregate: all as list
        @requires(dicts=IDictionary, key="language")    # map: dict keyed by property
        @requires(cache=IConfig, optional=True)         # optional: None if missing

    Strings still work:
        @requires(config="IConfig")

    The kernel injects as self.rt.<attr_name> (reactive — tracks dependencies).
    """
    def decorator(cls: type) -> type:
        meta = _get_meta(cls)
        for attr_name, spec in deps.items():
            # Detect list[X] → aggregate
            aggregate = False
            contract_type = None
            origin = getattr(spec, "__origin__", None)
            if origin is list:
                # list[IDictionary] → aggregate injection
                args = getattr(spec, "__args__", ())
                if args:
                    spec = args[0]  # extract the inner type
                    aggregate = True

            contract_str = _contract_name(spec)
            contract_type = spec if isinstance(spec, type) else None

            # key= present → map injection (also aggregate)
            if key:
                aggregate = True

            meta.requires[attr_name] = contract_str
            meta.requirements.append(RequirementDef(
                attr_name=attr_name, contract=contract_str,
                contract_type=contract_type,
                aggregate=aggregate, optional=optional, key=key,
            ))
        return cls
    return decorator


def runnable(
    name: str,
    *,
    params: type,
    returns: type | None = None,
    description: str = "",
    timeout_s: float | None = None,
    destructive: bool = False,
    internal: bool = False,
    transports: list[str] | None = None,
    requires_action: str = "",
    requires_role: str = "",
):
    """Declare a method as a runnable — a schema-only capability declaration.

    Runnables are the atomic unit of capability. The schema (name, params,
    description) is used by transport adapters and tool-gateway for discovery.
    Consumers call schema.handler directly — no bus dispatch.

    Transport visibility (per-operation):
        transports=None           → all transports + native (default)
        transports=["native"]     → direct calls only (replaces internal=True)
        transports=["mcp"]        → MCP tools only
        transports=["rest","mcp"] → REST + MCP, not CLI

    Auth requirements (enforced per consumer):
        requires_action="docs.write"  → caller must be authorized for this action
        requires_role="admin"         → caller must have this role
    """
    def decorator(fn: Callable) -> Callable:
        if not hasattr(fn, "__runnable_defs__"):
            fn.__runnable_defs__ = []
        fn.__runnable_defs__.append(RunnableDef(
            name=name,
            params_model=params,
            return_type=returns,
            fn=fn,
            description=description,
            timeout_s=timeout_s,
            destructive=destructive,
            internal=internal,
            transports=transports,
            requires_action=requires_action,
            requires_role=requires_role,
            is_async=inspect.iscoroutinefunction(fn),
        ))
        return fn
    return decorator


def prop(attr_name: str, prop_name: str, default: Any = None):
    """Declare a mutable component property.

    iPOPO equivalent: @Property

    Properties are exposed in the service registry and can be
    updated at runtime. service.ranking is a special property
    that controls service selection priority (lower = higher priority).

        @prop("_language", "language", "EN")
        @prop("_ranking", "service.ranking", 0)
        class EnglishDict: ...
    """
    def decorator(cls: type) -> type:
        meta = _get_meta(cls)
        meta.property_defs.append(PropertyDef(
            attr_name=attr_name, prop_name=prop_name, default=default,
        ))
        return cls
    return decorator


def subscribe(event_type: str, *, description: str = ""):
    """Declare a method as an event handler — the reactive counterpart to @runnable.

    The kernel registers the handler as a bus subscriber during activation.
    The method is called with (self, event_type: str, data: Any).

        @subscribe("case.created", description="Handle new cases")
        async def on_case_created(self, event_type, data):
            ...
    """
    def decorator(fn: Callable) -> Callable:
        if not hasattr(fn, "__subscribe_defs__"):
            fn.__subscribe_defs__ = []
        fn.__subscribe_defs__.append(SubscribeDef(
            event_type=event_type,
            fn=fn,
            description=description,
            is_async=inspect.iscoroutinefunction(fn),
        ))
        return fn
    return decorator


def computed(fn: Callable) -> Callable:
    """Declare a method as a reactive computed property.

    The method body reads from self.rt.* — those reads are tracked.
    The result is cached and recomputed only when dependencies change.

        @computed
        def base_url(self):
            return self.rt.config.get("my-app.url", "http://localhost")

    Access: self.base_url (always current, cached until deps change)
    """
    fn.__computed__ = ComputedDef(fn=fn, is_async=inspect.iscoroutinefunction(fn))
    return fn


def effect(fn: Callable | None = None, *, cancel_on_supersede: bool = False):
    """Declare a method as a reactive side effect.

    The method body reads from self.rt.* — those reads are tracked
    automatically. When any tracked dependency changes, the method re-runs.

        @effect
        async def on_config_change(self):
            url = self.rt.config.get("my-app.url")
            await self._reconnect(url)

    No need to declare dependencies — the kernel figures them out
    from what you read (like Vue's watchEffect).

    For async effects, ``@effect(cancel_on_supersede=True)`` asks the
    engine to cancel the in-flight task when a dependency changes
    mid-await. The body sees ``CancelledError`` at the next await point,
    and the engine then re-runs the effect with the latest values.
    Without this flag (the default), the in-flight body runs to
    completion and the engine re-fires once it returns.

    Inside an async body, ``is_stale()`` reports
    whether a newer run has been scheduled — useful for cooperative
    short-circuits between awaits.
    """
    def _decorate(f: Callable) -> Callable:
        f.__effect__ = EffectDef(
            fn=f,
            is_async=inspect.iscoroutinefunction(f),
            cancel_on_supersede=cancel_on_supersede,
        )
        return f
    if fn is None:
        # Called as @effect(...)
        return _decorate
    # Called as @effect with a function
    return _decorate(fn)


def kind(name: str, *, model: type, description: str = ""):
    """Register a named data schema contributed by this component.

    Kinds are Pydantic models that other components can discover and
    validate against.  The kernel maintains a kind registry queryable
    at runtime.

        @kind("alert", model=AlertModel, description="Security alert")
        class SecurityApp: ...
    """
    def decorator(cls: type) -> type:
        meta = _get_meta(cls)
        meta.kinds.append(KindDef(name=name, model=model, description=description))
        return cls
    return decorator


def skill(name: str, *, content: str, triggers: list[str] | None = None, description: str = ""):
    """Declare an AI knowledge bundle contributed by this component.

    Skills are markdown knowledge + trigger conditions.  Agent components
    query the skill registry to build context-aware prompts.

        @skill("spl-writer", content="...", triggers=["splunk", "spl", "query"])
        class SplunkApp: ...
    """
    def decorator(cls: type) -> type:
        meta = _get_meta(cls)
        meta.skills.append(SkillDef(
            name=name, content=content,
            triggers=triggers or [], description=description,
        ))
        return cls
    return decorator


class lifecycle:
    """Namespace for lifecycle callback decorators."""

    @staticmethod
    def activate(fn: Callable) -> Callable:
        """Mark a method as the activation callback."""
        fn.__lifecycle__ = "activate"
        return fn

    @staticmethod
    def deactivate(fn: Callable) -> Callable:
        """Mark a method as the deactivation callback."""
        fn.__lifecycle__ = "deactivate"
        return fn

    @staticmethod
    def health(fn: Callable) -> Callable:
        """Mark a method as the health check."""
        fn.__lifecycle__ = "health"
        return fn

    @staticmethod
    def snapshot(fn: Callable) -> Callable:
        """Mark a method that returns state to preserve across hot_update."""
        fn.__lifecycle__ = "snapshot"
        return fn

    @staticmethod
    def restore(fn: Callable) -> Callable:
        """Mark a method that receives preserved state after hot_update."""
        fn.__lifecycle__ = "restore"
        return fn

    @staticmethod
    def supervision(
        *,
        strategy: str = "one_for_one",
        max_restarts: int = 3,
        within_seconds: float = 60.0,
        backoff: str = "exponential",
        base_delay: float = 1.0,
    ):
        """Declare a supervision callback for child component failures.

        Strategies (Erlang/OTP):
            one_for_one  — restart only the failed child
            one_for_all  — restart ALL children
            rest_for_one — restart the failed child + everything started after it

        The decorated method is called with:
            (self, child_name: str, error: Exception, attempt: int, context: SupervisionContext)
        Return True to proceed with restart, False to give up.
        Raise SupervisionEscalation to fail this supervisor upward.

            @lifecycle.supervision(strategy="one_for_one", max_restarts=3, within_seconds=60)
            async def on_child_failure(self, child_name, error, attempt, context):
                return True  # proceed with restart
        """
        if strategy not in ("one_for_one", "one_for_all", "rest_for_one"):
            raise ValueError(f"Unknown supervision strategy: {strategy!r}")
        if backoff not in ("constant", "linear", "exponential"):
            raise ValueError(f"Unknown backoff strategy: {backoff!r}")

        def decorator(fn: Callable) -> Callable:
            fn.__lifecycle__ = "supervision"
            fn.__supervision_config__ = {
                "strategy": strategy,
                "max_restarts": max_restarts,
                "within_seconds": within_seconds,
                "backoff": backoff,
                "base_delay": base_delay,
            }
            return fn
        return decorator


def _finalize_meta(cls: type) -> ComponentMeta:
    """Scan a decorated class and collect all metadata into ComponentMeta.

    Called by the kernel at discovery time, after all decorators have run.
    """
    meta = get_meta(cls)

    # ── Scan annotations for implicit requirements ─────────────
    # If a class has `dictionary: IDictionary` and IDictionary is a
    # known contract type, treat it as an implicit @requires.
    try:
        # Use the class __annotations__ directly to avoid evaluating
        # string annotations from __future__.annotations
        hints = {}
        for klass in reversed(cls.__mro__):
            if klass is object:
                continue
            hints.update(getattr(klass, '__annotations__', {}))
    except Exception:
        hints = {}

    existing_reqs = {r.attr_name for r in meta.requirements}
    for attr_name, hint in hints.items():
        if attr_name in existing_reqs or attr_name in meta.requires:
            continue
        # Resolve string annotations to actual types
        resolved = hint
        if isinstance(hint, str):
            # Try to resolve from the module's namespace
            import sys
            mod = sys.modules.get(cls.__module__)
            if mod:
                resolved = getattr(mod, hint, hint)
        # Check if this is a known contract type
        if isinstance(resolved, type) and _is_contract_type(resolved):
            contract_str = _contract_name(resolved)
            meta.requires[attr_name] = contract_str
            meta.requirements.append(RequirementDef(
                attr_name=attr_name, contract=contract_str,
                contract_type=resolved,
            ))

    # Ensure every simple requires entry has a RequirementDef
    existing_reqs = {r.attr_name for r in meta.requirements}
    for attr_name, contract in meta.requires.items():
        if attr_name not in existing_reqs:
            meta.requirements.append(RequirementDef(
                attr_name=attr_name, contract=contract,
            ))

    # Collect runnables, subscriptions, binds, and lifecycle callbacks from methods
    for attr_name in dir(cls):
        obj = getattr(cls, attr_name, None)
        if obj is None:
            continue

        # Runnables
        if hasattr(obj, "__runnable_defs__"):
            for rd in obj.__runnable_defs__:
                if rd not in meta.runnables:
                    meta.runnables.append(rd)

        # Subscriptions
        if hasattr(obj, "__subscribe_defs__"):
            for sd in obj.__subscribe_defs__:
                if sd not in meta.subscriptions:
                    meta.subscriptions.append(sd)

        # Reactive computed properties
        cd = getattr(obj, "__computed__", None)
        if isinstance(cd, ComputedDef) and cd not in meta.computed_defs:
            meta.computed_defs.append(cd)

        # Reactive effects
        ed = getattr(obj, "__effect__", None)
        if isinstance(ed, EffectDef) and ed not in meta.effect_defs:
            meta.effect_defs.append(ed)

        # Lifecycle callbacks
        lc = getattr(obj, "__lifecycle__", None)
        if lc == "activate":
            meta.activate_fn = obj
            meta.activate_is_async = inspect.iscoroutinefunction(obj)
        elif lc == "deactivate":
            meta.deactivate_fn = obj
            meta.deactivate_is_async = inspect.iscoroutinefunction(obj)
        elif lc == "health":
            meta.health_fn = obj
        elif lc == "snapshot":
            meta.snapshot_fn = obj
            meta.snapshot_is_async = inspect.iscoroutinefunction(obj)
        elif lc == "restore":
            meta.restore_fn = obj
            meta.restore_is_async = inspect.iscoroutinefunction(obj)
        elif lc == "supervision":
            cfg = getattr(obj, "__supervision_config__", {})
            meta.supervision_def = SupervisionDef(
                fn=obj,
                is_async=inspect.iscoroutinefunction(obj),
                **cfg,
            )

    # ── Static call graph extraction ─────────────────────────────
    # Scan method source for self.rt.invoke("target") and self.rt.publish("event")
    # patterns to build the potential (weak) call graph.
    meta.invoke_targets = _extract_call_targets(cls, "invoke")
    meta.publish_events = _extract_call_targets(cls, "publish")

    return meta


import re

# Patterns to find string literals passed to self.rt.invoke() / self.rt.publish()
# Matches: self.rt.invoke("target.action"  or  self.rt.invoke('target.action'
# Also matches: rt.invoke("target.action"  (for activate(self, rt) style)
_INVOKE_PATTERN = re.compile(
    r'\.invoke\(\s*["\']([a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+)["\']'
)
_PUBLISH_PATTERN = re.compile(
    r'\.publish\(\s*["\']([a-zA-Z0-9_\-]+(?:\.[a-zA-Z0-9_\-*]+)*)["\']'
)


_FUNC_CALL_PATTERN = re.compile(r'\b(\w+)\s*\(')


def _extract_call_targets(cls: type, kind: str) -> list[str]:
    """Extract invoke/publish target strings from component method source code.

    Uses source inspection to find string literals passed to .invoke() or
    .publish() calls. This is best-effort static analysis — it won't catch
    dynamically constructed target names, but catches the common case of
    literal strings which covers 95%+ of real usage.

    Follows one level of function calls into same-module functions so that
    patterns like ``method() -> module_level_helper()`` are covered.
    """
    pattern = _INVOKE_PATTERN if kind == "invoke" else _PUBLISH_PATTERN
    targets: set[str] = set()
    module = inspect.getmodule(cls)
    scanned_funcs: set[str] = set()

    for attr_name in dir(cls):
        obj = getattr(cls, attr_name, None)
        if obj is None or not callable(obj):
            continue
        try:
            source = inspect.getsource(obj)
        except (OSError, TypeError):
            continue
        for match in pattern.finditer(source):
            targets.add(match.group(1))

        # Follow function calls to module-level functions (1 level deep)
        if module:
            for call_match in _FUNC_CALL_PATTERN.finditer(source):
                func_name = call_match.group(1)
                if func_name in scanned_funcs:
                    continue
                func = getattr(module, func_name, None)
                if func and callable(func) and func is not obj:
                    scanned_funcs.add(func_name)
                    try:
                        func_source = inspect.getsource(func)
                    except (OSError, TypeError):
                        continue
                    for m in pattern.finditer(func_source):
                        targets.add(m.group(1))

    return sorted(targets)
