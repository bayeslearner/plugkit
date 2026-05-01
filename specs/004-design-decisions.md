# Design Decisions — Captured from Discussion

These decisions were reached through conversation on 2026-04-30/05-01.
They inform the kernel enhancements and the prismi3 rewrite.

---

## 1. Registry vs Bus — Strong vs Weak Dependencies

**Registry** = strong dependency (lifecycle-coupled).
`@requires(config=IConfig)` means "I cannot exist without this. Boot me after it.
If it disappears, I'm broken."

**Bus** = weak dependency (runtime-optional).
`bus.invoke("splunk.query", params)` means "I can talk to this if it's there,
but I don't need it to exist." The caller doesn't hold a reference, doesn't
import the interface, and functions fine without it.

**The justification for both:**
- Registry provides: boot ordering (toposort), reactive propagation
  (Signal-backed injection), hot-swap, ref counting. You need the dependency
  graph to manage lifecycle.
- Bus provides: late binding, transport-agnostic dispatch, dynamic tools.
  You need the communication channel for loose coupling.

**A component can look up and message another component it doesn't depend on
for lifecycle.** That's the bus's purpose — weak dependency communication.

---

## 2. Bus Must Carry Schemas

The bus currently stores `{name: handler}` with no metadata. For tool
discovery (agent, gateway, transports), it needs:

```python
bus.register_handler(name, handler, schema=RunnableDef)
```

Schema includes: params model (Pydantic), description, metadata.
The gateway reads schemas FROM the bus — not from ComponentMeta directly.
This makes dynamic tool registration (MCP, hot-add) work without a parallel
discovery system.

**Nobody hardcodes tool names.** Names are assigned by the kernel
(`"{instance}.{runnable}"`). Discovery is via `bus.schemas()`. The LLM picks
tools from schemas. The agent dispatches whatever the LLM chose. Hardcoded
bus targets in application code = code smell = should be `@requires` instead.

---

## 3. Bus Must Be Observable

A Signal tracking registered handler names. When handlers are added/removed,
effects that read the tool list re-run. Kills the 60-second cache problem.

---

## 4. External MCP Servers Are Resources, Not Components

External MCP servers (GitLab, Google Workspace, etc.) are **resources managed
by a component**, not components themselves.

The `MCPBridge` component:
- Owns the connection lifecycle (connect, health-check, reconnect)
- Registers discovered tools on the schema-carrying bus
- Reacts to credential changes via `@effect` on config → reconnects
- Handles OAuth as a `@runnable("get_auth_url")` — just another bus operation

The kernel doesn't manage external process lifecycle (Docker Compose does).
The kernel manages the **connection** and **tool registration** for those
external processes.

**OAuth flow:** Frontend calls a runnable, gets auth URL, user completes OAuth
in the external MCP server's own callback. Kernel doesn't know when it
completes — frontend polls `check_connected` or the MCP server pushes
(if supported).

---

## 5. Kernel Graph Introspection

`kernel.graph()` exposes all relationships as one queryable structure:
- Dependency edges (who @requires whom)
- Reactive edges (which Signals feed which Effects/Computeds)
- Bus handlers (who registered what)
- Event subscriptions (who publishes, who subscribes)

This is the machine-readable version of what Serena traces manually.
All data already exists in kernel internals — needs ~50-100 lines to collect
and expose.

---

## 6. Config Layering — Detailed Design

### Layers (disk)

```
Layer 1: Pydantic model defaults     ← code, never written at runtime
Layer 2: workspace/local.yaml        ← operator writes, deployment config
Layer 3: workspace/settings.yaml     ← UI writes here, runtime overrides
```

### Rules

- Later layers win
- Writes from UI ALWAYS target Layer 3. Never touch Layer 1 or Layer 2.
- `reset(key)` removes Layer 3 override → falls back to Layer 2 or 1

### Updates are not always leaf-key

Three update patterns:

| Pattern | Example | Semantics |
|---------|---------|-----------|
| Leaf update | `set("agent.model", "opus")` | Single key changes |
| Branch replace | `set_branch("apps.splunk.targets", {...})` | Entire subtree replaced |
| Branch merge | `set_branch("apps.splunk.targets", {...}, merge_fn)` | Custom merge (e.g., preserve secrets) |

Branch updates need merge strategies:
- `REPLACE` — new dict replaces old (absent keys = deleted)
- `PATCH` — only present keys updated (absent keys = unchanged)
- `CUSTOM` — caller provides merge function (for domain logic like secret preservation)

### Traceability

Every value carries provenance:
```python
config.get_with_source("agent.model")
# → ("claude-opus", {"layer": 3, "file": "settings.yaml", "updated_at": ...})

config.get_with_source("safety.allow_writes")
# → (True, {"layer": 2, "file": "local.yaml", "updated_at": ...})
```

### Scoping — structural isolation

- Components see `IConfig` — flat reads, scoped to their section
- Settings UI sees `IConfigAdmin` — write, reset, provenance, branch updates
- `self.rt.config.get("targets")` is scoped to `apps.{component_name}.targets`
- Components can't read other components' config sections
- Shared sections (safety, observability) accessible via explicit `@requires`

### Validation

- **Boot time:** full merged config validated against PlatformConfig. Bad YAML = fail to boot.
- **Write time:** validate section against Pydantic model BEFORE updating Signal. Reject invalid.
- **Read time:** `get_typed()` validates with caching. Belt and suspenders.

### Pydantic models ARE the schema + defaults

No separate `defaults.yaml` for fields with Pydantic defaults. The model is
the single source of truth for "what keys exist and their default values."
YAML files are only for overrides — things that differ from code defaults.

This kills drift: if it's in the model, it has a default. If it's in YAML
but not in the model, validation catches it.

### Schema evolution

Migration logic in Pydantic `@model_validator(mode="before")`:
```python
class AgentConfig(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def migrate(cls, values):
        if "llm_model" in values and "model" not in values:
            values["model"] = values.pop("llm_model")
        return values
```

Old YAML with renamed keys still works. No separate migration scripts.

### Reactive propagation

Signal holds the merged view. `config.set()` validates → updates Signal →
persists Layer 3. Every `@effect` that read config re-runs immediately.
No cache TTL. No `_typed_cache.clear()`. No second singleton.

### Disk ↔ Memory cycle

```
Boot:   disk (L1+L2+L3) → merge → validate → Signal (memory)
Read:   Signal.get() → value (reactive-tracked)
Write:  validate → Signal.set(new) → persist Layer 3 only
Reset:  remove from L3 → re-merge L1+L2 → Signal.set(new) → persist
```

---

## 7. Branch Strategy

- **Kernel enhancements** (Phase 0: schema bus, observable bus, kernel.graph) → main branch
- **Prismi3 rewrite** → separate repo (e.g., `signalpy-platform`), depends on
  `signalpy-kernel` as a package

---

## 8. Phased Plan (Revised)

### Phase 0: Kernel enhancements (on main)
- 0a: Schema-carrying bus (~50 lines)
- 0b: Observable bus — Signal tracking handler names (~20 lines)
- 0c: `kernel.graph()` — unified introspection (~80 lines)

### Phase 1: Enhanced ConfigProvider (on main)
- Layered loading (Pydantic defaults + YAML overlay + runtime overrides)
- `get_typed()` with Pydantic validation
- Leaf + branch updates with merge strategies
- Provenance tracking (which layer provided this value)
- Scoped reads per component
- Signal-backed reactivity (one truth, immediate propagation)
- Persistent overrides (atomic write to settings.yaml)

### Phase 2+: Prismi3 rewrite (separate repo)
- MCPBridge component (external tool lifecycle)
- Agent as component
- Storage contracts (ICaseStore, IConversationStore)
- Port apps

---

## 9. The 9 Diagnosed Problems → Solutions Map

| # | Problem | Solution |
|---|---------|----------|
| 1 | 3 config paths disagree | One ConfigProvider, one Signal, no global singleton |
| 2 | 6 global registries, no coordination | Bus is the unified tool registry (schema-carrying, observable) |
| 3 | Tool identity in 4 places | Tool = @runnable on a component = one bus handler with schema |
| 4 | Path resolution in 4+ places | Workspace paths are ConfigProvider's concern, not module constants |
| 5 | God object AppRuntime over globals | Real DI via self.rt — no globals behind the facade |
| 6 | Frontend polls, no push | Signal changes → transport can push via SSE |
| 7 | Credential resolution 4+ paths | One CredentialProvider, structural scoping, @effect on rotation |
| 8 | MCP duplicate registries | MCPBridge registers on schema-carrying bus — one registry |
| 9 | Session state fragile | ISessionStore contract, atomic writes, proper locking |
