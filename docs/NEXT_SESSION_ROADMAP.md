# Next Session Roadmap

## Done (this session)

- **Renamed `reactpy` → `signalpy`** — Signal is the core primitive, avoids
  ReactPy namespace conflict. All imports, docs, pyproject.toml updated.
- **Merged ConfigProvider + ConfigAdmin** — single component provides both
  IConfig and IConfigAdmin. Config state stored in a Signal — `config.get()`
  is a reactive read, `config.set()` triggers all subscribers. No re-injection
  hack needed. Deleted configadmin.py.
- **Cleaned up v1 test code** — removed all references to `@requires_aggregate`,
  `@requires_map`, `@requires_best`, `@bind`, `@unbind`, `@platform_app`.
  All 253 tests pass using v2 decorators.

## Context

The kernel is just **Signal + Computed + Effect + component lifecycle**. Everything
else (config, logging, auth, bus, gateway) is components built on top.

Key insight: every backend developer faces the same problem as frontend — state changes
in one place need to propagate to all dependents. Signal handles this. No notify(), no
manual callbacks. `config.set("key", value)` IS the notification.

## Must do: rewrite docs around the core model

The Quarto docs should lead with: "the kernel is 3 reactive primitives + component
wiring. Everything else is components." Current docs still explain features as if
they're kernel mechanisms. They're not — they're components.

## 10 commercial software patterns to demo

Each pattern should be a runnable example + test + doc section.

### 1. Feature Flags (reactive config)
Frontend API call toggles a flag → all backend services react.
Shows: Signal-backed config, @effect, `config.set()` → propagation.
**Example 03 demos this now with Signal-backed config.**

### 2. Plugin/Extension System
Hot-add/remove components at runtime. list[C] aggregate injection.
Shows: hot_add, hot_remove, @effect re-runs, dynamic discovery.
**Example 04 already demos this.**

### 3. Database Failover (provider ranking + auto-switch)
Primary DB goes down → higher-ranked fallback takes over → consumers switch.
Shows: service.ranking, @prop, reactive auto-switch on hot_add.
**Example 06 already demos this.**

### 4. Secret Rotation (credential refresh)
Vault rotates API keys hourly → all services using those keys get new ones.
Shows: Signal in CredentialProvider, @effect in consumers.

### 5. A/B Testing (dynamic service selection)
Route 50% of traffic to search-v1, 50% to search-v2. Change split at runtime.
Shows: multiple providers of same contract, policy-based routing.

### 6. Multi-Tenant Isolation (L3 targeted)
Same component factory, different instances per tenant with different config.
Shows: kernel.instantiate() with properties, L3 targeted trait, bus routing by target.

### 7. Mini App / Extension Bundle
Install a "Slack integration" that brings 3 components (SlackNotifier,
SlackWebhookReceiver, SlackCommandHandler). One hot_add call adds all three.
Shows: component composition, multi-provides, cross-component bus calls.

### 8. Health Check / Circuit Breaker
Component monitors dependency health. When unhealthy, stops calling it.
Shows: @lifecycle.health, @effect watching provider state, @computed health status.

### 9. Audit Trail / Observability
Every bus invocation logged. Every @effect re-run traced. Runtime policy.
Shows: kernel.set_policy(audit=True), bus middleware pattern, trait-based auto-instrumentation.

### 10. Hot Code Update
Reload a module, restart affected components, preserve state.
Shows: hot_update (not yet implemented), state snapshot/restore.
**This one requires kernel changes — add hot_update method.**

## Architecture next steps

- Consider: should Bus be a component instead of a kernel primitive?
- Consider: Gateway + Transport — could the gateway BE the transport?
- PyPI publish planning (package name: `signalpy`)
- License decision
