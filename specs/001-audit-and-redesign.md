# Audit + Prismi3-on-SignalPy Redesign

## Part 1: Kernel Audit — What to Trim

### Dead code (remove)

| What | File:Line | Why dead |
|------|-----------|----------|
| `untracked()` | `reactive.py:604-613` | Only called in 1 test. No real component uses it. |
| `dispose_all()` | `reactive.py:616-619` | Only called in 2 tests. Effects self-dispose via lifecycle. |
| `Signal.update()` | `reactive.py:161-163` | 1 test. Everyone calls `.set()` directly. |

**Keep but document as internal:** `Signal.peek()` — used heavily in runtime.py,
providers, circuit-breaker. It's real.

### Unrealized claims (downgrade or remove from docs)

| Claim | Where claimed | Reality | Action |
|-------|--------------|---------|--------|
| Constitution #5 "Distribution is transparent" | CLAUDE.md:27, README | `BusTransport` is an empty ABC. No RemoteAdapter, no DaprAdapter, no JSON-RPC. Zero distributed capability. | **Reword:** "Distribution *can be* transparent — the bus is designed for pluggable transports." Remove the absolute claim. |
| Constitution #8 "Every API has a client counterpart" | CLAUDE.md:30, README | No client libraries exist. Zero. | **Remove** or reword to "planned." |
| `@exportable` decorator | component.py:562-568, traits.py:106 | Stores metadata (`transport`, `discovery`). Nothing reads it. No mDNS/etcd/Consul registration. | **Remove the decorator entirely.** It's aspirational dead weight. Update decorator count 13→12. |
| `@kind` discovery | component.py:524-543 | Stores kinds in `kernel.kinds`. No `find_kinds_for_model()`, no schema validation pipeline. | **Keep the decorator** (prismi3 uses case kinds), but drop the "components can discover and validate" claim. It's a registry, not a discovery engine. |
| `@skill` matching | component.py:545-557 | Stores skills in `kernel.skills`. Has `triggers` field but no trigger-matching logic. | **Keep** (prismi3 uses skills), same caveat: it's a registry. |
| `TARGETED` trait | traits.py:169 | Never computed. Target routing works in bus, but trait is never set. | **Fix:** add `if meta.properties: traits.append(TARGETED)` in `compute()`. ~1 line. |
| `SCOPED` trait | traits.py:170 | Never computed. Scoping works structurally (creds, storage) but trait not set. | **Fix:** infer from `@requires(creds=ICredentials)` or `@requires(storage=IStorage)`. |
| `PROFILED` trait | traits.py:171 | Completely absent. No code path, no semantics. | **Remove.** |
| Trait system generally | traits.py, CLAUDE.md:291-298 | Traits are computed but never queried by anything except `kernel.status()`. No trait-based routing, no trait predicates, no "components with Configurable trait" queries. | **Honest assessment:** traits are metadata for introspection, not a runtime mechanism. Reword docs accordingly. |

### Summary of trimming

- **Remove:** `untracked`, `dispose_all`, `Signal.update`, `@exportable`, `PROFILED` trait
- **Fix (~3 lines):** compute `TARGETED` and `SCOPED` traits
- **Reword docs:** Constitution #5, #8, trait system, @kind/@skill claims
- **Net effect:** 13 decorators → 12. Trait levels still L0-L3 but L3 is honest: `TARGETED` (yes), `SCOPED` (yes), `VERSIONED` (yes), `PROFILED` (gone)

---

## Part 2: Config Challenge — The Blood of Code

### The problem prismi3 solved (and the pattern that emerged)

Prismi3's `ConfigManager` is a three-layer merge:
1. **Hydra compose** from `conf/defaults.yaml` (immutable base)
2. **Workspace overlays** (`local.yaml`, `credentials.yaml`, `_apps/*/config.yaml`)
3. **Runtime overrides** (`settings.yaml`, persisted atomically, git-backed)

Validation is on-read: `get_typed(section, AgentConfig)` → Pydantic model.
Update is validate-then-persist: `set(key, value)` → Pydantic check → atomic
write → cache clear → git backup → changelog line.

This is **config as state management**, not config as code. The questions:

### How / When / Who / Where

| Dimension | Prismi3 today | SignalPy kernel today | Gap |
|-----------|--------------|----------------------|-----|
| **How** (structure) | OmegaConf DictConfig, Pydantic validation on read | Plain dict, no schema | Kernel has no config schema at all |
| **When** (lifecycle) | Load at boot, hot-update via `set()`, changelog | Load at boot, Signal-backed `set()` triggers effects | Kernel has reactivity but no persistence, no changelog |
| **Who** (scope/access) | Per-app namespace (`apps.splunk.*`), per-workspace credentials, redaction | Per-component structural scoping (creds, storage) | Kernel's scoping is simpler but works |
| **Where** (flow) | ConfigManager singleton → `request.app.state.config` or `get_config()` | `self.rt.config` (Signal-backed, reactive) | Kernel's approach is better for reactivity |

### What the kernel needs to learn from prismi3

1. **Typed config with Pydantic.** The kernel's `config.get("key", default)` returns
   untyped values. Prismi3's `get_typed("agent", AgentConfig)` validates and caches.
   The kernel should support `config.get_typed("section", Model)` alongside the
   current untyped API.

2. **Layered loading.** The kernel's ConfigProvider loads from a flat dict or a
   single YAML file. Prismi3's three-layer merge (defaults → workspace → runtime)
   is battle-tested. The kernel needs at least defaults + overlay + runtime.

3. **Persistent overrides.** Prismi3's `settings.yaml` with atomic write + git
   backup is real operational tooling. The kernel's `config.set()` is in-memory only.

4. **Credential separation.** Prismi3 separates credentials from config with
   OmegaConf `${oc.env:VAR}` interpolation — secrets never land in YAML. The
   kernel's CredentialProvider is similar in spirit but simpler.

### Reconciliation with popular tools

| Tool | What it does | How it relates |
|------|-------------|----------------|
| **Pydantic** | Schema definition + validation | Already used by prismi3. Kernel should adopt for config schemas. Natural fit — Pydantic models ARE the config contract. |
| **datamodel-code-generator** | JSON Schema → Pydantic models | Useful for apps that define config via JSON Schema (like MCP service manifests). Generate Pydantic models from app schemas at install time. |
| **CUE** (Go ecosystem) | Config language with types, constraints, unification | Overkill for Python. CUE's value is compile-time constraint checking for large config surfaces (Kubernetes, Istio). The kernel's config surface is small enough that Pydantic + runtime validation is sufficient. If we ever need cross-component constraint checking ("these two apps can't both claim port 8080"), that's a kernel-level validator, not a language. |
| **Hydra/OmegaConf** | Composable config, interpolation, multirun | Prismi3 uses it. The kernel could adopt OmegaConf for interpolation (`${oc.env:VAR}`) without the full Hydra compose. Or: keep it simple and use `os.path.expandvars()` + Pydantic. |

### Recommendation

The kernel's ConfigProvider should evolve to:
```
ConfigProvider(IConfig, IConfigAdmin)
  ├── Layer 1: defaults.yaml (Axis 2, ships with providers)
  ├── Layer 2: workspace overlay (operator config)
  ├── Layer 3: runtime overrides (Signal-backed, persisted)
  ├── Validation: Pydantic models per section (optional, on-read)
  └── Reactivity: Signal.set() on any mutation → effects re-run
```

The Signal-based reactivity is the kernel's advantage over prismi3's `_typed_cache.clear()`.
Prismi3 invalidates cache and waits for next read; the kernel proactively pushes to
all `@effect` consumers. Keep that.

---

## Part 3: Prismi3-on-SignalPy Redesign

### Architecture overview — what maps to what

```
prismi3 today                          SignalPy redesign
═══════════════                        ═════════════════

core/config.py (ConfigManager)    →    ConfigProvider (@provides IConfig)
                                       + Pydantic schema validation
                                       + layered load + persistent overrides
                                       + Signal-backed reactivity

core/runtime.py (AppRuntime)      →    ReactiveRuntime (self.rt)
                                       Already exists. AppRuntime.tool() →
                                       @runnable. AppRuntime.kind() → @kind.
                                       AppRuntime.resolve_credential() →
                                       self.rt.creds.

core/lifecycle.py                 →    @lifecycle.activate/deactivate
                                       Already exists.

core/gate.py (schema gate)        →    @subscribe("bus.pre_invoke") hook
                                       or: Pydantic params on @runnable
                                       (already validates)

core/app_loader.py                →    kernel.discover() + PluginLoader
                                       Already exists for hot-add/hot-remove.

tools/_registry.py (@tool)        →    @runnable (already exists)
                                       Native tools ARE runnables.

tools/unified.py                  →    Bus + MCP adapter
  (UnifiedRegistry)                    Bus.invoke() for native; MCP adapter
                                       for external. Unified dispatch
                                       through bus routing.

gateway/tool_gateway.py           →    APIGateway (@api declarations)
  (ToolPolicy, tiers)                  + policy layer (new)

mcp/server.py                     →    MCPTransport (already exists)
  (FastMCP, sub-clients)               + sub-client registration (new)

agent/client.py                   →    AgentComponent (new @component)
  (AgentClient loop)                   @effect for reactive context updates
                                       @runnable("chat") for the loop entry

agent/providers/anthropic.py      →    LLMProvider (@component, @provides ILLMProvider)
                                       Hot-swappable via registry. New
                                       provider → all @effects that read
                                       self.rt.llm re-run.

api/routes/*                      →    RESTTransport (already exists)
                                       Routes auto-generated from @runnable
                                       + @api("rest") declarations.

commands/*                        →    CLITransport (already exists)
                                       Commands auto-generated from @runnable
                                       + @api("cli") declarations.

apps/splunk/*                     →    SplunkApp (@component)
                                       @provides ISplunkClient
                                       @requires(config=IConfig, creds=ICredentials)
                                       @runnable("query_splunk", ...)

apps/system/*                     →    SystemApp (@component)
                                       @runnable("case_read", ...)
                                       @runnable("case_write", ...)
```

### Cross-cutting concern #1: FastAPI vs bypassed CLI

**Prismi3 today:**
- FastAPI routes are the primary API surface
- CLI commands mostly delegate to HTTP (`_chat_turn()` calls `/api/chat`)
- Some standalone commands (bootstrap, doctor) run in-process

**SignalPy redesign:**
- **Same @runnable serves both.** `@api("rest")` generates FastAPI routes.
  `@api("cli")` generates Click commands. Both invoke the same @runnable via
  the bus. No HTTP round-trip for CLI — the bus is in-process.
- **CLI bypass is native.** The CLITransport calls `bus.invoke()` directly.
  No need for the HTTP-first pattern prismi3 uses.
- **Standalone commands** (bootstrap, doctor) are just @runnables on a
  SystemApp that aren't exposed via REST.

**Net effect:** Eliminate the HTTP-round-trip pattern for CLI. The bus unifies
all transports.

### Cross-cutting concern #2: Config (see Part 2)

**Key change:** Replace prismi3's `get_config()` singleton with
`self.rt.config` (reactive). When config changes, effects that read config
re-run automatically. No cache invalidation dance.

**Migration path:**
1. `get_config().get("agent.model")` → `self.rt.config.get("agent.model")`
2. `get_config().get_typed("agent", AgentConfig)` → new `self.rt.config.get_typed("agent", AgentConfig)`
3. `get_config().set("key", value)` → `self.rt.config.set("key", value)` (triggers effects)

### Cross-cutting concern #3: Self-managed agent loop vs LangGraph/DeepAgent

**Prismi3 today:**
- `AgentClient` is a hand-rolled multi-turn loop
- `LangGraphAdapter` exists but is a stub
- The loop handles: tool dispatch, context management, conversation persistence, streaming

**Two viable paths:**

**Path A: Keep the hand-rolled loop as a @component**
```python
@component("agent", version="1.0")
@requires(llm="ILLMProvider", tools="IToolRegistry", config="IConfig")
class AgentComponent:

    @effect
    async def on_provider_change(self):
        # Reactive: when LLM provider is hot-swapped, re-init
        self._provider = self.rt.llm

    @runnable("chat", params=ChatParams, description="Multi-turn chat")
    async def chat(self, params):
        # The loop lives here
        messages = params.messages
        while True:
            response = await self._provider.stream(messages, self._tool_schemas())
            if response.stop_reason == "end_turn":
                return response
            # Tool dispatch through bus
            for tool_call in response.tool_calls:
                result = await self.rt.invoke(tool_call.name, tool_call.params)
                messages.append(tool_result(result))
```

**Pros:** Full control, no external deps, reactive config/provider changes
propagate naturally, the kernel's bus handles tool dispatch.

**Cons:** Re-implementing streaming, context management, turn budgets.

**Path B: LangGraph DeepAgent as an external component**
```python
@component("agent-langgraph", version="1.0")
@requires(config="IConfig")
@provides("IAgentLoop")
class LangGraphAgent:
    # LangGraph graph is the loop; kernel provides tools as LangChain tools
    # via schema/to_langchain.py conversion
```

**Pros:** LangGraph handles checkpointing, HITL, subgraph composition.
Deeper agent patterns (multi-agent, supervisor) come free.

**Cons:** LangGraph is an external dependency. Its graph execution model
doesn't align with SignalPy's reactive model — you can't `@effect` inside
a LangGraph node. The two runtimes would coexist awkwardly.

**Recommendation:** **Path A for core, Path B as an optional adapter.**

The agent loop is a component. If someone wants LangGraph, they write a
`LangGraphAgentComponent` that wraps the graph and exposes it as
`@provides("IAgentLoop")`. The kernel doesn't need to know or care — it
just invokes `agent.chat` via the bus.

### Cross-cutting concern #4: Native tool vs MCP tool

**Prismi3 today:**
- Native: `@tool()` decorator → `_REGISTRY` → `invoke()`
- MCP: `register_sub_client()` → namespaced `{name}__{tool}` → JSON round-trip
- Unified: `UnifiedRegistry.dispatch()` tries MCP first, native fallback

**SignalPy redesign:**
- **Native tools ARE @runnables.** `@runnable("query_splunk", ...)` registers
  with the bus. `bus.invoke("splunk.query_splunk", params)` dispatches.
- **MCP tools** are discovered by an MCPClientComponent that `@provides` them
  as runnables on the bus. External MCP tool `foo__bar` becomes
  `mcp-foo.bar` on the bus.
- **Unification through the bus.** The bus IS the unified registry. No separate
  `UnifiedRegistry` needed. `bus.invoke("splunk.query_splunk")` doesn't know
  if `splunk` is a native component or an MCP proxy.

**Schema generation:**
- `@runnable` already has `params` (Pydantic model) and `description`
- Schema conversion (to_anthropic, to_openai, to_langchain) reads from
  the gateway surface, not from a parallel registry
- The agent component calls `gateway.tools()` to get schemas in whatever
  format the LLM provider needs

### Cross-cutting concern #5: Simple YAML vs safe/incremental/fast/multi-user

**Prismi3 today:**
- Case data is YAML files on disk (`records/cases/PREFIX-NNNN/case.yaml`)
- Reads: `yaml.safe_load()`, full file each time
- Writes: full rewrite + atomic replace
- Multi-user: workspace-level git worktrees (`dev`, `prod`)
- No incremental updates, no concurrent write safety beyond git

**The real requirements for production:**
1. **Safe writes** — atomic replace (prismi3 has this)
2. **Incremental updates** — don't rewrite 10MB case files for a tag change
3. **Fast reads** — index/cache hot paths
4. **Multi-user** — concurrent access without corruption
5. **Audit trail** — who changed what when

**SignalPy redesign — storage as a component:**

The kernel's `IStorage` contract is too simple (`put/get/list/delete` with
string keys). For prismi3's needs, replace with a richer `ICaseStore`:

```python
class ICaseStore(Protocol):
    async def get(self, case_id: str) -> Case: ...
    async def put(self, case: Case) -> None: ...
    async def patch(self, case_id: str, patch: dict) -> Case: ...  # incremental
    async def list(self, filters: CaseFilter) -> list[CaseSummary]: ...
    async def history(self, case_id: str) -> list[LifecycleEvent]: ...
```

**Storage backends as swappable components:**
- `YAMLCaseStore` — current prismi3 approach (dev/single-user)
- `SQLiteCaseStore` — incremental, fast, multi-user (WAL mode), zero-dep
- `PostgresCaseStore` — production multi-user with proper ACID

The kernel's `@provides`/`@requires` pattern means the agent component
doesn't know which backend is active. `self.rt.case_store.patch(id, {...})`
works the same regardless.

**Multi-user handling:**
- SQLite WAL mode gives concurrent readers + single writer (good for 10-20 users)
- For real multi-user: PostgreSQL with row-level locking
- The kernel's hot-swap means you can start with SQLite and `hot_update` to
  Postgres when you need it

### Cross-cutting concern #6: Core vs System vs Custom App boundaries

**Three-ring architecture:**

```
┌─────────────────────────────────────────────────┐
│                  Custom Apps                     │
│  SplunkApp, GitLabApp, CriblApp, ...            │
│  @component + @requires + @runnable             │
│  Each app is a standalone package               │
├─────────────────────────────────────────────────┤
│                  System Layer                    │
│  AgentComponent, CaseManager, ToolGateway,      │
│  ConversationManager, IngestPipeline            │
│  @component + @provides system contracts        │
│  Ships with the platform, not the kernel        │
├─────────────────────────────────────────────────┤
│                  Kernel (Axis 1)                 │
│  Signal, Computed, Effect, batch                 │
│  @component, @provides, @requires, @runnable     │
│  Bus, Registry, Lifecycle, Traits               │
│  ZERO business logic. ZERO domain knowledge.     │
└─────────────────────────────────────────────────┘
```

**Extension points (kernel provides, apps consume):**

| Extension point | Kernel mechanism | What apps do |
|----------------|-----------------|--------------|
| **Tool registration** | `@runnable` | App declares tools as runnables |
| **Service provision** | `@provides(ISplunkClient)` | App provides domain-specific service |
| **Config scoping** | `self.rt.config` (namespaced) | App reads `apps.splunk.*` |
| **Credential scoping** | `self.rt.creds` (structural) | App gets only its own secrets |
| **Event handling** | `@subscribe("case.updated")` | App reacts to domain events |
| **Schema declaration** | `@kind("splunk-error", model=...)` | App declares data shapes |
| **Transport exposure** | `@api("rest", prefix="/splunk")` | App gets REST routes automatically |
| **Lifecycle hooks** | `@lifecycle.activate/deactivate` | App initializes/cleans up |
| **Reactive updates** | `@effect` | App reacts to config/service changes |

**What the kernel must NOT know:**
- What a "case" is
- What "Splunk" is
- What an "agent loop" is
- What "MCP" is (beyond the transport adapter)
- What "conversation" or "chat" means

**What the system layer knows:**
- Cases, conversations, ingestion, agent loops — these are system-level
  components that ship with the platform but are NOT part of the kernel.
  They are replaceable components that `@provides` system contracts.

**What custom apps know:**
- Splunk query syntax, GitLab API, ServiceNow schemas — domain knowledge
  that plugs into the system layer via extension points.

### Cross-cutting concern #7: Multi-turn agentic testing

**The problem:** Testing a multi-turn agent backend requires:
1. Simulating multi-turn conversations with tool use
2. Verifying the agent makes correct tool calls in response to user messages
3. Testing error handling, context management, turn budgets
4. Testing complex scenarios (investigation workflows, case mutations)

**Current state in prismi3:**
- ~2,600 tests across 3 tiers (unit, integration, E2E)
- E2E uses `agent-browser` against running dev server
- No LLM-as-judge, no agentic evaluation

**What's needed — a realistic agentic judge:**

```python
@component("eval-judge", version="1.0")
@requires(llm="ILLMProvider", agent="IAgentLoop")
class AgenticJudge:

    @runnable("eval_scenario", params=EvalScenario)
    async def eval_scenario(self, params):
        """
        Run a scenario: sequence of user messages + expected behaviors.
        Judge uses a separate LLM call to evaluate if the agent's
        tool calls and responses match the expected behavior.
        """
        conversation = []
        for turn in params.turns:
            result = await self.rt.invoke("agent.chat", {
                "message": turn.user_message,
                "history": conversation,
            })
            conversation.extend(result.messages)

            # Judge: did the agent do the right thing?
            judgment = await self._judge(
                turn.expected_behavior,
                result.tool_calls,
                result.final_text,
            )
            if not judgment.passed:
                return {"passed": False, "turn": turn.index, "reason": judgment.reason}

        return {"passed": True, "turns": len(params.turns)}
```

**Scenario format:**
```yaml
name: "Investigation workflow — Splunk error triage"
turns:
  - user: "I'm seeing 500 errors in the prod Splunk instance"
    expect:
      tool_calls: ["query_splunk"]
      tool_params_contain: {query: "status=500"}
      no_tool_calls: ["case_write"]  # shouldn't write yet

  - user: "Create a case for this"
    expect:
      tool_calls: ["case_write"]
      tool_params_contain: {kind: "splunk-error"}
      response_contains: "Created case"

  - user: "What's the root cause?"
    expect:
      tool_calls: ["query_splunk"]
      # Agent should query with refined search
      response_not_contains: "I don't know"
```

**Three levels of evaluation:**

1. **Structural** — did the agent call the right tools in the right order?
   (Deterministic, no LLM judge needed)

2. **Semantic** — is the agent's response correct given the tool results?
   (LLM-as-judge, separate model call)

3. **Behavioral** — does the agent handle edge cases correctly?
   (Scenario-based, multi-turn, requires mock tool backends)

**The kernel's role:** The kernel provides the component infrastructure.
The judge is just another component that `@requires` the agent and evaluates
it. The bus handles dispatch. Tool backends can be mocked by providing
test components that `@provides` the same contracts with canned responses.

---

## Part 4: Component Map for the Redesign

### Kernel (Axis 1) — unchanged

```
Signal, Computed, Effect, batch
@component, @provides, @requires, @runnable, @api, @subscribe, @kind, @skill
@lifecycle.activate/deactivate/health/snapshot/restore
@computed, @effect, @prop
Bus, Registry, LifecycleManager, TraitRegistry, ReactiveRuntime
```

### System layer (new, ships with platform)

```python
# Config (enhanced)
ConfigProvider          @provides IConfig, IConfigAdmin
                        Layered load, Pydantic validation, Signal-backed

# Agent
AgentComponent          @provides IAgentLoop
                        @requires ILLMProvider, IToolGateway, IConfig
                        Multi-turn loop, streaming, conversation persistence

# LLM
AnthropicProvider       @provides ILLMProvider
                        Anthropic SDK wrapper, hot-swappable

# Tool Gateway
ToolGatewayComponent    @provides IToolGateway
                        Policy-based filtering, tier management
                        Reads @runnable declarations from all components

# Case Management
CaseManager             @provides ICaseStore
                        CRUD + lifecycle + audit trail
                        Backend-swappable (YAML/SQLite/Postgres)

# Conversation
ConversationManager     @provides IConversationStore
                        Multi-turn state, fork/split, persistence

# Ingestion
IngestPipeline          @provides IIngestPipeline
                        Source connectors, scheduling, rollback

# MCP Client (for external tools)
MCPClientManager        @provides IMCPTools
                        Sub-client registration, namespaced dispatch

# Eval
AgenticJudge            @provides IEvalRunner
                        Scenario-based multi-turn evaluation
```

### Custom apps (user-provided, plugin-loaded)

```python
SplunkApp               @provides ISplunkClient
                        @runnable("query_splunk", ...)
                        @kind("splunk-error", ...)

GitLabApp               @provides IGitLabClient
                        @runnable("list_issues", ...)

CriblApp                @provides ICriblClient
                        @runnable("get_pipelines", ...)
```

---

## Part 5: What's Hard (Honest Assessment)

### 1. The agent loop doesn't fit neatly into @effect

The agent loop is imperative: user message → LLM call → tool dispatch →
repeat. It's not reactive in the Signal/Computed/Effect sense. The reactive
parts (config changes, provider swaps) are peripheral to the loop itself.

**Resolution:** The agent component uses `@effect` for reactive periphery
(config, provider changes) and `@runnable` for the imperative loop entry.
The loop body is plain async Python, not reactive.

### 2. Streaming breaks the @runnable return model

`@runnable` returns a value. Streaming returns chunks over time.
Prismi3's SSE streaming doesn't map to `bus.invoke()` → return.

**Resolution:** Two options:
- `bus.invoke_stream("agent.chat", params)` → `AsyncIterator[chunk]` (new bus method)
- `@runnable` returns a conversation ID; client polls/subscribes for updates

The first is cleaner. Add `invoke_stream()` to the bus contract.

### 3. MCP sub-client tool discovery is dynamic

Native `@runnable` tools are discovered at boot. MCP sub-client tools
appear at runtime (when external services start). The gateway surface
must be rebuilt dynamically.

**Resolution:** The kernel already handles this via reactive propagation.
When the MCPClientManager `@provides` new tools, the gateway's `@effect`
re-runs and rebuilds the surface. This is exactly what SignalPy was built for.

### 4. Pydantic everywhere vs kernel's zero-dep claim

The kernel's core has zero deps. Pydantic is needed for config schemas,
@runnable param validation, @kind models. If we require Pydantic, the
zero-dep claim dies.

**Resolution:** Keep zero-dep for Axis 1 (kernel). Pydantic lives in
Axis 2 (providers, system layer). The kernel's `@runnable(params=...)` accepts
any callable validator — Pydantic is just the default when available. The
kernel itself never imports Pydantic.

### 5. Multi-user state is a storage problem, not a kernel problem

The kernel has no opinion on storage backends. Multi-user concurrent
access is solved by the `ICaseStore` provider, not by the kernel. This
is the right boundary — but it means the kernel can't guarantee data
safety. That's the provider's job.

---

## Part 6: Execution Plan (What to Do First)

### Phase 0: Audit trim (this session)
- Remove dead code (untracked, dispose_all, Signal.update, @exportable, PROFILED)
- Fix TARGETED/SCOPED trait computation
- Reword docs (Constitution #5, #8, trait claims, decorator count)

### Phase 1: Config enhancement
- Add Pydantic validation to ConfigProvider (`get_typed()`)
- Add layered loading (defaults + overlay + runtime)
- Add persistent overrides with atomic write
- Keep Signal-backed reactivity as the differentiator

### Phase 2: System layer scaffolding
- Define system contracts (IAgentLoop, ILLMProvider, IToolGateway, ICaseStore, IConversationStore)
- Implement AgentComponent (port prismi3's AgentClient as a @component)
- Implement AnthropicProvider (port prismi3's provider)
- Add `bus.invoke_stream()` for streaming

### Phase 3: Tool unification
- Port prismi3's tool registry to @runnable declarations
- Port MCPClientManager (sub-client registration)
- Port ToolGateway (policy filtering)

### Phase 4: Storage + persistence
- Implement CaseManager with YAML backend first
- Add SQLite backend for multi-user
- Port conversation persistence

### Phase 5: App porting
- Port SplunkApp as first custom app
- Port SystemApp (case tools)
- Validate the extension-point model works

### Phase 6: Agentic testing
- Implement AgenticJudge component
- Port prismi3's test scenarios to YAML eval format
- Build mock tool backends as test components
