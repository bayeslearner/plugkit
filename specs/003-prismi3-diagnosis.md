# Prismi3 Backend Diagnosis — What's Actually Broken

This is a code-grounded diagnosis, not an architecture pitch.
Every item has file:line references so you can verify it yourself.

---

## Problem 1: Config Has 3 Access Paths That Can Disagree

There are **three ways** to get config, and they return different objects
at different times:

| Path | Where used | What it returns |
|------|-----------|----------------|
| `request.app.state.config` | FastAPI routes | ConfigManager set once during `lifespan()` (`system.py:105`) |
| `get_config()` | Agent, tools, CLI, MCP | Module-global `_GLOBAL_CONFIG` singleton (`config.py:638`), lazy-loaded |
| Direct `os.environ` / `OmegaConf` | Scattered | Raw env vars, bypassing ConfigManager entirely |

**Where they diverge:**

- `system.py:105` creates `app.state.config = ConfigManager.load(WORKSPACE_ROOT)`
- `get_config()` at `config.py:640-677` creates a SEPARATE `ConfigManager.load()` call
  when first accessed from non-request code (agent tools, MCP server, etc.)
- These are **two different ConfigManager instances** unless `get_config()` happens
  to be called after FastAPI sets `app.state.config`. They share no state.
- `config.py:644` even acknowledges this: "FastAPI routes should prefer
  `request.app.state.config`; this helper serves code paths without a request"
- **Result:** If a route calls `config.set("agent.model", "new")`, the tools
  called by the agent (which use `get_config()`) don't see the change.
  The settings.yaml file sees it (persisted), but the in-memory `_GLOBAL_CONFIG`
  singleton still has the old value.

**Additional mess:**
- `api/routes/settings.py:26-27` tries to bridge: `if hasattr(request.app.state, "config")
  and request.app.state.config is not None: return request.app.state.config` — but
  this only helps routes, not tools.
- `core/context.py:76` says "reuses request.app.state.config rather than re-composing" —
  acknowledging the problem exists.

---

## Problem 2: 6 Global Mutable Registries, No Coordination

Module-level singletons that accumulate state independently:

| Registry | File:Line | What it holds | Who writes | Who reads |
|----------|-----------|---------------|-----------|----------|
| `_GLOBAL_CONFIG` | `config.py:638` | ConfigManager | `get_config()` lazy init | Everything non-request |
| `_REGISTRY` (tools) | `tools/_registry.py:49` | `{name: Tool}` | `@tool` decorator at import | `invoke()`, `UnifiedRegistry` |
| `_registry` (unified) | `tools/unified.py:184` | `UnifiedRegistry()` | Nobody — wraps others | Agent loop |
| `_REGISTRY` (sources) | `source_protocol.py:150` | `SourceRegistry()` | `register_source()` at app activation | Ingest pipeline |
| `_REGISTRY` (commands) | `commands/__init__.py:10` | `{name: Callable}` | `@command` at import | CLI dispatcher |
| `_sub_client_tools` etc. | `mcp/server.py` (4 module dicts) | MCP sub-client state | `register_sub_client()` | `UnifiedRegistry.schemas()` |
| `CONNECTOR_REGISTRY` | `connector_registry.py` | Connector instances | `init_splunk_connectors()` | Routes, tools |
| `_native_tool_to_app` | `gateway/tool_gateway.py` | Tool→app mapping | `_build_native_tool_map()` | Gateway filtering |

**The coordination problem:**
- `UnifiedRegistry.schemas()` at `unified.py:54-96` merges tools from `_REGISTRY`
  (native) and `mcp/server.py` (MCP sub-clients) with a **60-second TTL cache**.
- When a new MCP service starts and registers tools via `register_sub_client()`,
  the agent won't see them for up to 60 seconds.
- When the gateway's tool policy changes (admin disables a tool), the cached
  schema in `UnifiedRegistry` serves stale results for up to 60 seconds.
- `_native_tool_to_app` in `tool_gateway.py` is built lazily and cached — a
  new tool registration doesn't invalidate it.

---

## Problem 3: Tool Identity Lives in 4 Places

A single tool (e.g., `query_splunk`) exists simultaneously in:

1. **`tools/_registry.py:_REGISTRY`** — name → Tool dataclass (import-time)
2. **`mcp/server.py:_server`** — registered as a FastMCP tool (boot-time via
   `_build_native_server()`)
3. **`tools/unified.py:_registry._schema_cache`** — merged Anthropic schema
   (cached, time-based expiry)
4. **`gateway/tool_gateway.py:_native_tool_to_app`** — tool→app mapping
   (cached, lazy-built)

**When they disagree:**
- A tool registered via `@tool` at import but NOT in a gateway tier →
  appears in `_REGISTRY`, appears in MCP, appears in `UnifiedRegistry`
  schemas, but gateway filtering removes it → agent can't call it but
  the MCP server can. The tool API route shows it as disabled but the
  MCP listing shows it as available.
- An MCP sub-client tool registered at runtime → appears in `_sub_client_schemas`,
  appears in `UnifiedRegistry` after cache expires, but `_native_tool_to_app`
  doesn't know about it → `get_tool_app()` returns None → gateway can't
  filter it → the tool is unfiltered while native tools get filtered.

---

## Problem 4: Workspace/Path Resolution Has 4 Fallback Chains

`paths.py` has 24 module-level constants computed at import time.
`_resolve_workspace_root()` at `paths.py:119-148` has 4 fallback levels:

1. `PRISMI3_WORKSPACE` env var
2. `conf/local.yaml::current_workspace`
3. Legacy `conf/active_workspace.yaml::workspace` (deprecated, warns)
4. Hard-coded `.workspaces/prod/`

**Problem:** These constants are computed once at module import. If a test
monkeypatches `WORKSPACE_ROOT`, every other constant that was computed from
the old value is stale. The codebase has `_SRC_DIR = Path(__file__).resolve().parent.parent.parent`
computed independently in **5 different files**:
- `agent/aop.py:65`
- `agent/lifecycle_tools.py:57`
- `agent/split.py:48`
- `commands/push.py:65`
- `sources/tracking.py:67`

Each one resolves the repo root independently. If the file moves, that
computation silently breaks.

**Additionally:** `paths.py` has both free functions AND `_PathsNamespace`
class (in `runtime.py`) that duplicate the same path logic. A component
can call `paths.case_file(id)` (module function) or
`rt.workspace.paths.case_file(id)` (runtime namespace) — same result,
two code paths.

---

## Problem 5: The Runtime Object Is a God Object

`runtime.py` defines 8 classes that know too much about each other:

```
AppRuntime
  ├── .config     → AppScopedConfig (wraps ConfigManager)
  ├── .workspace  → WorkspaceAPI (wraps paths)
  ├── .cases      → CaseAPI (wraps YAML read/write)
  ├── .credentials → CredentialResolver (wraps config + env)
  ├── .bus        → ToolBus (wraps tool dispatch)
  └── methods: .tool(), .kind(), .route()
```

**The problem:** `AppRuntime` is passed to `app.activate(rt)`, and the
app uses `rt.tool()`, `rt.config.get()`, `rt.cases.read()`, etc. But
these all reach back into global singletons:
- `rt.bus.invoke()` calls `get_unified_registry().dispatch()` (global)
- `rt.config` wraps `get_config()` (global singleton)
- `rt.cases` reads from `paths.CASES_DIR` (module constant)
- `rt.credentials` reads from config (global) AND `os.environ` AND
  workspace files

The `AppRuntime` looks like proper dependency injection but is actually
a facade over globals. You can't have two AppRuntimes pointing at
different workspaces in the same process.

---

## Problem 6: Frontend/Backend State Sync Is Request-Based

The frontend polls the backend for state. There's no push mechanism.

- `api/routes/tools.py:get_app_tools()` — rebuilds the tool list from
  scratch on every GET by reading `_REGISTRY` + MCP + gateway + tiers
- `api/routes/instances.py:list_instances()` — reads instance files from
  disk on every GET
- `api/routes/settings.py:get_settings()` — reads from ConfigManager
  (which may be the request's config or the global singleton, depending
  on `_get_cm()`)
- `api/routes/cases.py` — reads YAML from disk on every request

**When they go out of sync:**
- User changes a setting via the Settings UI → `PUT /settings` updates
  `app.state.config` → the change is persisted to `settings.yaml`
- But the agent (running in background) uses `get_config()` (the other
  ConfigManager instance) → doesn't see the change
- Frontend polls `GET /settings` → sees the new value
- Frontend polls `GET /tools` → may see stale cached schemas from
  `UnifiedRegistry` (60s TTL)
- A case updated via the agent tool doesn't notify the frontend —
  the frontend has to poll `GET /cases` to see it

---

## Problem 7: Credential Resolution Has 4 Overlapping Paths

Secrets can be resolved via:

1. **OmegaConf `${oc.env:VAR}`** — in defaults.yaml, resolved at read time
2. **`credentials.yaml`** — workspace-level, `credentials.<name>.value: ${oc.env:VAR}`
3. **`CredentialResolver`** (`runtime.py`) — checks instance config first,
   then falls back to ConfigManager
4. **Legacy `token_env` shim** (`config.py:_materialize_legacy_target_tokens()`) —
   synthesizes `credentials.<app>-<target>` entries from old-style `token_env` fields
5. **Direct `os.environ`** — some code still reads env vars directly

**`config.py:_materialize_legacy_target_tokens()`** materializes legacy
entries into the config dict at load time. But `CredentialResolver` in
`runtime.py` resolves credentials at request time by walking a different
path. If a credential exists in both the legacy shim and the new format,
which one wins depends on merge order — which depends on whether the
config was loaded via `app.state.config` or `get_config()`.

---

## Problem 8: MCP Has Its Own Duplicate Registries

`mcp/server.py` maintains 4 module-level dicts:
- `_sub_client_tools` — tool metadata
- `_sub_client_schemas` — input schemas
- `_sub_client_proxies` — invocation proxies
- `_sub_client_descriptions` — descriptions

These are **separate from** `tools/_registry.py:_REGISTRY`. When
`UnifiedRegistry.schemas()` at `unified.py:70-81` merges them, it
does a name-collision check (`if tool_name not in native_names`) but
the collision check is on the **schema merge**, not on the dispatch
path. `UnifiedRegistry.dispatch()` tries MCP first, native fallback —
meaning an MCP tool with the same name as a native tool shadows it
silently.

`mcp/gateway.py` has its OWN regex patterns (`_ENV_VAR_RE`,
`_ENV_PASSTHROUGH_RE`) duplicated from `core/instance_config.py` —
same patterns, defined in two files, can diverge independently.

---

## Problem 9: Session/Conversation State Is Fragile

- `api/sessions.py:SessionManager` — in-memory dict, lost on restart
- Conversations persisted as YAML in `_chats/<session_id>.yaml` — but
  only written at turn boundaries
- `agent/context.py:ToolContext` — per-request ContextVar, installed at
  route entry, read by tools
- `agent/context.py:SessionState` — per-request ContextVar with
  investigation trace log

If the server crashes mid-turn:
- Session manager loses all sessions
- Last persisted conversation is N turns behind
- Any investigation entries accumulated in SessionState are lost

If two concurrent requests share the same session:
- `SessionManager` has no locking
- Both read the same YAML, both append messages, last writer wins
- The YAML write in `api/helpers.py` is not atomic (no temp+rename)

---

## Summary: The Root Causes

1. **Global singletons instead of dependency injection.** Config, tools,
   MCP, sources, connectors — all module-level globals. Can't have two
   instances, can't test in isolation, can't hot-swap.

2. **Cache-based consistency instead of reactive propagation.** Tool
   schemas cached for 60s. Config changes visible to one path but not
   another. No change notification mechanism.

3. **Multiple representations of the same truth.** A tool exists in 4
   registries. Config accessible via 3 paths. Credentials resolvable
   via 4 paths. Paths computed in 5+ locations.

4. **Facade-over-globals masquerading as DI.** `AppRuntime` looks like
   injected context but delegates to global state. Tests monkeypatch
   globals instead of injecting different instances.

5. **No frontend push.** Frontend polls. Backend changes aren't
   broadcast. Stale UI is the norm, not the exception.

These are exactly the problems SignalPy's reactive model was designed
to solve — but the redesign spec needs to address them specifically,
not just map boxes to boxes.
