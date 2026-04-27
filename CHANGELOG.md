# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/).

## [0.1.0] — 2026-04-27

First public release.

### Added

- **Reactive kernel** (~2,600 LOC, 9 files, zero required dependencies):
  - `Signal`, `Computed`, `Effect`, `batch` reactive primitives
  - `ReactiveRuntime` with Signal-backed dependency injection
  - `ServiceRegistry` with reference counting + hot-add/hot-remove
  - `Bus` (invoke / publish / subscribe) with auth enforcement
  - `LifecycleManager` with dependency-ordered activation and effect lifecycle
  - L0–L3 trait system, auto-computed from declared decorators
- **13 decorators**: `@component`, `@provides`, `@requires`, `@computed`,
  `@effect`, `@lifecycle.{activate,deactivate,health,snapshot,restore}`,
  `@runnable`, `@api`, `@subscribe`, `@kind`, `@skill`, `@prop`, `@exportable`
- **Platform providers**: config (Signal-backed, layered YAML + runtime updates),
  logging (structured JSON), credentials (per-component scoped), storage
  (local FS), auth (token-based), tracing (OpenTelemetry bridge), workspace,
  API gateway
- **Transport adapters**: REST (FastAPI), MCP, CLI (Click)
- **Hot code update**: `kernel.hot_update(NewClass)` with `@lifecycle.snapshot`/
  `@lifecycle.restore` hooks
- **L3 targeted instances**: same factory, multiple instances with different
  properties, routed by `target` parameter
- **298 tests** covering reactivity, lifecycle, registry, bus, gateway,
  FastAPI integration, hot-update, threading
- **Documentation site** at <https://bayeslearner.github.io/signalpy-kernel/>:
  7 tutorials, 7 concept docs (including line-by-line annotated reactive
  engine), 10 patterns (including reactive-intent-vs-default recipes),
  reference for traits / decorators / contracts / kernel API

### Notes

- Built with Claude's help. Reviews and corrections welcome via
  [Issues](https://github.com/bayeslearner/signalpy-kernel/issues).
