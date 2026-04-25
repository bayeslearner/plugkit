# SignalPy Microkernel

A Signal-based reactive component kernel for backend services. The kernel is
three reactive primitives (Signal, Computed, Effect) + component wiring.
Everything else is components.

## What is this?

A small (~2,600 LOC core) reusable kernel built on a two-axis model:

**Axis 1 — The Mechanism** (irreplaceable core, `src/signalpy/kernel/`):
- **Reactivity engine** — Signal, Computed, Effect primitives (Vue 3 / Preact Signals model)
- **Component model** — `@component`, `@provides`, `@requires`, `@runnable`, `@api` decorators
- **Lifecycle manager** — dependency-ordered activation (toposort), reverse-ordered shutdown
- **Service registry** — give/take: components provide and require services by contract name
- **Service bus** — `invoke(target, params)` (request/response) + `publish/subscribe` (events)
- **Runtime** — per-component scoped context with structural isolation (credentials, storage)
- **Trait system** — L0–L3 traits auto-computed from decorators, queryable at runtime

**Axis 2 — The Vocabulary** (replaceable components, `providers/` + `adapters/`):
- **Platform components** — config (Signal-backed, layered YAML + runtime updates), logging (structured JSON), credentials (per-app scoped), storage (local FS), tracing (OTel bridge)
- **API Gateway** — composes `@api` declarations from all components into unified surfaces per transport
- **Transport adapters** — REST (FastAPI), MCP (tool server), CLI (Click)

## Quick Example

```python
from pydantic import BaseModel
from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle, computed, effect

class GreetParams(BaseModel):
    name: str = "world"

@component("greeter", version="1.0")
@provides("IGreeter")
@requires(config="IConfig")
class GreeterApp:

    @lifecycle.activate
    def activate(self):
        pass  # self.rt is available here

    @computed
    def prefix(self):
        """Cached. Auto-recomputes when config changes."""
        return self.rt.config.get("greeter.prefix", "Hello")

    @effect
    def on_config_change(self):
        """Auto-tracks deps, re-runs when they change."""
        prefix = self.rt.config.get("greeter.prefix")
        print(f"Greeter prefix is now: {prefix}")

    @runnable("greet", params=GreetParams, description="Greet someone by name")
    async def greet(self, params):
        return {"message": f"{self.prefix()}, {params.name}!"}
```

The same runnables automatically appear as REST endpoints, MCP tools, and CLI commands — the component never knows which transport serves it.

## The 13 decorators

```
@component  @provides  @requires           # core
@computed   @effect                         # reactive
@lifecycle.activate/deactivate/health       # lifecycle
@runnable   @api   @exportable              # surface
@prop   @kind   @skill                      # metadata
@subscribe                                  # events
```

## Project Layout

```
src/signalpy/
├── kernel/                  ← Axis 1 (~2,600 LOC core)
│   ├── __init__.py          Kernel class: boot(), shutdown(), discover()
│   ├── reactive.py          Signal, Computed, Effect, batch
│   ├── component.py         13 decorators + metadata
│   ├── runtime.py           ReactiveRuntime: Signal-backed injection
│   ├── registry.py          ServiceRegistry: provide(), require(), query()
│   ├── bus.py               Bus: invoke(), publish(), subscribe()
│   ├── lifecycle_manager.py LifecycleManager: state machine, toposort
│   ├── traits.py            TraitRegistry: L0-L3 trait definitions
│   └── contracts.py         Protocol interfaces: IConfig, IStorage, ILogger, etc.
│
├── providers/               ← Axis 2: platform components
│   ├── config.py            ConfigProvider → IConfig + IConfigAdmin (Signal-backed)
│   ├── logging_provider.py  LoggingProvider → ILogger (structured JSON)
│   ├── credentials.py       CredentialProvider → ICredentials (per-app scoped)
│   ├── storage.py           StorageProvider → IStorage (local filesystem)
│   ├── auth.py              AuthProvider → IAuth (token-based, pluggable)
│   ├── workspace.py         WorkspaceProvider → IWorkspace (paths, settings)
│   ├── tracing.py           TracingProvider → ITracer (OTel + Phoenix bridge)
│   └── gateway.py           APIGateway → IGateway (surface composition)
│
├── adapters/                ← Axis 2: transport adapters
│   ├── rest.py              RESTTransport (FastAPI routes)
│   ├── mcp.py               MCPTransport (MCP tools)
│   └── cli.py               CLITransport (Click commands)
│
├── tests/                   253 tests
└── examples/                Progressive examples (01-07)
```

## Running

```bash
# Reactive config demo
PYTHONPATH=src python -m signalpy.examples.03_reactive_config

# Full test suite (253 tests)
PYTHONPATH=src python -m pytest src/signalpy/tests/ -v
```

## Constitution

1. **Everything is a Component.** Storage, auth, logging, the API layer — all components.
2. **Components Give and Take.** No globals, no singletons, no ambient state.
3. **The Kernel has zero business logic.** All domain behavior lives in apps.
4. **Transport is an adapter, never a core concern.**
5. **Distribution is transparent.** In-process or cross-network — caller doesn't know.
6. **Apps are deployment units, components are composition units.**
7. **Lifecycle is explicit and managed.**
8. **Every API has a client counterpart.**
9. **The kernel is small.** If you can't read the entire kernel source in one sitting, it's too big.

## Dependencies

- `kernel/` has ZERO required dependencies
- `providers/` needs: pyyaml (optional, for YAML config)
- `adapters/rest` needs: fastapi
- `adapters/cli` needs: click
- Tracing needs: opentelemetry-api, opentelemetry-sdk
- Examples/tests need: pydantic

## Inspiration

iPOPO (OSGi patterns for Python), Vue 3 (Signal/Computed/Effect reactivity),
Dapr (building blocks), Engin/Uber Fx (give/take DI).
