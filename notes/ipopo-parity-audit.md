# iPOPO Parity Audit

File-by-file comparison between iPOPO (pelix/) and Bayeslearner Microkernel (kernel/).
Honest assessment: what's borrowed, what's different, what's missing, what's new.

## Size comparison

| | iPOPO | Microkernel |
|---|---|---|
| Core files | 17 files, 14,443 lines | 8 files, 2,560 lines |
| Total project | 101 files | 8 kernel + 9 providers + 3 adapters |
| Required deps | 0 (stdlib only) | 0 (kernel only) |

---

## FILE-BY-FILE: iPOPO → Microkernel mapping

### 1. pelix/framework.py (1949 lines) → No direct equivalent

**iPOPO:** Full OSGi-like bundle system. Bundles are Python modules loaded dynamically. 
Each has its own BundleContext, lifecycle (INSTALLED → ACTIVE → STOPPED), and can be
installed/started/stopped/updated/uninstalled at runtime. Service registration tied to
bundle lifecycle — when a bundle stops, all its services are unregistered.

**Microkernel:** No bundle concept. Components ARE the unit. `kernel.discover([classes])`
replaces bundle installation. `hot_add()`/`hot_remove()` replaces dynamic bundle loading.
There's no BundleContext — components get `self.rt` instead.

**What we lack:**
- Bundle-level isolation (one module = one sandbox)
- Dynamic module loading from filesystem
- Bundle update with component restart
- Framework UUID identification
- Service scopes (SINGLETON, BUNDLE, PROTOTYPE)

**Assessment:** Deliberate omission. Bundles add complexity for features we don't need
(hot module reloading, per-bundle service sandboxing). Our `hot_add`/`hot_remove` covers
the common case. Service scopes are a real gap — Service Factory partially covers it.

---

### 2. pelix/ipopo/decorators.py (2198 lines) → kernel/component.py (752 lines)

**iPOPO decorators we have:**
| iPOPO | Ours | Notes |
|---|---|---|
| `@ComponentFactory(name)` | `@component(name)` | Same thing |
| `@Provides(spec)` | `@provides(spec)` | We accept types too |
| `@Requires(field, spec, ...)` | `@requires(field=spec)` | We accept types too |
| `@Requires(..., aggregate=True)` | `@requires_aggregate(field, spec)` | Split to own decorator |
| `@RequiresMap(field, spec, key=)` | `@requires_map(field, spec, key=)` | Same |
| `@RequiresBest(field, spec)` | `@requires_best(field, spec)` | Same |
| `@Property(field, name, default)` | `@prop(field, name, default)` | Same |
| `@BindField(field)` | `@bind(field)` | Same |
| `@UnbindField(field)` | `@unbind(field)` | Same |
| `@Validate` | `@lifecycle.activate` | Same (we support both sync/async) |
| `@Invalidate` | `@lifecycle.deactivate` | Same |
| `@Instantiate(name)` | Implicit (auto in boot) | We auto-instantiate all factories |

**iPOPO decorators we DON'T have:**
| iPOPO | What it does | Impact |
|---|---|---|
| `@RequiresBroadcast(field, spec)` | Call method on ALL matching services | Medium — workaround: use aggregate + loop |
| `@RequiresVarFilter(field, spec)` | Dynamic LDAP filter based on component properties | Low — niche use case |
| `@Temporal(field, spec, timeout)` | Blocking wait for service with timeout | Medium — useful for startup ordering |
| `@UpdateField(field)` | Callback when dependency's properties change | Low — @bind/@unbind cover most cases |
| `@PostRegistration` | Callback after service registered | Low |
| `@PostUnregistration` | Callback after service unregistered | Low |
| `@HiddenProperty(field, name)` | Property not propagated to services | Low |
| `@SingletonFactory(name)` | Only one instance allowed | Low — can be enforced by convention |

**What we have that iPOPO doesn't:**
| Ours | What it does |
|---|---|
| `@runnable(name, params=Model)` | Typed callable with Pydantic schema — the capability primitive |
| `@api(transport, prefix=)` | Declare REST/MCP/CLI exposure per transport |
| `@subscribe(event_type)` | Declarative event handler (iPOPO uses service interface) |
| `@kind(name, model=)` | Data schema contribution |
| `@skill(name, content=)` | AI knowledge bundle |
| `@exportable(transport=, discovery=)` | Remote service export declaration |
| `@platform_app(name, ...)` | Bundle decorator (component + requires + api) |
| `@lifecycle.health` | Health check callback |
| Type-based contracts | `@requires(x=IDictionary)` — Python types, not strings |
| Annotation injection | Class annotations create implicit requirements |

---

### 3. pelix/ipopo/core.py (1261 lines) → kernel/__init__.py (801 lines)

**iPOPO:** The `_IPopoService` manages factory registration, component instantiation,
instance validation/invalidation, event dispatch. It has a handler plugin system — 8
builtin handler types loaded dynamically.

**Microkernel:** `Kernel` class does the same but simpler. No handler plugin system.
All handler logic (requires, provides, bind) is inline in `_build_runtime()` and
`_register_component_bus()`.

**What we lack:**
- Handler plugin system (add new injection types at runtime)
- Waiting list (deferred instantiation when factory isn't available yet)
- Factory validation (check all required handlers exist before instantiation)
- Auto-restart on bundle update

**Assessment:** The handler plugin system is architecturally elegant — iPOPO can add
new injection types by adding handler modules. Our approach is simpler but less extensible.
For a ~1000 LOC kernel this is fine. If we grow past 3000 LOC, we'd want plugins.

---

### 4. pelix/ipopo/instance.py (896 lines) → kernel/lifecycle_manager.py (261 lines)

**iPOPO `StoredInstance`:** Rich instance management with per-handler state controllers,
error traceback storage, thread-safe state transitions via locks, component context
binding.

**Microkernel `ComponentInstance`:** Simple dataclass — factory_class, meta, name,
properties, state, instance, error, parent, children. State transitions are simple
assignments.

**What we lack:**
- Per-handler state controllers (each handler can independently gate activation)
- Thread-safe state transitions (we rely on GIL)
- Detailed error traceback storage
- KILLED state distinct from STOPPED

**Assessment:** iPOPO needs thread safety because it supports real concurrent service
dynamics. We're async-first (single event loop), so GIL + asyncio suffices. The
per-handler state controllers are powerful but we don't have handlers to control.

---

### 5. pelix/ipopo/contexts.py (567 lines) → kernel/component.py (RequirementDef, ComponentMeta)

**iPOPO:** `Requirement` is a rich object — aggregate flag, optional flag, immediate_rebind,
LDAP filter with caching, copy/serialization. `FactoryContext` carries all metadata.
`ComponentContext` is the runtime accessor.

**Microkernel:** `RequirementDef` is a dataclass with the same flags minus LDAP filter
and immediate_rebind. `ComponentMeta` is the factory context. `Runtime` is the component
context.

**What we lack:**
- LDAP filter on requirements
- immediate_rebind flag
- Filter caching

**Assessment:** LDAP filters are iPOPO's approach to dynamic service selection — 
"give me IDatabase where vendor=postgres and region=us-east". We don't have this.
Our property filtering on `registry.require(**filter_props)` is a basic version.

---

### 6. pelix/internals/registry.py (1544 lines) → kernel/registry.py (196 lines)

**iPOPO:** Thread-safe service registry with ServiceReference (immutable reference),
ServiceRegistration (holder with update/unregister), EventDispatcher for service
events, _UsageCounter for reference counting, _FactoryCounter for per-bundle factory
instances, ListenerHook system, LDAP filter integration.

**Microkernel:** Simple dict-based registry. provide/require/query, on_change listeners,
service.ranking, require_map, require_for (factory support), entries_for.

**What we lack:**
- ServiceReference as an immutable opaque handle
- ServiceRegistration with update/unregister capabilities
- Reference counting (who's using this service?)
- Listener hooks (intercept service events)
- LDAP filter integration
- Thread safety beyond GIL

**Assessment:** Our registry is ~8x smaller because it does less. The reference
counting and ServiceReference pattern is important for production systems — it prevents
dangling references when services disappear. We just inject the raw implementation.

---

### 7. pelix/ipopo/handlers/requires.py (653 lines) → kernel/__init__.py (inline in _build_runtime)

**iPOPO:** Dedicated handler classes (`SimpleDependency`, `AggregateDependency`) that
implement `ServiceListener`. They react to service events, manage binding, handle
rebinding, fire callbacks. Thread-safe.

**Microkernel:** Inline in `_build_runtime()` — a series of if/elif branches that
check `req.aggregate`, `req.key`, `req.best`, `req.optional`. Bind/unbind callbacks
wired via `registry.on_change()` in `_wire_bind_callbacks()`.

**Assessment:** Same behavior, different architecture. iPOPO's approach is more
extensible (add new handler types). Ours is more readable (all logic in one place).

---

### 8-10. requiresmap.py, requiresbest.py, requiresbroadcast.py → our equivalents

**RequiresMap (644 lines):** We have `@requires_map` — same feature, ~20 lines of
implementation in `_build_runtime` + `registry.require_map()`.

**RequiresBest (221 lines):** We have `@requires_best` — same feature via
`registry.require()` with ranking sort.

**RequiresBroadcast (472 lines):** **We DON'T have this.** iPOPO's @RequiresBroadcast
injects a proxy that, when you call any method on it, calls that method on ALL matching
services and returns a list of results. With exception muffling/tracing options.

---

### 11. temporal.py (360 lines) → No equivalent

**iPOPO @Temporal:** Inject a service reference that blocks (with timeout) if the
service isn't available. The reference becomes invalid after the timeout. Useful for
startup ordering — "I need IDatabase, I'll wait 30s for it to appear."

**Microkernel:** We don't have this. Components that depend on unavailable services
get None (optional) or a warning (required). No blocking wait.

**Gap assessment:** Medium. Temporal dependencies are useful in real systems where
services start asynchronously. Workaround: use `@lifecycle.activate` with explicit
retry logic.

---

### 12. properties.py (227 lines) → kernel/component.py (@prop)

**iPOPO @Property:** Injects getter/setter on the component. Changes propagate to
service properties. Thread-safe.

**Microkernel @prop:** Sets default value on instance and includes it in service
registry properties. No dynamic getter/setter — properties are set at registration
time and are static.

**Gap:** Our properties don't propagate changes at runtime. If you change
`self._language = "FR"`, the service registry still shows `"EN"`. iPOPO's properties
are live.

---

### 13. provides.py (344 lines) → kernel/__init__.py (in _register_component_bus)

**iPOPO:** Service controller pattern — a component can enable/disable its provided
services dynamically. ServiceFactory and PrototypeServiceFactory support.

**Microkernel:** Simple provide/unprovide. We added Service Factory (per-consumer
instances) but not PrototypeServiceFactory or service controllers.

---

### 14. configadmin.py (1288 lines) → providers/configadmin.py (150 lines)

**iPOPO:** Full OSGi ConfigAdmin — persistent configurations (JSON files), PID-based
config tracking, ManagedService/ManagedServiceFactory notifications, configuration
directory, LDAP filtering for config queries, config reloading on file change.

**Microkernel:** Basic ConfigAdmin — in-memory configs, PID-based tracking,
ManagedService/ManagedServiceFactory notifications, bus-exposed runnables. No
persistence, no file watching, no LDAP filtering.

**Gap:** Persistence is the big one. Our ConfigAdmin is ephemeral.

---

### 15. eventadmin.py (299 lines) → kernel/bus.py (155 lines)

**iPOPO EventAdmin:** Publish/subscribe with topic-based routing, LDAP event
property filtering, async delivery via thread pool, ServiceEventHandler protocol.

**Microkernel Bus:** Publish/subscribe with exact event type matching. No topic
wildcards, no LDAP filtering on event properties, no thread pool (async/await instead).

**Gap:** Topic wildcards and property filtering. iPOPO's EventAdmin lets handlers
subscribe to `"sensor/temperature/*"` and filter on `(location=building-a)`. Our
bus does exact match only.

---

### 16. ldapfilter.py (927 lines) → No equivalent

**iPOPO:** Full LDAP filter parser — AND/OR/NOT, comparison operators, wildcards,
substring matching, caching, optimization. Used everywhere: service queries,
event filtering, requirement filters.

**Microkernel:** We have `registry.require(**filter_props)` which does exact equality
matching only. No wildcard, no logical operators, no filter expressions.

**Gap assessment:** Significant architectural difference. LDAP filters give iPOPO
fine-grained dynamic service selection. We compensate with typed contracts and
property-based require_map, but can't express complex queries.

---

## WHAT WE HAVE THAT iPOPO DOESN'T

These features are genuinely new — not in iPOPO at all:

### 1. Trait System (kernel/traits.py, 169 lines)
22 computed traits across 4 levels. The kernel infers traits from what a component
declares: `@requires(config=IConfig)` → Configurable trait, `@runnable` → Runnable
trait, `@subscribe` → Subscribable trait. Queryable via `kernel.status()`.

**But:** Traits are currently DIAGNOSTIC ONLY. They appear in `kernel.status()` output
and that's it. They don't drive behavior. No auto-instrumentation for Observable
components. No auto-export for Exportable ones. No rate-limiting for Routable ones.

**Verdict:** The trait system is the biggest architectural differentiator from iPOPO
but it needs to DO more. Today it's metadata.

### 2. @runnable + @api + Gateway + Transport Adapters
Components declare `@runnable("query", params=QueryParams)` and `@api("rest", prefix="/splunk")`.
The APIGateway component collects all declarations. Transport adapters (REST, MCP, CLI)
read from the gateway and auto-generate bindings.

iPOPO has no equivalent. In iPOPO, you write servlets for HTTP, there's no tool/runnable
concept, no API composition layer.

**Verdict:** This is real, tested (FastAPI integration works), and not something iPOPO
can do without a major extension.

### 3. Typed Contracts
`@provides(IDictionary)`, `@requires(dictionary=IDictionary)` — Python types instead
of strings. IDE autocomplete, import-time error detection, `find all references`.
Annotation-based injection: `dictionary: IDictionary` on a class creates an implicit
requirement.

iPOPO v3 added `@Specification` on Protocol classes but it's sugar — the framework
still uses strings internally. We use types natively.

### 4. Bus-Level Auth
`@runnable("delete", requires_action="docs.delete")` — the bus handler checks
IAuth before the runnable executes. Same check for REST, MCP, CLI, direct call.
iPOPO has no auth story at all.

### 5. AI-Native: @kind + @skill
`@kind("alert", model=AlertModel)` — data schema contribution.
`@skill("spl-writer", content="...", triggers=["splunk"])` — AI knowledge bundle.
iPOPO has no AI concepts.

### 6. Structural Scoping
Credentials and storage are automatically scoped per component. `self.rt.creds` can
only see the component's own secrets. `self.rt.storage` is prefixed to the component's
namespace. Not configurable — structural. iPOPO has no equivalent.

### 7. self.rt Unified Namespace
All kernel services under `self.rt.*`. Clean separation from component's own `self.*`.
iPOPO scatters injections across `self._field1`, `self._field2`.

### 8. Sync + Async Support
Runnables, lifecycle, subscribe, bind/unbind all work with both `def` and `async def`.
Detected at decoration time via `inspect.iscoroutinefunction`. iPOPO is sync-only
(uses threading, not asyncio).

---

## TRAIT ASSESSMENT: What Each Trait Actually Does Today

### L0 Traits (always present)
| Trait | Computed from | What kernel does with it | Drives behavior? |
|---|---|---|---|
| identifiable | @component(name) | Names the component in registry/bus | Yes — bus targets, status |
| lifecycle | @lifecycle.activate/deactivate | Manages state transitions | Yes — boot/shutdown |
| dependable | @requires | Toposorts activation order | Yes — dependency resolution |
| registrable | @provides/@requires | Wires services | Yes — injection |
| inspectable | @lifecycle.health | Included in kernel.status() | Partial — reported but not probed |
| factoryable | @component | Allows kernel.instantiate() | Yes — L3 targeted |

### L1 Traits (from @requires)
| Trait | Computed from | What kernel does with it | Drives behavior? |
|---|---|---|---|
| observable | requires ILogger/ITracer | Scoped logger injection | Yes — ComponentLogger |
| configurable | requires IConfig | Config injection | Yes — config access |
| secured | requires ICredentials/IAuth | Scoped credential injection | Yes — structural scoping |
| storable | requires IStorage | Scoped storage injection | Yes — prefix scoping |
| communicable | has runnables/subscriptions | Reported in status | **NO — just metadata** |
| exportable | @exportable() | Reported in status | **NO — RemoteAdapter not implemented** |

### L2 Traits (from decorators)
| Trait | Computed from | What kernel does with it | Drives behavior? |
|---|---|---|---|
| runnable | @runnable | Registers bus handlers, gateway entries | Yes — core capability |
| subscribable | @subscribe | Registers event handlers | Yes — bus subscription |
| kinded | @kind | Stores in kernel.kinds | Partial — stored, not enforced |
| skillful | @skill | Stores in kernel.skills | Partial — stored, not queried by kernel |
| routable | @api | Gateway API surface composition | Yes — transport generation |
| adaptable | (aspirational) | Nothing | **NO — not implemented** |

### L3 Traits (from properties)
| Trait | Computed from | What kernel does with it | Drives behavior? |
|---|---|---|---|
| targeted | properties.target + instantiate | Bus target routing | Yes — L3 routing |
| scoped | (aspirational) | Nothing | **NO** |
| profiled | (aspirational) | Nothing | **NO** |
| versioned | version != "0.0.0" | Reported in status | **NO — just metadata** |

### Summary
- **10 traits drive behavior** (the kernel uses them to do something concrete)
- **4 traits are metadata only** (reported in status, nothing else)
- **3 traits are aspirational** (defined but not implemented)
- **5 traits are partial** (stored but not fully utilized)

---

## PRIORITY GAP LIST

### Must fix (claimed features that don't work)
1. **Exportable trait does nothing** — declared, computed, but RemoteAdapter not implemented
2. **Adaptable trait does nothing** — defined but never computed or used
3. **Scoped/Profiled traits do nothing** — L3 aspirational, not implemented
4. **Properties don't propagate at runtime** — @prop sets initial value only

### Should add (iPOPO features that matter)
5. **Topic wildcards in bus.subscribe** — `"sensor/*"` matching like iPOPO EventAdmin
6. **@RequiresBroadcast** — call all matching services, collect results
7. **@Temporal** — timeout-based wait for service availability
8. **Config persistence** — ConfigAdmin should survive restart
9. **ServiceReference pattern** — immutable handle, reference counting

### Nice to have (low priority)
10. LDAP filter for service queries
11. Service controller (enable/disable provided services)
12. PrototypeServiceFactory
13. UpdateField callback (dependency property changed)
14. immediate_rebind option on @requires
15. Handler plugin system for extensible injection types

---

## WHAT WOULD MAKE TRAITS A REAL DIFFERENTIATOR

Right now traits are mostly diagnostic. To make them drive behavior:

1. **Observable → auto-instrumentation.** If a component is Observable, the kernel
   should automatically wrap its runnables with tracing spans. Bus calls to/from
   Observable components get spans. No manual `with self.rt.tracer.span():` needed.

2. **Exportable → auto-export.** When a component has the Exportable trait, the kernel
   should automatically register it with the remote services layer. Currently @exportable
   sets metadata but nothing reads it.

3. **Communicable → bus policy auto-application.** If a component is Communicable,
   the kernel could auto-audit its bus calls, rate-limit its publish frequency, etc.

4. **Versioned → version-aware routing.** Multiple versions of the same component
   could coexist, with the bus routing to the right version based on caller preference.

5. **Kinded → schema validation on bus.invoke.** When params flow through the bus,
   the kernel could validate them against the runnable's params_model automatically
   (we do this partially but could enforce more strictly).

6. **Trait-based discovery.** `kernel.query(traits=["runnable", "subscribable"])` →
   "give me all components that are both Runnable and Subscribable." This exists
   partially via `kernel.status()` but not as a first-class query API.
