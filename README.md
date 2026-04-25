# Bayeslearner Microkernel

A framework-agnostic, distribution-agnostic component kernel for building extensible applications with AI-native characteristics. Designed to be source-embedded into other projects.

## What is this?

A small (~650 LOC core) reusable kernel built on a two-axis model:

**Axis 1 — The Mechanism** (irreplaceable core, `kernel/`):
- **Component model** — `@component`, `@provides`, `@requires`, `@runnable`, `@api` decorators
- **Lifecycle manager** — dependency-ordered activation (toposort), reverse-ordered shutdown
- **Service registry** — give/take: components provide and require services by contract name
- **Service bus** — `invoke(target, params)` (request/response) + `publish/subscribe` (events), pluggable transport
- **Runtime** — per-component scoped context with structural isolation (credentials, storage)
- **Trait system** — L0–L3 traits auto-computed from decorators, queryable at runtime

**Axis 2 — The Vocabulary** (replaceable components, `platform/` + `adapters/`):
- **Platform components** — config (layered YAML), logging (structured JSON), credentials (per-app scoped), storage (local FS), tracing (OTel bridge)
- **API Gateway** — composes `@api` declarations from all components into unified surfaces per transport
- **Transport adapters** — REST (FastAPI), MCP (tool server), CLI (Click) — all read from the gateway, not raw bus handlers
- **Entry points** — `entries/` bootstraps the kernel for different deployment modes

## Constitution

1. **Everything is a Component.** Storage, auth, logging, the API layer, the CLI — all components.
2. **Components Give and Take. Nothing else.** No globals, no singletons, no ambient state.
3. **The Kernel has zero business logic.** All domain behavior lives in apps.
4. **Transport is an adapter, never a core concern.** Components declare runnables; they never know how they're exposed.
5. **Distribution is transparent.** In-process or cross-network — caller doesn't know.
6. **Apps are deployment units, components are composition units.**
7. **Lifecycle is explicit and managed.** Dependency-ordered activation, reverse-ordered shutdown.
8. **Every API has a client counterpart.** Co-generated from the same contract.
9. **The kernel is small.** If you can't read the entire kernel source in one sitting, it's too big.

## Quick Example

```python
from pydantic import BaseModel
from kernel import Kernel, component, provides, requires, runnable, lifecycle, api

class GreetParams(BaseModel):
    name: str = "world"

@component("greeter", version="1.0", depends=["config"])
@provides("IGreeter")
@requires(config="IConfig")
@api("rest", prefix="/greetings", version="v1")
@api("mcp", name="greeting-tools")
class GreeterApp:

    @lifecycle.activate
    def activate(self, rt):
        self._prefix = rt.config.get("greeter.prefix", "Hello")

    @runnable("greet", params=GreetParams, description="Greet someone by name")
    async def greet(self, rt, params):
        name = params.get("name", "world") if isinstance(params, dict) else "world"
        return {"message": f"{self._prefix}, {name}!"}
```

The same runnables automatically appear as REST endpoints, MCP tools, and CLI commands — the component never knows which transport serves it.

## Project Layout

```
bayeslearner-microkernel/
├── kernel/                  ← Axis 1 (~650 LOC core)
│   ├── __init__.py          Kernel class: boot(), shutdown(), discover()
│   ├── component.py         @component, @provides, @requires, @runnable, @api decorators
│   ├── lifecycle_manager.py LifecycleManager: state machine, toposort, activation
│   ├── registry.py          ServiceRegistry: provide(), require(), query()
│   ├── bus.py               Bus: invoke(), publish(), subscribe()
│   ├── runtime.py           Runtime: per-component scoped context
│   ├── traits.py            TraitRegistry: L0-L3 trait definitions
│   └── contracts.py         Protocol interfaces: IConfig, IStorage, ILogger, etc.
│
├── providers/               ← Axis 2: platform components
│   ├── config.py            ConfigProvider → IConfig (layered YAML)
│   ├── logging_provider.py  LoggingProvider → ILogger (structured JSON)
│   ├── credentials.py       CredentialProvider → ICredentials (per-app scoped)
│   ├── storage.py           StorageProvider → IStorage (local filesystem)
│   ├── auth.py              AuthProvider → IAuth (token-based, pluggable)
│   ├── workspace.py         WorkspaceProvider → IWorkspace (paths, settings)
│   ├── tracing.py           TracingProvider → ITracer (OTel + Phoenix bridge)
│   └── gateway.py           APIGateway → IGateway (surface composition)
│
├── adapters/                ← Axis 2: transport adapters
│   ├── rest.py              RESTTransport → IRestAPI (FastAPI routes)
│   ├── mcp.py               MCPTransport → IMCPServer (MCP tools)
│   └── cli.py               CLITransport → ICLI (Click commands)
│
├── entries/                 ← Bootstrap points
│   ├── fastapi_entry.py     Boot kernel inside FastAPI lifespan
│   └── cli_entry.py         Boot kernel for CLI mode
│
├── tests/                   Test suite (50 tests)
├── examples/                Deployment examples
│   ├── sample_app.py        Shared sample components
│   ├── as_library.py        Plain Python — bus calls + hot_add/hot_remove
│   ├── as_fastapi.py        FastAPI — kernel inside lifespan
│   ├── as_django.py         Django — sync bridge, AppConfig pattern
│   ├── as_cli.py            CLI — Click commands from runnables
│   ├── as_mcp_server.py     MCP — runnables as AI agent tools
│   ├── as_tauri.py          Tauri/PyO3 — Rust shell, blue-green upgrades
│   └── full_demo.py         10 components, all transports, bus cross-calls
└── specs/                   Spec-driven development docs
```

## Deployment Examples

```bash
pip install pydantic pyyaml fastapi click

# Plain library — bus calls, hot_add, hot_remove
PYTHONPATH=. python examples/as_library.py

# FastAPI — REST endpoints auto-generated from @runnable + @api
uvicorn examples.as_fastapi:app --port 8000

# CLI — Click commands auto-generated from @runnable + @api("cli")
PYTHONPATH=. python examples/as_cli.py greet hello name=World

# MCP server — runnables as AI agent tools
PYTHONPATH=. python examples/as_mcp_server.py

# Django — sync bridge, kernel in AppConfig.ready()
PYTHONPATH=. python examples/as_django.py

# Tauri/PyO3 — Rust desktop app, blue-green kernel upgrades
PYTHONPATH=. python examples/as_tauri.py
```

The same components (`GreeterApp`, `MathApp`) work unchanged across all
deployment modes. The component never knows which transport serves it.

## Documentation

- [Architecture Overview](docs/architecture.html) — constitution, two-axis model, boot sequence, cross-process patterns
- [Component Traits](docs/traits.html) — L0–L3 trait catalog, composition examples
- [Code Review Spec](specs/01-code-review/spec.md) — known issues, improvement roadmap

Open HTML docs in a browser for SVG diagrams.

## Status

**v0 — Working prototype.** Core kernel, 8 platform components, 3 transport
adapters, gateway pattern, 64 tests, 7 deployment examples.

**Kernel features:** lifecycle management, service registry, bus (invoke +
pub/sub), runtime scoping, policy enforcement, params validation, drain +
health states, hot_add/hot_remove, trait auto-computation, `@subscribe`,
`@kind`, `@skill`, `@platform_app` bundle, L3 targeted multi-instance routing.

**Remaining gaps:**
- RemoteAdapter (cross-process JSON-RPC + mDNS) is aspirational
- `rt.spawn()` component trees work; `@children` component tree introspection in status TBD

## Inspiration

iPOPO (OSGi patterns for Python), Dapr (building blocks), Engin/Uber Fx (give/take DI), wasmCloud (capability providers).
