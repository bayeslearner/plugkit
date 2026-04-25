# Bayeslearner Microkernel v2 (Reactive)

## What this is

A Vue 3-style reactive component kernel for backend services. 13 decorators,
2,661 lines across 9 files. Source-embeddable package. Two-axis architecture:

- **Axis 1** (`kernel/`) -- the irreplaceable mechanism: reactivity engine,
  lifecycle, registry with ref counting, bus, runtime, component model, traits
- **Axis 2** (`providers/`, `adapters/`) -- replaceable vocabulary: config,
  logging, credentials, storage, auth, workspace, tracing, gateway,
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
5. Distribution is transparent.
6. Apps are deployment units, components are composition units.
7. Lifecycle is explicit and managed.
8. Every API has a client counterpart.
9. The kernel is small. Readable in one sitting.

## The 13 decorators

```
@component  @provides  @requires           # core
@computed   @effect                         # reactive
@lifecycle.activate/deactivate/health       # lifecycle
@runnable   @api   @exportable              # surface
@prop   @kind   @skill                      # metadata
@subscribe                                  # events
```

Down from 21 in v1. Removed: `@requires_aggregate`, `@requires_map`,
`@requires_best`, `@bind`, `@unbind`, `@on_change`, `@platform_app`.
Unified `@requires` handles all injection modes. `@computed` and `@effect`
replace manual dependency callbacks.

## How to write a component

```python
from pydantic import BaseModel
from kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    computed, effect, api, subscribe, kind, skill,
)
from kernel.contracts import IConfig, ILogger

class SearchParams(BaseModel):
    query: str
    limit: int = 10

@component("my-app", version="1.0")
@provides("IMyService")
@requires(config=IConfig, logger=ILogger)
@api("rest", prefix="/my-app", version="v1")
@api("mcp", name="my-tools")
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

    @runnable("_reindex", params=BaseModel, internal=True, description="Reindex")
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
- `@bind("config")` / `@unbind("config")` -- use `@effect` instead
- `@on_change("config")` -- use `@effect`, it auto-tracks
- `@platform_app(...)` -- be explicit with `@component` + `@api`

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

**`self.rt` is the component's window.** All kernel services are namespaced
under `self.rt` to avoid conflicts with your own attributes. Every read is
reactive (tracks dependencies when inside `@effect` or `@computed`):
- `self.rt.config` -- IConfig (scoped)
- `self.rt.logger` -- ILogger (scoped ComponentLogger with component identity)
- `self.rt.creds` -- ICredentials (scoped to component's credential namespace)
- `self.rt.storage` -- IStorage (scoped to component's storage prefix)
- `self.rt.invoke("other.runnable", params)` -- bus call (policy-checked)
- `self.rt.publish("event.type", data)` -- bus event
- `self.rt.spawn("factory", "name", props)` -- create child component
- `self.rt.properties` -- instance properties (for L3 targeted)
- `self.rt.peek("config")` -- read without reactive tracking

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
`@runnable`, `@api`, `@subscribe`, `@kind`, `@skill`, `@computed`, `@effect`
attach metadata. The kernel reads it at discovery time. Dependencies are
auto-inferred from `@requires`.

**Trait system is real, not labels.** The kernel auto-computes traits from
what a component declares. `@requires(config=IConfig)` gives it the
Configurable trait. `@runnable` gives it the Runnable trait. `@computed` or
`@effect` gives it the Reactive trait. `kernel.status()` reports all traits
per component, including reactive status (computed/effect counts).

**Gateway pattern.** Components declare `@api("rest", ...)`, `@api("mcp", ...)`.
The `APIGateway` component composes all declarations into unified surfaces per
transport. Transport adapters (`RESTTransport`, `MCPTransport`, `CLITransport`)
read from the gateway and render the surface. Components never know which
transport serves them.

**L3 Targeted.** Same factory, multiple instances with different config:
```python
kernel.instantiate("splunk", "splunk-prod", {"target": "prod"})
kernel.instantiate("splunk", "splunk-dev",  {"target": "dev"})
# Route by target: kernel.bus.invoke("splunk.query", {"target": "prod"})
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

**Auth enforcement at bus level.** Runnables can declare auth
requirements. The bus checks IAuth before invoking -- same check
regardless of transport (REST, MCP, CLI, or direct bus call):

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

**Batch updates.** Group multiple signal changes so effects fire once:
```python
from kernel import batch

with batch():
    name_signal.set("Alice")
    age_signal.set(30)
# Effects that depend on name or age run ONCE here, not twice
```

## Boot sequence

```
kernel = Kernel()
kernel.discover([ConfigProvider, LoggingProvider, ..., MyApp, RESTTransport])
await kernel.boot()
# 1. Set up reactive propagation (service changes update consumer Runtimes)
# 2. Instantiate all factories
# 3. Resolve dependency order
# 4. Activate in order: build Runtime (Signal-backed), register bus handlers,
#    set up @computed and @effect wrappers
# 5. Gateway rebuilds surfaces, transports re-activate
await kernel.shutdown()
# Deactivates in reverse order, disposes effects/computeds
```

## Contracts (Protocol interfaces in `kernel/contracts.py`)

| Contract | Methods | Provider |
|----------|---------|----------|
| IConfig | `get(key, default)` | ConfigProvider |
| ILogger | `info/warning/error/debug(msg, **kwargs)` | LoggingProvider |
| ICredentials | `get(key, target=)`, `for_target(t)`, `list_targets()` | CredentialProvider |
| IStorage | `put/get/list/delete(key)` (async) | StorageProvider |
| IAuth | `authenticate(token)`, `authorize(identity, action)` | AuthProvider |
| ITracer | `span(name, **attrs)` context manager | TracingProvider |
| IWorkspace | `.root` (Path), `.settings` (dict) | WorkspaceProvider |
| IConfigAdmin | `get_configuration(pid)`, `update(pid, props)`, `delete(pid)` | ConfigAdminProvider |

## Trait levels

| Level | Name | Traits | Inferred from |
|-------|------|--------|---------------|
| L0 | Kernel | identifiable, lifecycle, dependable, registrable, factoryable, inspectable | every component / `@lifecycle.health` |
| L1 | Platform | configurable, observable, secured, storable, communicable, exportable | `@requires(config=IConfig)`, runnables, `@exportable` |
| L2 | App | runnable, subscribable, kinded, skillful, routable, reactive | `@runnable`, `@subscribe`, `@kind`, `@skill`, `@api`, `@computed`/`@effect` |
| L3 | Instance | targeted, scoped, profiled, versioned | properties, `version=` |

## Project layout

```
kernel/                  Axis 1 -- reactive kernel v2 (2,661 lines, 9 files)
  reactive.py              Signal, Computed, Effect, batch (327 lines)
  component.py             13 decorators + metadata (673 lines)
  runtime.py               ReactiveRuntime with signal-backed injection (151 lines)
  registry.py              ServiceRegistry with ref counting (199 lines)
  bus.py                   invoke/publish/subscribe (155 lines)
  lifecycle_manager.py     state machine + effect lifecycle (270 lines)
  traits.py                L0-L3 trait system (173 lines)
  contracts.py             Protocol interfaces (74 lines)
  __init__.py              Kernel orchestrator (639 lines)
kernel_v1/               original v1 kernel (preserved)
providers/               Axis 2 -- platform components
adapters/                Axis 2 -- transport adapters (REST, MCP, CLI)
entries/                 Bootstrap templates (fastapi_entry.py, cli_entry.py)
tests/                   Test suite
examples/                Deployment examples (library, FastAPI, Django, CLI, MCP, Tauri)
```

## Running

```bash
PYTHONPATH=. python examples/full_demo.py   # 10 components, all transports
PYTHONPATH=. python examples/as_library.py  # plain library + hot_add/hot_remove
PYTHONPATH=. python examples/as_tauri.py    # Tauri/PyO3 blue-green upgrade sim
pytest tests/test_reactive_kernel.py        # 34 tests for v2
pytest tests/                               # full test suite
```

## Dependencies

- `kernel/` has ZERO required dependencies
- `providers/` needs: pyyaml (config)
- `adapters/rest` needs: fastapi
- `adapters/cli` needs: click
- `adapters/mcp` needs: (MCP SDK when pinned)
- Tracing needs: opentelemetry-api, opentelemetry-sdk
- Example needs: pydantic
