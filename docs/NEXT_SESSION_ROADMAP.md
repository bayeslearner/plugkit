# Next Session Roadmap

## Context from this conversation

The kernel is just **Signal + Computed + Effect + component lifecycle**. Everything
else (config, logging, auth, bus, gateway) is components built on top.

Key insight: every backend developer faces the same problem as frontend — state changes
in one place need to propagate to all dependents. Signal handles this. No notify(), no
manual callbacks. `self._state.value = new_val` IS the notification.

## Must do: merge ConfigProvider + ConfigAdmin

They're the same thing. A config component that:
- Loads initial config (YAML, env vars) at boot
- Accepts runtime updates (push new config via runnable)
- Stores state in Signal so changes propagate reactively to all consumers
- Persists to JSON file

## Must do: rewrite docs around the core model

The docs should lead with: "the kernel is 3 reactive primitives + component wiring.
Everything else is components." Current docs still explain features as if they're
kernel mechanisms. They're not — they're components.

## 10 commercial software patterns to demo

Each pattern should be a runnable example + test + doc section.

### 1. Feature Flags (reactive config)
Frontend API call toggles a flag → all backend services react.
Shows: Signal-backed state, @effect, API → bus → propagation.
**Example 03 already demos this. Expand with the feature flag provider.**

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

## Architecture simplification

Consider merging these v1 leftovers:
- ConfigProvider + ConfigAdmin → one component with Signal state
- Gateway + Transport → could the gateway BE the transport?
- Consider: should Bus be a component instead of a kernel primitive?

## Open questions for user

- Package name: `reactpy` might conflict with ReactPy (the Python→React UI framework).
  Consider: `reactpy-kernel`, `reaktor`, `signalbus`, or similar.
- Should we publish to PyPI?
- License?
