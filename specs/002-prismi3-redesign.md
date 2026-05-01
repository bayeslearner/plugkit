# Prismi3 Redesign — Built on SignalPy Kernel

**Goal:** Redesign the prismi3-agent backend (`prismi3-backend/`) to run on
SignalPy kernel as its foundation. The kernel provides the reactive component
model; prismi3 becomes a set of components composed on top.

**Scope:** Architecture spec only — no code changes to prismi3 source repo.

---

## 1. Current prismi3 Architecture (As-Is)

### Layers

```
┌─────────────────────────────────────────────┐
│ api/routes/*     CLI commands/*              │  Surface
│ (FastAPI)        (Click)                     │
├─────────────────────────────────────────────┤
│ agent/           gateway/                    │  Agent + Gateway
│ (AgentClient,    (ToolPolicy,               │
│  providers,       tiers)                     │
│  hooks, AOP)                                 │
├─────────────────────────────────────────────┤
│ tools/           mcp/                        │  Tool Layer
│ (@tool registry, (FastMCP server,            │
│  unified,         sub-clients)               │
│  schema conv)                                │
├─────────────────────────────────────────────┤
│ apps/            sources/                    │  Domain
│ (splunk, system, (connectors,                │
│  cribl, kestra)   pipeline)                  │
├─────────────────────────────────────────────┤
│ core/                                        │  Foundation
│ (ConfigManager, AppRuntime, lifecycle,        │
│  ServiceManager, paths, gate, schemas)       │
└─────────────────────────────────────────────┘
```

### Key abstractions prismi3 invented that SignalPy already has

| prismi3 | SignalPy equivalent | Notes |
|---------|-------------------|-------|
| `ConfigManager` (3-layer merge, Pydantic on-read) | `ConfigProvider` (@provides IConfig) | SignalPy adds Signal-backed reactivity |
| `AppRuntime` (per-app context: tools, kinds, routes, creds) | `ReactiveRuntime` (self.rt: config, logger, creds, storage) | Same idea, reactive |
| `AppManager` (toposort, activation) | `LifecycleManager` (toposort, state machine) | Same |
| `@tool()` decorator + `_REGISTRY` | `@runnable` + bus registration | Same, bus-unified |
| `UnifiedRegistry` (native + MCP dispatch) | `Bus` (local handlers + pluggable transports) | Bus IS the unified registry |
| `ToolPolicy` + tiers | Could be `APIGateway` policy layer | New extension needed |
| `ConnectorRegistry` | `ServiceRegistry` (@provides + @requires) | Same pattern |
| `AppManifest` + `discover_apps()` | `kernel.discover()` + `PluginLoader` | Same |
| Hydra compose + OmegaConf | (Gap — kernel uses flat dict) | Needs layered config |
| `get_config()` singleton | `self.rt.config` (reactive) | SignalPy is better here |
| `CredentialResolver` | `CredentialProvider` (structural scoping) | Same idea |
| `Provider` ABC (LLM abstraction) | `@provides ILLMProvider` | Natural fit |

### What prismi3 has that SignalPy doesn't (yet)

1. **Typed config** — Pydantic validation per section
2. **Layered config** — defaults → workspace → runtime overrides with persistence
3. **Agent loop** — multi-turn, streaming, tool dispatch
4. **Tool schema conversion** — to_anthropic, to_langchain, to_mcp, to_openai
5. **MCP sub-client registration** — external tool services
6. **Tool gateway** — policy-based filtering, tier management
7. **Case management** — CRUD, lifecycle, audit trail
8. **Conversation persistence** — YAML-based, fork/split
9. **Triage pipeline** — request classification
10. **AOP/hooks** — pre/post tool invocation
11. **Code execution sandbox**
12. **Workspace structure** — git worktrees for dev/prod
13. **Multi-user session management**

---

## 2. Redesigned Architecture (To-Be)

### Three-ring model

```
┌─────────────────────────────────────────────────────────┐
│                    Custom Apps (Ring 3)                   │
│                                                          │
│  SplunkApp         GitLabApp        CriblApp             │
│  @component        @component       @component           │
│  @provides         @provides        @provides            │
│  ISplunkClient     IGitLabClient    ICriblClient          │
│                                                          │
│  Domain knowledge lives here. Each app is a standalone   │
│  package that plugs into Ring 2's extension points.      │
├─────────────────────────────────────────────────────────┤
│                    System Layer (Ring 2)                  │
│                                                          │
│  AgentComponent    CaseManager      IngestPipeline       │
│  @provides         @provides        @provides            │
│  IAgentLoop        ICaseStore       IIngestPipeline       │
│                                                          │
│  ConversationMgr   ToolGateway      MCPClientMgr         │
│  @provides         @provides        @provides            │
│  IConversationStore IToolGateway    IMCPTools             │
│                                                          │
│  LLMProvider       EvalJudge        SessionMgr           │
│  @provides         @provides        @provides            │
│  ILLMProvider      IEvalRunner      ISessionStore         │
│                                                          │
│  Ships with the platform. Replaceable but not domain.    │
├─────────────────────────────────────────────────────────┤
│                    Kernel (Ring 1)                        │
│                                                          │
│  Signal  Computed  Effect  batch                          │
│  @component  @provides  @requires  @runnable  @api       │
│  @lifecycle  @computed  @effect  @subscribe  @kind       │
│  @skill  @prop                                            │
│  Bus  Registry  LifecycleManager  TraitRegistry           │
│  ReactiveRuntime  ConfigProvider  LoggingProvider          │
│  CredentialProvider  StorageProvider  AuthProvider         │
│                                                          │
│  ZERO business logic. ZERO domain knowledge.              │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Cross-Cutting Concerns: Detailed Design

### 3.1 FastAPI vs CLI — Bus Unifies Both

**Problem:** prismi3's CLI mostly delegates to HTTP (round-trip overhead,
requires running server). Some commands bypass FastAPI for direct execution,
creating two code paths.

**Redesign:**

```python
# The component declares its API surface once:
@component("case-manager", version="1.0")
@requires(config="IConfig", storage="IStorage")
@provides("ICaseStore")
@api("rest", prefix="/cases", version="v1")
@api("cli", group="cases")
class CaseManager:

    @runnable("list", params=CaseFilter, description="List cases")
    async def list_cases(self, params):
        return await self._query(params)

    @runnable("create", params=CaseCreate, description="Create a case")
    async def create_case(self, params):
        return await self._create(params)
```

Both transports call `bus.invoke("case-manager.list", params)`:
- **RESTTransport** generates `GET /v1/cases/list` → `bus.invoke()`
- **CLITransport** generates `prismi3 cases list --query=...` → `bus.invoke()`
- **No HTTP round-trip from CLI.** Direct in-process bus call.

### 3.2 Config — Reactive, Typed, Layered

**Problem:** prismi3's ConfigManager is sophisticated (3-layer, Pydantic,
persistent) but not reactive. The kernel's ConfigProvider is reactive but
flat and untyped.

**Redesign — enhance ConfigProvider:**

```python
@component("config", version="2.0")
@provides("IConfig", "IConfigAdmin")
class ConfigProvider:

    @lifecycle.activate
    def activate(self):
        # Layer 1: defaults (shipped with platform)
        base = self._load_yaml("conf/defaults.yaml")
        # Layer 2: workspace overlay
        overlay = self._load_yaml(self._workspace / "local.yaml")
        # Layer 3: runtime overrides (persisted)
        overrides = self._load_yaml(self._workspace / "settings.yaml")
        # Merge: overrides > overlay > base
        merged = deep_merge(base, overlay, overrides)
        self._data = Signal(merged)

    def get(self, key, default=None):
        # Reactive read — tracks dependency
        return dotted_get(self._data.get(), key, default)

    def get_typed(self, section: str, model: type[BaseModel]):
        # Validate section against Pydantic model, cache result
        raw = dotted_get(self._data.get(), section, {})
        return model.model_validate(raw)

    def set(self, key, value):
        # Validate → update Signal → persist → changelog
        current = dict(self._data.peek())
        dotted_set(current, key, value)
        self._data.set(current)  # triggers all @effects that read this key
        self._persist_overrides()
```

**What's different from prismi3:**
- `self.rt.config.get("agent.model")` inside an `@effect` is a tracked read
- When config changes, effects re-run automatically (no cache invalidation needed)
- Pydantic validation via `get_typed()` (same API as prismi3)
- Persistent overrides with atomic write (same as prismi3)

### 3.3 Agent Loop — Component, Not Framework

**Problem:** prismi3's AgentClient is a standalone class with its own
state management. It doesn't participate in the kernel's reactive model.

**Redesign:**

```python
@component("agent", version="1.0")
@requires(llm="ILLMProvider", config="IConfig", tools="IToolGateway")
@provides("IAgentLoop")
@api("rest", prefix="/chat", version="v1")
class AgentComponent:

    @lifecycle.activate
    def activate(self):
        self._max_turns = 20

    @effect
    async def on_config_change(self):
        # Reactive: picks up model/provider changes automatically
        agent_config = self.rt.config.get_typed("agent", AgentConfig)
        self._max_turns = agent_config.max_turns

    @runnable("send", params=ChatParams, description="Send a message")
    async def send(self, params):
        messages = params.history + [{"role": "user", "content": params.message}]

        for turn in range(self._max_turns):
            tool_schemas = self.rt.tools.schemas(format="anthropic")
            response = await self.rt.llm.complete(
                messages=messages,
                tools=tool_schemas,
                system=self._build_system_prompt(params),
            )

            if response.stop_reason == "end_turn":
                return {"messages": messages, "response": response.text}

            # Tool dispatch through the bus
            for tool_call in response.tool_calls:
                result = await self.rt.invoke(tool_call.name, tool_call.params)
                messages.append(tool_result_message(tool_call, result))

        return {"messages": messages, "response": "Turn limit reached"}

    @runnable("stream", params=ChatParams, description="Stream a response")
    async def stream(self, params):
        # Returns an async iterator — needs bus.invoke_stream()
        ...
```

**Key insight:** The agent loop is imperative (request-response-tool-repeat).
It's NOT reactive internally. But its configuration IS reactive — when the
LLM provider is hot-swapped or config changes, effects handle it.

**Streaming challenge:** `@runnable` returns a value. Streaming needs a new
bus method:

```python
# New bus capability:
async for chunk in bus.invoke_stream("agent.stream", params):
    yield chunk  # SSE to client
```

### 3.4 Native Tools vs MCP Tools — Bus as Unified Registry

**Problem:** prismi3 has `UnifiedRegistry` as a separate layer merging
native `@tool()` and MCP sub-client tools.

**Redesign:** The bus IS the unified registry.

```python
# Native tool — just a @runnable on an app component:
@component("splunk", version="1.0")
class SplunkApp:
    @runnable("query", params=SplunkQueryParams, description="Run SPL query")
    async def query(self, params): ...

# MCP external tools — proxied through a manager component:
@component("mcp-client", version="1.0")
@provides("IMCPTools")
class MCPClientManager:

    @lifecycle.activate
    async def activate(self):
        for service in self._discover_services():
            tools = await self._connect(service)
            for tool in tools:
                # Register as bus handler: "mcp-splunk.query" etc.
                self.rt.bus.register(f"mcp-{service.name}.{tool.name}", tool.invoke)

# The agent doesn't care — bus.invoke() dispatches both:
await bus.invoke("splunk.query", params)       # native
await bus.invoke("mcp-splunk.query", params)   # MCP proxy
```

**Schema generation:** Tool gateway reads @runnable metadata from all
components via the registry. MCP tools register their schemas at connection
time. The agent calls `gateway.tools(format="anthropic")` to get a merged
schema list.

### 3.5 Storage — YAML vs Safe/Incremental/Multi-User

**Problem:** prismi3 stores cases as YAML files. Works for single-user dev
but doesn't scale.

**Redesign — pluggable ICaseStore:**

```python
class ICaseStore(Protocol):
    async def get(self, case_id: str) -> Case: ...
    async def put(self, case: Case) -> None: ...
    async def patch(self, case_id: str, patch: dict) -> Case: ...
    async def list(self, filters: CaseFilter) -> list[CaseSummary]: ...
    async def history(self, case_id: str) -> list[LifecycleEvent]: ...

# Backend 1: YAML (dev, current prismi3 behavior)
@component("case-store-yaml", version="1.0")
@provides("ICaseStore")
class YAMLCaseStore: ...

# Backend 2: SQLite (multi-user, incremental, WAL mode)
@component("case-store-sqlite", version="1.0")
@provides("ICaseStore")
class SQLiteCaseStore: ...

# Backend 3: PostgreSQL (production)
@component("case-store-pg", version="1.0")
@provides("ICaseStore")
class PostgresCaseStore: ...
```

Consumer code doesn't change:
```python
@component("case-manager")
@requires(store="ICaseStore")  # any backend
class CaseManager:
    async def create(self, params):
        await self.rt.store.put(case)
```

Hot-swap backends at runtime via `kernel.hot_update()`.

### 3.6 Core vs System vs Custom App Boundaries

**The rules:**

| Ring | Knows about | Doesn't know about |
|------|-------------|-------------------|
| **Kernel (1)** | Signals, components, bus, lifecycle | Cases, agents, Splunk, conversations |
| **System (2)** | Cases, agents, conversations, tools | Splunk query syntax, GitLab API, Cribl pipelines |
| **Apps (3)** | Their domain (Splunk, GitLab, etc.) | Each other (no direct app-to-app deps) |

**Extension points Ring 1 provides to Ring 2:**
- `@provides`/`@requires` for service injection
- `@runnable` for tool registration
- `@subscribe` for event handling
- `@effect` for reactive updates
- `@api` for transport exposure
- `@kind` for schema declaration
- `@lifecycle.*` for initialization

**Extension points Ring 2 provides to Ring 3:**
- `IToolGateway` — register tools with policy/tiers
- `ICaseStore` — read/write cases
- `IIngestPipeline` — register data sources
- `ILLMProvider` — LLM abstraction for agent
- Bus events: `case.created`, `case.updated`, `intake.completed`

**Apps extend the system by:**
1. `@provides(ISplunkClient)` — offering domain services
2. `@runnable("query_splunk", ...)` — registering domain tools
3. `@subscribe("intake.completed")` — reacting to system events
4. `@kind("splunk-error", model=SplunkErrorKind)` — declaring data shapes
5. Registering connectors for the ingest pipeline

### 3.7 Agentic Testing — Judge as Component

**Problem:** No automated multi-turn evaluation. Tests are unit/integration
only. No LLM-as-judge.

**Redesign:**

```python
@component("eval-judge", version="1.0")
@requires(agent="IAgentLoop", config="IConfig")
@provides("IEvalRunner")
class AgenticJudge:

    @runnable("run_scenario", params=EvalScenario)
    async def run_scenario(self, params):
        conversation = []
        results = []

        for turn in params.turns:
            response = await self.rt.invoke("agent.send", {
                "message": turn.user_message,
                "history": conversation,
            })
            conversation = response["messages"]

            # Level 1: Structural check (deterministic)
            structural = self._check_structural(turn.expect, response)

            # Level 2: Semantic check (LLM-as-judge)
            semantic = await self._judge_semantic(turn, response)

            results.append({"turn": turn.index, **structural, **semantic})

            if not structural["passed"]:
                break

        return {"scenario": params.name, "results": results, "passed": all(r["passed"] for r in results)}
```

**Three levels:**
1. **Structural** — deterministic: right tools called? Right params? Right order?
2. **Semantic** — LLM-as-judge: response correct given tool results?
3. **Behavioral** — scenario-based: edge cases, error handling, recovery

**Mock tools for testing:**
```python
@component("mock-splunk", version="1.0")
@provides("ISplunkClient")
class MockSplunkApp:
    @runnable("query", params=SplunkQueryParams)
    async def query(self, params):
        return self._canned_responses.get(params.query, {"results": []})
```

The kernel's `@provides` with service ranking means the mock overrides
the real Splunk app when both are discovered (set mock's ranking lower).

---

## 4. What the Kernel Needs (Gaps to Fill)

### Must have for the redesign

| Gap | What to build | Effort |
|-----|--------------|--------|
| **Typed config** | `get_typed(section, Model)` on ConfigProvider | Small — Pydantic optional import |
| **Layered config** | defaults → workspace → runtime in ConfigProvider | Medium — merge logic, persist |
| **bus.invoke_stream()** | Streaming dispatch for agent responses | Medium — new bus method |
| **Tool schema export** | `gateway.tools(format="anthropic")` from @runnable metadata | Small — schema converter |
| **Pre/post hooks** | Bus middleware for AOP (tracing, validation, auth) | Medium — hook chain |

### Nice to have

| Gap | What to build | Effort |
|-----|--------------|--------|
| **MCP sub-client manager** | Connect to external MCP services, proxy as bus handlers | Medium |
| **Conversation persistence** | IConversationStore contract + YAML backend | Medium |
| **Workspace structure** | IWorkspace with git worktree awareness | Small (exists partially) |
| **Code sandbox** | ISandbox contract for code execution | Small |

### Not needed (kernel provides already)

- Component lifecycle (toposort, activation, deactivation)
- Service registry (provide, require, ref counting)
- Hot-add/hot-remove/hot-update
- Reactive propagation (Signal, Computed, Effect)
- Transport adapters (REST, CLI, MCP)
- Structural scoping (credentials, storage)
- Auth enforcement at bus level

---

## 5. Migration Path

### Phase 1: Enhanced ConfigProvider
- Add `get_typed()` with optional Pydantic validation
- Add layered loading (defaults.yaml + overlay + runtime)
- Add persistent overrides with atomic write
- Keep Signal-backed reactivity

### Phase 2: System Contracts
- Define: IAgentLoop, ILLMProvider, IToolGateway, ICaseStore, IConversationStore
- Implement AnthropicProvider as @component
- Implement AgentComponent with @runnable("send")
- Add bus.invoke_stream() for SSE

### Phase 3: Tool Unification
- Port tool schema converters (to_anthropic, to_langchain)
- Implement ToolGateway component (policy/tier filtering)
- Implement MCPClientManager for external tools

### Phase 4: Case + Conversation
- Implement CaseManager with YAML backend
- Implement ConversationManager with YAML backend
- Port schema gate (Pydantic validation before writes)

### Phase 5: Apps
- Port SplunkApp as @component
- Port SystemApp (case tools)
- Port source connectors as @provides(ISourceConnector)

### Phase 6: Testing
- Implement AgenticJudge component
- Port test scenarios to eval format
- Build mock backends as test @components

---

## 6. What This Proves About the Kernel

If prismi3 redesigns cleanly onto SignalPy, it validates:

1. **Reactivity as foundation works** — config changes propagate automatically
2. **12 decorators are sufficient** — no new decorators needed for a real app
3. **Two-axis model holds** — kernel stays untouched, all domain is components
4. **Bus as unified dispatch** — native tools and MCP tools through one path
5. **Hot-swap is real** — swap LLM providers, storage backends at runtime
6. **The kernel is small enough** — prismi3 is a layer on top, not a fork

What would falsify it:
- If the agent loop needs kernel internals (breaks encapsulation)
- If streaming requires kernel changes beyond bus.invoke_stream()
- If multi-user needs kernel-level locking (storage provider's job)
- If tool schema conversion bloats the kernel (should stay in system layer)
