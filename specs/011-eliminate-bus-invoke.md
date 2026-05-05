# Eliminate bus.invoke — Direct Dispatch via @requires

Motivated by: prismi3 graph visualization revealed that bus.invoke creates
hidden, stringly-typed dependencies invisible to static analysis. 112
bus.invoke calls vs 33 @requires — the weaker mechanism dominated. The most
important call path (agent → tools) was completely untraceable.

## Decision

**Remove bus.invoke from application code.** All component-to-component
communication uses `@requires` injection + direct method calls. The bus
narrows to an event bus (`publish`/`subscribe` only).

## What changes

### 1. Kill `@api` decorator — collapse into `@runnable` + `@component`

`@api` and `@runnable` are both schema-only metadata declarations. Merge them.

**`@component`** absorbs per-component transport config:

```python
@component("my-app", version="1.0",
           rest={"prefix": "/my-app", "version": "v1"},
           mcp={"name": "my-tools"})
```

**`@runnable`** absorbs per-operation transport visibility:

```python
# All transports + native (default)
@runnable("search", params=SearchParams, description="Search")

# Direct calls only — not exposed on any external transport
@runnable("reindex", params=BaseModel, description="Reindex",
          transports=["native"])

# Agent + direct only, admin required
@runnable("dangerous-op", params=BaseModel, description="Dangerous",
          transports=["native", "mcp"],
          requires_role="admin")
```

`transports=None` (default) means all transports the component declared
plus native. `transports=["native"]` means direct calls only (replaces
`internal=True`).

**Removed:** `@api` decorator, `APIDef` class, `include`/`exclude` lists,
`internal=True` on `@runnable`.

**Decorator count: 12 → 11.**

### 2. `@runnable` becomes schema-only

Today `@runnable` both declares a schema AND registers a bus handler during
activation. After this change, `@runnable` only declares the schema. No
bus handler registration.

At activation time the kernel:
1. Records the schema on `ComponentMeta`
2. Stores `handler: Callable` (the bound method reference) on the schema
3. That's it — no `bus.register_handler()`

### 3. `HandlerSchema` carries handler reference

Add `handler: Callable` to `HandlerSchema` (or its successor). This is the
bound method reference, populated at component activation when the instance
exists. Before activation, `handler` is `None`.

### 4. Kernel provides runnable discovery API

System components need clean access to the merged schema + handler:

```python
# Flat list, filtered by transport. Tool-gateway uses this.
kernel.runnables(transport="native")

# Grouped by component with component metadata attached.
# Transport adapters use this for per-component config (prefix, mcp name).
kernel.runnables_by_component(transport="rest")
```

These replace `bus.schemas()` as the discovery mechanism.

### 5. Remove `self.rt.invoke()` from Runtime

Components use `@requires` + direct method calls:

```python
# Before
result = await self.rt.invoke("case-store.get", {"case_id": "MAIN-0001"})

# After
@requires(store=ICaseStore)
class MyApp:
    async def do_work(self):
        result = await self.rt.store.get(case_id="MAIN-0001")
```

`self.rt.invoke()` and `self.rt.invoke_nowait()` are removed from the
public Runtime API.

### 6. Bus narrows to event bus

The bus keeps:
- `publish(event_type, data)` — fan-out to subscribers
- `subscribe(event_type, handler)` — register event handler
- `unsubscribe(event_type, handler)`
- Wildcard matching (fnmatch)
- `BusTransport` for remote event relay
- Dead letter channel (for failed event delivery)

The bus loses:
- `invoke()`, `invoke_nowait()`
- `register_handler()`, `unregister_handler()`
- `schemas()`, `get_schema()`
- `_resolve_handler()`, name resolution / hallucination hardening
- `_target_routes` (L3 target routing)
- `handler_signal` (moves to kernel — see below)

### 7. `handler_signal` moves to kernel

The Signal tracking available runnables is about operation discovery, not
bus dispatch. It moves to the kernel or a runnable registry. Transport
adapters and tool-gateway use `@effect` on this signal to react when the
runnable set changes (hot-add/hot-remove), rebuilding their surfaces.

```python
# Transport adapter reacts to runnable changes
@effect
async def on_runnables_changed(self):
    schemas = self.rt.kernel.runnables(transport="rest")
    self._rebuild_routes(schemas)
```

## Consumer patterns

### Tool-gateway (agent tool dispatch)

```python
@requires(invoker=ISafeInvoker)
class ToolGateway:
    def build_tools(self):
        for schema in kernel.runnables(transport="native"):
            tool = wrap_as_langchain_tool(
                lambda params: self.rt.invoker.safe_invoke(
                    schema.handler, params),
                schema)
```

Tool-gateway reads schemas, wraps each `schema.handler` through
safe-invoker, builds LangChain tools. Dynamic dispatch (agent picks a
tool by name) works through the tool list, not bus.invoke.

### REST adapter

```python
for component, schemas in kernel.runnables_by_component(transport="rest"):
    prefix = component.meta.rest["prefix"]
    for schema in schemas:
        app.add_route(f"{prefix}/{schema.name}", schema.handler)
```

### MCP adapter

```python
for component, schemas in kernel.runnables_by_component(transport="mcp"):
    group = component.meta.mcp["name"]
    for schema in schemas:
        register_mcp_tool(group, schema)
```

## Safe-invoker becomes a wrapper

Today safe-invoker calls `self.rt.invoke(target, params)` internally.
After this change, it wraps a callable directly:

```python
safe_invoke(schema.handler, params, timeout=5.0, max_size=1_000_000)
```

It stays a component (provides `ISafeInvoker`) so that policy (timeouts,
size caps) is config-driven through the kernel's config system. But it no
longer touches the bus.

## Auth enforcement

Today the bus checks `requires_action`/`requires_role` before dispatch.
With no bus dispatch, each consumer is responsible:

- Tool-gateway checks before calling the handler
- REST adapter checks via middleware
- MCP adapter checks its own way

This is better — auth was one-size-fits-all at the bus level. The schema
carries the auth requirements (`requires_action`, `requires_role`) so
each consumer can enforce them appropriately for its transport.

## L3 Targeted routing

Today L3 uses `bus._target_routes` to route `factory.runnable` +
`target` param → `instance.runnable`. Without bus.invoke, L3 routing
moves to the runnable registry. `kernel.runnables()` returns instance-
level schemas with the target property, and consumers filter/route
by target value directly.

## Multi-process story

Three patterns, each handled differently:

**Remote tool invocation** (process A calls tool in process B):
Transport adapters already expose runnables over HTTP/MCP. Process A
discovers tools via the remote API, calls over HTTP. No bus.invoke needed.

**Remote events** (process A publishes, process B subscribes):
`BusTransport` subclass for NATS/Redis relays `publish()` to a broker
and delivers inbound events to local subscribers. `@subscribe` unchanged.

**Remote services** (process A `@requires` service in process B):
Future spec. Proxy generation for contract interfaces — a proxy
`ICaseStore` that calls over HTTP satisfies `@requires(store=ICaseStore)`
transparently. Cleaner distribution transparency than `bus.invoke` ever
provided.

## What does NOT change

- Reactive core (Signal, Computed, Effect, batch)
- Component model and remaining 11 decorators
- `bus.publish` / `@subscribe` — legitimate loose coupling for events
- Lifecycle states and state machine
- Supervision trees and restart strategies
- Trait system (L0-L3)
- ServiceRegistry with ref counting
- Structural scoping (credentials, storage)
- Constitution rules — rule 5 (distribution transparency) is better
  served by contract proxies than stringly-typed bus.invoke

## Migration path

1. Add `handler: Callable` to schema, add `transports` to `@runnable`
2. Add `kernel.runnables()` and `kernel.runnables_by_component()`
3. Move transport config from `@api` to `@component`
4. Update transport adapters to use new discovery API
5. Update tool-gateway to use `schema.handler` through safe-invoker
6. Remove `self.rt.invoke()` from Runtime
7. Strip invoke machinery from Bus
8. Remove `@api` decorator and `APIDef`
9. Update CLAUDE.md (decorator count 12 → 11, examples)
