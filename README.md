# SignalPy Kernel

[![PyPI](https://img.shields.io/pypi/v/signalpy-kernel.svg)](https://pypi.org/project/signalpy-kernel/)
[![Python](https://img.shields.io/pypi/pyversions/signalpy-kernel.svg)](https://pypi.org/project/signalpy-kernel/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-bayeslearner.github.io-blue)](https://bayeslearner.github.io/signalpy-kernel/)

A Signal-based reactive component microkernel for Python backend services.

The kernel is three reactive primitives — **Signal**, **Computed**, **Effect** — plus
component wiring. Everything else (config, logging, credentials, storage, REST,
MCP, CLI) is just components built on top.

> **Disclosure.** Built with Claude's help. The author hopes it lands somewhere
> between "trash" and "god code" — and is actively asking Python folks who
> know reactive systems, DI containers, or microkernels to tell them which
> mistakes were made. Reviews welcome via [Issues](https://github.com/bayeslearner/signalpy-kernel/issues)
> or [Discussions](https://github.com/bayeslearner/signalpy-kernel/discussions).

## Install

```bash
pip install signalpy-kernel             # core kernel only (zero deps)
pip install "signalpy-kernel[all]"      # + providers + REST/CLI + tracing
```

## 60-second example

```python
import asyncio
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle, computed, effect,
)
from signalpy.kernel.contracts import IConfig
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider


class GreetParams(BaseModel):
    name: str = "world"


@component("greeter", version="1.0")
@provides("IGreeter")
@requires(config=IConfig)
class Greeter:

    @lifecycle.activate
    def activate(self):
        pass  # self.rt.config, self.rt.logger, etc. now available

    @computed
    def prefix(self):
        # Cached. Auto-recomputes when config changes.
        return self.rt.config.get("greeter.prefix", "Hello")

    @effect
    def on_prefix_change(self):
        # Auto-tracks deps. Re-runs when they change.
        print(f"prefix is now: {self.rt.config.get('greeter.prefix')}")

    @runnable("greet", params=GreetParams, description="Greet someone by name")
    async def greet(self, params):
        return {"message": f"{self.prefix()}, {params.name}!"}


# Consumer uses @requires — direct method call, no bus.invoke
@component("app", version="1.0")
@requires(greeter="IGreeter")
class App:
    async def run(self):
        result = await self.rt.greeter.greet(GreetParams(name="Alice"))
        print(result)  # {"message": "Hello, Alice!"}


async def main():
    kernel = Kernel()
    kernel.discover([ConfigProvider, LoggingProvider, Greeter, App])
    await kernel.boot()

    # Change config — the @effect re-runs automatically.
    kernel.registry.require("IConfig").set("greeter.prefix", "Howdy")

    await kernel.shutdown()


asyncio.run(main())
```

`@runnable` declares the operation schema. Transport adapters (REST, MCP, CLI)
discover schemas via `kernel.runnables()` and call `schema.handler` directly.
Components never know which transport serves them.

## What makes it different

- **Reactivity is the foundation, not a layer on top.** Every injected service
  is a `Signal`. Reading `self.rt.config` inside an `@effect` or `@computed`
  is a tracked read — when config changes, the effect re-runs automatically.
  No manual callbacks, no `@on_change`, no re-injection hacks.

- **11 decorators total.** `@component`, `@provides`, `@requires`, `@computed`,
  `@effect`, `@lifecycle.*`, `@runnable`, `@subscribe`, `@kind`, `@skill`,
  `@prop`. That's the whole API surface.

- **Two-axis architecture.** Axis 1 (the kernel) is irreplaceable mechanism: ~3,800
  LOC across 9 files, zero required dependencies. Axis 2 is replaceable vocabulary:
  config, logging, credentials, storage, REST/MCP/CLI transports — all just
  components. The kernel is small enough to read in one sitting.

- **Same `@runnable` → multiple transports.** Transport adapters discover
  `@runnable` schemas and expose them as REST endpoints, MCP tools, or CLI
  commands. Per-runnable `transports=[]` controls visibility. Auth requirements
  are declared on the schema and enforced by each consumer.

## Documentation

The full guided tour is at **<https://bayeslearner.github.io/signalpy-kernel/>**:

- **Tutorials** — first component → give-and-take → dynamic services → runnables → gateway → auth → building a provider
- **Concepts** — architecture, reactivity by example, line-by-line annotated reactive engine, threading model, deployment scales
- **Patterns** — reactive-intent recipes (`batch`, `is_stale`, `cancel_on_supersede`, cross-thread writes, mutate-in-place, first-run, cleanup), secret rotation, A/B testing, multi-tenant, hot code update, more
- **Reference** — traits (L0–L3), all 11 decorators, contracts, kernel API

## Project layout

```
src/signalpy/
├── kernel/                  Axis 1 — the irreplaceable core (~3,800 LOC, 9 files)
│   ├── reactive.py            Signal, Computed, Effect, batch
│   ├── component.py           11 decorators + metadata
│   ├── runtime.py             ReactiveRuntime: Signal-backed injection
│   ├── registry.py            ServiceRegistry: provide/require + ref counting
│   ├── bus.py                 Event bus: publish / subscribe
│   ├── lifecycle_manager.py   Dependency-ordered activation, effect lifecycle
│   ├── traits.py              L0–L3 trait system
│   └── contracts.py           Protocol interfaces (IConfig, ILogger, IStorage, …)
│
├── providers/               Axis 2 — platform components (config, logging,
│                            credentials, storage, auth, tracing, gateway, …)
├── adapters/                Axis 2 — transport adapters (REST/FastAPI, MCP, CLI/Click)
├── examples/                Progressive examples 01–07
└── tests/                   341 tests
```

## Constitution (the non-negotiable rules)

1. Everything is a component. No privileged subsystems.
2. Components give and take. No globals, singletons, or ambient state.
3. The kernel has zero business logic.
4. Transport is an adapter, never a core concern.
5. Distribution can be transparent — contracts hide location.
6. Apps are deployment units, components are composition units.
7. Lifecycle is explicit and managed.
8. Every API is transport-agnostic.
9. The kernel is small. Readable in one sitting.

## Inspiration

- **Vue 3 / Preact Signals / SolidJS** — the Signal/Computed/Effect reactive model
- **iPOPO** — OSGi-style component lifecycle for Python
- **Dapr** — building blocks as pluggable components
- **Engin / Uber Fx** — give-and-take dependency injection

## License

MIT — see [LICENSE](LICENSE).
