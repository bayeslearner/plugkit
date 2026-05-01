# Repo-Wide Patterns from Prismi3

Patterns found outside `src/` that the platform redesign should accommodate.

---

## 1. App Manifest + Workspace Seeding (keep — good pattern)

Each app has a `manifest.yaml` (`_apps/splunk/manifest.yaml`) declaring:
- Name, version, description
- Python package path (`prismi3.apps.splunk`)
- Seedable content directories (skills, case_sources, tags)
- Registered kinds, connectors, native tools
- Target schema (fields the UI renders for per-instance config)
- Config schema (for validation)

On workspace bootstrap, the app's `seeds/` directories are copied into the
workspace. The operator then customizes per workspace.

**Why keep:** This is a clean separation — the app ships defaults, the
workspace owns the runtime state. The manifest is the app's contract with
the platform.

**For SignalPy:** The `@component` decorator + properties handle most of this.
But the manifest (declarative YAML for seeds, target schema, tool tiers) is
workspace-level config-as-code that lives outside the kernel. The PluginLoader
or a `ManifestLoader` component would read these.

---

## 2. Workspace = Git Worktree (keep — operational pattern)

Workspaces are git worktrees on orphan branches, not directories:
- `prod` — orphan branch, sacred, real data
- `dev-data` — branched from prod, safe to mutate
- Tests use ephemeral `tmp_path` — no persistent test workspace

`make bootstrap-dev` creates the worktree. `make reset-dev` tears down and
re-creates from current prod. NEVER `cp -a` (corrupts git link).

**Why keep:** Version-tracked config changes. Branchable dev environments.
Clean separation between code (main branch) and data (workspace branches).

**For SignalPy:** The kernel doesn't need to know about git worktrees. But the
platform's workspace management component should understand this pattern.
The ConfigProvider's deferred layer sources handle the path resolution
naturally.

---

## 3. Skills = Markdown with YAML Frontmatter (keep — content pattern)

Skills are markdown files with YAML frontmatter:
```yaml
---
name: ticket-triage
description: Triage and investigate ServiceNow ticket cases.
triggers_on: [servicenow_tickets]
scope: case
---
# Ticket Triage
When investigating a ticket-kind case...
```

The agent's prompt builder loads matching skills into the system prompt
based on `triggers_on` labels. Skills are workspace content — operators
can add/remove/edit them without code changes.

**Why keep:** This is config-as-code for agent behavior. The markdown body
IS the skill (injected into the prompt). The frontmatter IS the metadata.
Simple, human-editable, version-trackable.

**For SignalPy:** The `@skill` decorator in the kernel registers skills
programmatically. But workspace-level skills (the markdown files) are
loaded by a SkillLoader component, not by the kernel. The kernel's `@skill`
is for code-defined skills; the SkillLoader handles file-defined ones.

---

## 4. Case Sources = YAML Config-as-Code (keep — operational pattern)

Case sources are YAML files describing data ingestion jobs:
```yaml
name: splunk-mc-errors
kind: error
connector: splunk.query
schedule:
  cron: "0 */6 * * *"
config:
  instance: main
  earliest: "-24h"
  source:
    index: _internal
    log_levels: [ERROR, WARN]
```

The scheduler reads these, invokes the connector, validates results through
the schema gate, and writes cases. Operators configure ingestion by editing
YAML — no code changes needed.

**Why keep:** Declarative data pipeline configuration. Operators don't write
Python to add a new data source — they write YAML and drop it in a directory.

**For SignalPy:** This is a pure system-layer concern. An IngestPipeline
component reads case source YAMLs, schedules cron jobs, and dispatches
connector calls via the bus.

---

## 5. Tags = Declarative Matching Rules (keep — domain pattern)

Tags are YAML-defined labels with deterministic match criteria:
```yaml
tags:
  - name: noise
    description: Known benign error — suppress from triage.
    color: gray
  - name: actionable
    description: Error with a known remediation path.
    color: green
```

Apps seed starter tags. Operators customize per workspace.

**For SignalPy:** Pure application concern. A TagManager component.

---

## 6. Test Workspace Fixtures (adopt — testing pattern)

The `conftest.py` creates isolated test workspaces via `tmp_path` +
monkeypatching path constants. `make_case()` + `seed_case()` helpers
build test data without touching real workspaces.

Key pattern: `_reset_singletons_per_module()` — resets the global
ConfigManager between test modules to prevent cross-test leakage.

**For SignalPy:** The kernel doesn't need global resets because there ARE
no globals — each test boots its own kernel instance. But the test helpers
(make_case, seed_case) should be portable to the new system layer.

---

## 7. Docker Compose for MCP Services (keep — deployment pattern)

External MCP services (like mcp-google-workspace) run in Docker containers
managed by `docker-compose.yml`. The platform's `services_up()` starts them,
`services_down()` stops them.

The `external/contrib/google_workspace_mcp/` directory contains the MCP
server source for Google Workspace — it's a separate service with its own
OAuth flow, credential storage, and tool registration.

**For SignalPy:** The MCPBridge component manages connections to these
services. Docker Compose orchestration stays external (Makefile/infra).

---

## 8. Dual Dev/Prod with Shared Phoenix (keep — operational pattern)

Dev and prod run simultaneously on different ports:
- Dev: `:5175` (HTTP) + `:5173` (Vite)
- Prod: `:5275` (HTTPS) + `:5273` (Vite)

Phoenix tracing (`:6006`) is shared — first process launches it, second
connects. This means both environments share the same observability
backend.

**For SignalPy:** The kernel doesn't know about dev/prod. But the platform's
deployment model should support multiple kernel instances per host with
shared observability. The TracingProvider already handles this.

---

## 9. Settings Changelog (keep — audit pattern)

`settings.changes.jsonl` — append-only log of every config mutation:
```json
{"timestamp": "...", "key": "agent.model", "old": "sonnet", "new": "opus", "source": "ui"}
```

**For SignalPy:** The ConfigProvider should support a changelog hook.
When a writable layer is persisted, optionally append to a changelog
source. This is a ConfigSource concern — a `ChangelogSource` wrapper
that delegates to the real source and appends to a log.

---

## Summary: What Matters for the Redesign

| Pattern | Where it lives | Kernel needs to know? |
|---------|---------------|----------------------|
| App manifests | System layer | No — PluginLoader reads them |
| Git worktree workspaces | Deployment | No — paths passed in via config |
| Skills (markdown + frontmatter) | System layer | No — SkillLoader component |
| Case sources (YAML cron jobs) | System layer | No — IngestPipeline component |
| Tags (declarative labels) | Application | No |
| Test workspace fixtures | Test harness | No — kernel is per-test-instance |
| Docker Compose for MCP | Deployment | No — MCPBridge manages connections |
| Dual dev/prod | Deployment | No — multiple kernel instances |
| Settings changelog | ConfigProvider | Yes — small addition (changelog hook) |

**Only one kernel addition found:** a changelog hook on the ConfigProvider
for audit trail of config mutations. Everything else is system-layer or
deployment-level.
