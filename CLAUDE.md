# SignalPy Microkernel v2 (Reactive)

## What this is

A Signal-based reactive component kernel for backend services. 11 decorators,
~3,800 lines across 9 files. Source-embeddable package. Two-axis architecture:

- **Axis 1** (`src/signalpy/kernel/`) -- the irreplaceable mechanism: reactivity
  engine, lifecycle, registry with ref counting, bus, runtime, component model,
  traits
- **Axis 2** (`src/signalpy/providers/`, `adapters/`) -- replaceable vocabulary:
  config, logging, credentials, storage, auth, workspace, tracing, gateway,
  REST/MCP/CLI transport adapters

**v2 core idea:** reactivity IS the foundation. Services are wrapped in
Signals. `self.rt.config` is a reactive read -- inside `@effect` or
`@computed`, the kernel tracks what you read and re-runs when it changes.
No manual callbacks. No `@bind`/`@unbind`. No `@on_change`. The reactive
graph handles propagation automatically.

## Constitution (non-negotiable rules)

1. Everything is a component. No privileged subsystems.
2. Components give and take. No globals, no singletons, no ambient state.
3. The kernel has zero business logic.
4. Transport is an adapter, never a core concern.
5. Distribution can be transparent — contracts hide location.
6. Apps are deployment units, components are composition units.
7. Lifecycle is explicit and managed.
8. Every API is transport-agnostic.
9. The kernel is small. Readable in one sitting.

## The 11 decorators

```
@component  @provides  @requires           # core
@computed   @effect                         # reactive
@lifecycle.activate/deactivate/health       # lifecycle
@runnable                                   # surface (schema-only)
@prop   @kind   @skill                      # metadata
@subscribe                                  # events
```

Unified `@requires` handles all injection modes (single, aggregate, map,
optional). `@computed` and `@effect` provide reactive dependency tracking.
`@runnable` is schema-only (name, params, description); transport
visibility is per-runnable via `transports=[]`. Transport config
(REST prefix, MCP name) is on `@component`.

## How to write a component

```python
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    computed, effect, subscribe, kind, skill,
)
from signalpy.kernel.contracts import IConfig, ILogger

class SearchParams(BaseModel):
    query: str
    limit: int = 10

@component("my-app", version="1.0",
           rest={"prefix": "/my-app", "version": "v1"},
           mcp={"name": "my-tools"})
@provides("IMyService")
@requires(config=IConfig, logger=ILogger)
@kind("search-result", model=BaseModel, description="Search result schema")
class MyApp:

    @lifecycle.activate
    def activate(self):
        pass  # self.rt is available here

    @computed
    def base_url(self):
        """Cached, auto-recomputes when self.rt.config changes."""
        return self.rt.config.get("my-app.url", "http://localhost")

    @effect
    async def on_config_change(self):
        """Auto-tracks deps, re-runs when they change."""
        url = self.rt.config.get("my-app.url")
        await self._reconnect(url)

    @runnable("search", params=SearchParams, description="Search for things")
    async def search(self, params):
        return {"results": [], "url": self.base_url, "limit": params.limit}

    @runnable("_reindex", params=BaseModel, description="Reindex",
              transports=["native"])
    async def reindex(self, params):
        return {"ok": True}

    @subscribe("data.updated", description="React to data changes")
    async def on_data_updated(self, event_type, data):
        self.rt.logger.info("Data updated", data=data)
```

**Sync and async both work.** Runnables, lifecycle callbacks, subscribe
handlers, effects, and computeds can be `def` or `async def`. The kernel
detects which at decoration time and dispatches correctly.

**Typed contracts.** Use Python types instead of strings:
```python
@provides(IDictionary)              # type instead of "IDictionary"
@requires(dictionary=IDictionary)   # IDE finds references, catches typos
```
Both types and strings work everywhere. Types are recommended for new code.

**What you DON'T write:**
- `depends=["config", "logging"]` -- auto-inferred from `@requires`
- `def activate(self, rt):` -- just `def activate(self):`, use `self.rt.*`
- `params.get("name") if isinstance(params, dict)` -- just `params.name`
- `rt.config` in method args -- just `self.rt.config`

## Key patterns

**Reactivity is the foundation.** Every injected service is a `Signal`.
Reading `self.rt.config` inside an `@effect` or `@computed` is a reactive
read -- the kernel tracks the dependency. When the config provider is
replaced or updated, the effect/computed re-runs automatically.

```python
# This effect auto-tracks self.rt.config. When config changes, it re-runs.
@effect
async def on_config_change(self):
    url = self.rt.config.get("my-app.url")
    await self._reconnect(url)

# This computed caches the result. Only recomputes when deps change.
@computed
def base_url(self):
    return self.rt.config.get("my-app.url", "http://localhost")
```

**Signal-backed config.** The ConfigProvider stores its state in a Signal.
`config.get()` is a reactive read. `config.set()` creates a new dict and
notifies all subscribers. No re-injection hack needed:

```python
config = kernel.registry.require("IConfig")
config.set("scraper.url", "http://production.com")
# All @effect/@computed that read scraper.url re-run automatically
```

ConfigProvider also provides IConfigAdmin for managed service patterns:
```python
@requires(config_admin=IConfigAdmin)
class MyApp:
    async def update_printer(self):
        await self.rt.config_admin.update("printer", {"width": 80})
```

**`self.rt` is the component's window.** All kernel services are namespaced
under `self.rt` to avoid conflicts with your own attributes. Every read is
reactive (tracks dependencies when inside `@effect` or `@computed`):
- `self.rt.config` -- IConfig (scoped)
- `self.rt.logger` -- ILogger (scoped ComponentLogger with component identity)
- `self.rt.creds` -- ICredentials (scoped to component's credential namespace)
- `self.rt.storage` -- IStorage (scoped to component's storage prefix)
- `self.rt.publish("event.type", data)` -- bus event
- `self.rt.spawn("factory", "name", props)` -- create child component
- `self.rt.properties` -- instance properties (for L3 targeted)
- `self.rt.peek("config")` -- read without reactive tracking
- `self.rt.<name>` -- any `@requires` injection, direct method calls

**Unified `@requires`.** One decorator, three injection modes:

```python
# Single service (highest-ranked)
@requires(config=IConfig)
class App: ...  # self.rt.config -> single IConfig

# Aggregate: inject ALL matching services as a list
@requires(dicts=list[IDictionary])
class SpellChecker: ...  # self.rt.dicts -> [en_dict, fr_dict, ...]

# Map: inject services keyed by a property
@requires(dicts=IDictionary, key="language")
class SpellChecker: ...  # self.rt.dicts -> {"EN": en_dict, "FR": fr_dict}

# Optional: component stays valid without this dep
@requires(cache=IConfig, optional=True)
class App: ...  # self.rt.cache -> None if no provider
```

**Decorators declare, kernel executes.** `@component`, `@provides`, `@requires`,
`@runnable`, `@subscribe`, `@kind`, `@skill`, `@computed`, `@effect`
attach metadata. The kernel reads it at discovery time. Dependencies are
auto-inferred from `@requires`.

**Trait system is real, not labels.** The kernel auto-computes traits from
what a component declares. `@requires(config=IConfig)` gives it the
Configurable trait. `@runnable` gives it the Runnable trait. `@computed` or
`@effect` gives it the Reactive trait. `kernel.status()` reports all traits
per component, including reactive status (computed/effect counts).

**Transport adapters.** Components declare transport config on `@component`
(e.g. `rest={"prefix": "/my-app"}`). Transport adapters (`RESTTransport`,
`MCPTransport`, `CLITransport`) use `kernel.runnables_by_component(transport=)`
to discover operations and their schemas, then build routes/tools/commands
by calling `schema.handler` directly. Components never know which transport
serves them.

**L3 Targeted.** Same factory, multiple instances with different config:
```python
kernel.instantiate("splunk", "splunk-prod", {"target": "prod"})
kernel.instantiate("splunk", "splunk-dev",  {"target": "dev"})
# Consumer @requires the service and calls directly
```

**Structural scoping.** Credentials and storage are automatically scoped per
component. A component can only see its own secrets and its own storage prefix.
This is not configurable -- it's structural.

**Ref counting.** The `ServiceRegistry` tracks who is using each service.
`registry.acquire(contract, consumer)` / `registry.release(contract, consumer)`.
`registry.ref_count(contract)` returns total consumers. Hot-remove cleans up
ref counts automatically.

**Component properties.** `@prop` declares mutable properties that
propagate to the service registry. `service.ranking` controls service
selection priority (lower = higher priority):

```python
@prop("_language", "language", "EN")
@prop("_ranking", "service.ranking", 0)
class EnglishDict: ...
```

**Auth enforcement per consumer.** Runnables declare auth requirements
in the schema. Each consumer (transport adapter, tool-gateway) enforces
them appropriately for its transport:

```python
@runnable("delete", params=DeleteParams, description="Delete",
          requires_action="docs.delete")
async def delete(self, params): ...

@runnable("admin_op", params=BaseModel, description="Admin",
          requires_role="admin")
async def admin_op(self, params): ...
```

**Reactive propagation.** When a service is provided or removed at
runtime (hot_add/hot_remove), the kernel automatically updates all
consumer Runtimes. Because injected services are Signals, any `@effect`
or `@computed` that reads the changed service re-runs automatically.
No manual wiring needed.

**Supervision trees (spec 010).** Components that spawn children can
declare a supervision strategy. When a child fails activation, the
supervisor decides whether/how to restart:

```python
@component("my-supervisor", version="1.0")
class MySupervisor:
    @lifecycle.activate
    async def activate(self):
        await self.rt.spawn("worker-a")
        await self.rt.spawn("worker-b")

    @lifecycle.supervision(
        strategy="one_for_one",      # one_for_one | one_for_all | rest_for_one
        max_restarts=3,
        within_seconds=60,
        backoff="exponential",
        base_delay=2.0,
    )
    async def on_child_failure(self, child_name, error, attempt, context):
        return True  # proceed with restart
```

Strategies: `one_for_one` (restart failed child), `one_for_all` (restart
all siblings), `rest_for_one` (restart failed + everything after it).
Exceeding max_restarts escalates to the supervisor's own supervisor.

**Dead letter channel.** Failed event deliveries are published to
`__dead_letter__` for monitoring:

```python
self.rt.on("__dead_letter__", self._on_failure)
```

**Batch updates.** Group multiple signal changes so effects fire once:
```python
from signalpy.kernel import batch

with batch():
    name_signal.set("Alice")
    age_signal.set(30)
# Effects that depend on name or age run ONCE here, not twice
```

## Lifecycle states

```
DISCOVERED → RESOLVED → ACTIVATING → ACTIVE → DEACTIVATING → STOPPED
                  ↑              ↘ ERRORED
                  └── RESTARTING ←┘  (supervised retry with backoff)
```

## Boot sequence

```
kernel = Kernel()
kernel.discover([ConfigProvider, LoggingProvider, ..., MyApp, RESTTransport])
await kernel.boot()
# 1. Set up reactive propagation (service changes update consumer Runtimes)
# 2. Instantiate all factories
# 3. Resolve dependency order
# 4. Activate in order: build Runtime (Signal-backed), populate schema handlers,
#    set up @computed and @effect wrappers
# 5. If activation fails and component has a supervisor → supervised restart
# 6. Transport adapters rebuild surfaces from kernel.runnables()
await kernel.shutdown()
# Deactivates in reverse order, disposes effects/computeds
```

## Contracts (Protocol interfaces in `kernel/contracts.py`)

| Contract | Methods | Provider |
|----------|---------|----------|
| IConfig | `get(key, default)`, `set(key, value)`, `all()` | ConfigProvider |
| IConfigAdmin | `get_configuration(pid)`, `update(pid, props)`, `delete(pid)` | ConfigProvider |
| ILogger | `info/warning/error/debug(msg, **kwargs)` | LoggingProvider |
| ICredentials | `get(key, target=)`, `for_target(t)`, `list_targets()` | CredentialProvider |
| IStorage | `put/get/list/delete(key)` (async) | StorageProvider |
| IAuth | `authenticate(token)`, `authorize(identity, action)` | AuthProvider |
| ITracer | `span(name, **attrs)` context manager | TracingProvider |
| IWorkspace | `.root` (Path), `.settings` (dict) | WorkspaceProvider |
| IManagedService | `updated(properties)` | (consumer protocol) |
| IManagedServiceFactory | `updated(pid, props)`, `deleted(pid)` | (consumer protocol) |

## Trait levels

| Level | Name | Traits | Inferred from |
|-------|------|--------|---------------|
| L0 | Kernel | identifiable, lifecycle, dependable, registrable, factoryable, inspectable | every component / `@lifecycle.health` |
| L1 | Platform | configurable, observable, secured, storable, communicable, supervisable | `@requires(config=IConfig)`, runnables, `@lifecycle.supervision` |
| L2 | App | runnable, subscribable, kinded, skillful, routable, reactive | `@runnable`, `@subscribe`, `@kind`, `@skill`, `@computed`/`@effect` |
| L3 | Instance | targeted, scoped, versioned | `@prop`, `@requires(creds/storage)`, `version=` |

## Project layout

```
src/signalpy/
  kernel/                  Axis 1 -- reactive kernel v2 (~3,800 lines, 9 files)
    reactive.py              Signal, Computed, Effect, batch
    component.py             11 decorators + metadata + SupervisionDef
    runtime.py               ReactiveRuntime with signal-backed injection
    registry.py              ServiceRegistry with ref counting
    bus.py                   event bus: publish/subscribe + dead letter
    lifecycle_manager.py     state machine + supervision strategies
    traits.py                L0-L3 trait system
    contracts.py             Protocol interfaces
    __init__.py              Kernel orchestrator
  providers/               Axis 2 -- platform components
  adapters/                Axis 2 -- transport adapters (REST, MCP, CLI)
  tests/                   Test suite (341 tests)
  examples/                Progressive examples (01-07)
```

## Running

```bash
PYTHONPATH=src python -m signalpy.examples.03_reactive_config  # Signal-backed config
PYTHONPATH=src python -m pytest src/signalpy/tests/             # 341 tests
```

## Dependencies

- `kernel/` has ZERO required dependencies
- `providers/` needs: pyyaml (config YAML loading, optional)
- `adapters/rest` needs: fastapi
- `adapters/cli` needs: click
- `adapters/mcp` needs: (MCP SDK when pinned)
- Tracing needs: opentelemetry-api, opentelemetry-sdk
- Example needs: pydantic
