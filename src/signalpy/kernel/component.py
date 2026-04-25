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
    """A callable with a typed schema — the atomic unit of capability."""
    name: str
    params_model: type
    return_type: type | None
    fn: Callable
    description: str = ""
    timeout_s: float | None = None
    destructive: bool = False
    internal: bool = False   # if True, not exposed via any API
    requires_action: str = ""  # auth action required (e.g. "docs.write")
    requires_role: str = ""    # auth role required (e.g. "admin")
    is_async: bool = True    # detected at decoration time (default True for backward compat)


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
class APIDef:
    """An API surface declaration — how a component wants to be exposed.

    An App can declare multiple APIs (REST + MCP + CLI).
    Each transport adapter picks up declarations matching its type.
    """
    transport: str                        # "rest", "mcp", "cli", "grpc", or custom
    prefix: str = ""                      # REST: /splunk;  CLI: splunk
    name: str = ""                        # MCP: tool group name
    auth: bool = False                    # require auth for this surface
    version: str = ""                     # API version (e.g. "v1")
    rate_limit: str = ""                  # e.g. "100/min"
    include: list[str] | None = None      # explicit runnable list (None = all non-internal)
    exclude: list[str] | None = None      # exclude specific runnables
    properties: dict[str, Any] = field(default_factory=dict)  # transport-specific config


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
    apis: list[APIDef] = field(default_factory=list)         # API surface declarations
    activate_fn: Callable | None = None
    activate_is_async: bool = False
    deactivate_fn: Callable | None = None
    deactivate_is_async: bool = False
    health_fn: Callable | None = None
    snapshot_fn: Callable | None = None
    snapshot_is_async: bool = False
    restore_fn: Callable | None = None
    restore_is_async: bool = False
    properties: dict[str, Any] = field(default_factory=dict)
    exportable: dict[str, str] | None = None  # {transport, discovery}
    dependencies: list[str] = field(default_factory=list)  # factory names

    @property
    def qualified_name(self) -> str:
        """Namespace-qualified factory name: 'ns.factory' or just 'factory'."""
        if self.namespace:
            return f"{self.namespace}.{self.factory_name}"
        return self.factory_name

    def api_runnables(self, transport: str) -> list[RunnableDef]:
        """Return runnables that should be exposed for a given transport.

        If the component has an @api declaration for this transport,
        apply its include/exclude filters.  If no @api for this transport,
        return empty (component doesn't want this transport).
        """
        api_def = next((a for a in self.apis if a.transport == transport), None)
        if api_def is None:
            return []

        candidates = [r for r in self.runnables if not r.internal]

        if api_def.include is not None:
            candidates = [r for r in candidates if r.name in api_def.include]
        if api_def.exclude:
            candidates = [r for r in candidates if r.name not in api_def.exclude]

        return candidates

    def has_api(self, transport: str | None = None) -> bool:
        """Check if this component declares any API (or a specific transport)."""
        if transport:
            return any(a.transport == transport for a in self.apis)
        return bool(self.apis)


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
    depends: list[str] | None = None,
):
    """Mark a class as a component factory.

    This is the equivalent of iPOPO's @ComponentFactory — it says
    "this POPO is now factoryable by the kernel."

    Namespace scopes bus targets, service registry, storage prefix,
    and credential paths.  Defaults to "" (root namespace).

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
        meta.dependencies = depends or []
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
    requires_action: str = "",
    requires_role: str = "",
):
    """Declare a method as a runnable — a callable with a typed schema.

    Runnables are the atomic unit of capability.  Transport adapters
    (MCP, REST, CLI) read runnables from the registry and auto-generate
    bindings.  The component never knows which transport serves it.

    Set internal=True to keep the runnable on the bus but hide it
    from all API surfaces.  Internal runnables are for cross-component
    calls only.

    Auth enforcement (bus-level, all transports):
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
            requires_action=requires_action,
            requires_role=requires_role,
            is_async=inspect.iscoroutinefunction(fn),
        ))
        return fn
    return decorator


def api(
    transport: str,
    *,
    prefix: str = "",
    name: str = "",
    auth: bool = False,
    version: str = "",
    rate_limit: str = "",
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    **properties: Any,
):
    """Declare an API surface for a specific transport.

    An App can stack multiple @api decorators — one per transport it
    wants to be exposed through.  Transport adapters pick up only
    the declarations matching their type.

    Examples:
        @api("rest", prefix="/splunk", auth=True, version="v1")
        @api("mcp", name="splunk-tools")
        @api("cli", prefix="splunk")
        class SplunkApp: ...

    If a component has NO @api declarations, transport adapters
    fall back to exposing all non-internal runnables (backward compat).
    """
    def decorator(cls: type) -> type:
        meta = _get_meta(cls)
        meta.apis.append(APIDef(
            transport=transport,
            prefix=prefix,
            name=name,
            auth=auth,
            version=version,
            rate_limit=rate_limit,
            include=include,
            exclude=exclude,
            properties=properties,
        ))
        return cls
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


def effect(fn: Callable) -> Callable:
    """Declare a method as a reactive side effect.

    The method body reads from self.rt.* — those reads are tracked
    automatically. When any tracked dependency changes, the method re-runs.

        @effect
        async def on_config_change(self):
            url = self.rt.config.get("my-app.url")
            await self._reconnect(url)

    No need to declare dependencies — the kernel figures them out
    from what you read (like Vue's watchEffect).
    """
    fn.__effect__ = EffectDef(fn=fn, is_async=inspect.iscoroutinefunction(fn))
    return fn


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


# ── Trait bundles (convenience composites) ─────────────────────────

def exportable(*, transport: str = "jsonrpc", discovery: str = "mdns"):
    """Mark this component's provided services for remote export."""
    def decorator(cls: type) -> type:
        meta = _get_meta(cls)
        meta.exportable = {"transport": transport, "discovery": discovery}
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

    # Auto-infer dependencies from @requires contracts
    # Convention: IConfig → "config", ILogger → "logging", etc.
    _CONTRACT_TO_FACTORY = {
        "IConfig": "config",
        "ILogger": "logging",
        "ICredentials": "credentials",
        "IStorage": "storage",
        "IAuth": "auth",
        "ITracer": "tracing",
        "IWorkspace": "workspace",
        "IGateway": "gateway",
    }
    for contract in meta.requires.values():
        factory = _CONTRACT_TO_FACTORY.get(contract)
        if factory and factory not in meta.dependencies and factory != meta.factory_name:
            meta.dependencies.append(factory)

    return meta
