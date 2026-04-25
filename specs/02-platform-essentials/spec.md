---
spec_id: 02-platform-essentials
status: SHIPPED
since: 2026-04-22
until: null
epic: kernel
features: [platform-essentials]
supersedes: []
superseded_by: null
depends_on: [01-code-review]
---

# Platform Essentials

## Context

The microkernel is meant to be source-embedded in other projects and used
by AI agents. For it to be trustworthy as a foundation, it needs: tests
that prove the contracts work, docs that match code, a CLAUDE.md so agents
can write correct components on first try, and the missing platform
components that the architecture promises (auth, workspace).

## Constraints

- Keep kernel/ under ~1000 lines (C9)
- Zero required deps for kernel/ (Axis 1)
- Tests must run without external services
- CLAUDE.md must be readable by an AI agent in one pass

## Tasks

### P1 — Must Do

- [x] 1.1 Fix all HTML doc inconsistencies (23 items from audit)
  - Fixed project layout, class names, boot sequence, SVG diagrams
  - Removed references to unimplemented adapters (gRPC, Remote, ClientGen)
  - Fixed IMetrics references in traits.html
- [x] 1.2 Write kernel tests (lifecycle, registry, bus, component model, runtime scoping)
  - 36 tests covering TraitRegistry, ServiceRegistry, Bus, LifecycleManager, Runtime, full integration
  - Tests: dependency ordering, circular deps, policy enforcement, params validation, shutdown order
- [x] 1.3 Create CLAUDE.md for AI agent onboarding
  - Constitution, component writing guide, contracts table, boot sequence, project layout
- [x] 1.4 Implement AuthProvider (providers/auth.py → IAuth)
  - Token-based auth with configurable tokens and role-based policies
  - Pluggable: swap by providing different IAuth component
- [x] 1.5 Implement WorkspaceProvider (providers/workspace.py → IWorkspace)
  - Workspace root, settings, path resolution
- [x] 1.6 Add proper __init__.py re-exports for all packages
  - providers/, adapters/, entries/ all have clean exports

### P2 — Should Do

- [x] 2.1 Write platform component tests (config, credentials, storage, auth, workspace, gateway)
  - 14 tests: dotted config, env overrides, scoped credentials, path traversal rejection,
    auth/authorize, workspace paths
- [x] 2.2 Write adapter tests (REST, MCP, CLI surface composition)
  - 3 tests: API surface composition, bus invocation through gateway, internal runnable exclusion

## Log

**2026-04-22** — Created after doc audit found 23 inconsistencies between
HTML docs and code. Platform components auth and workspace are declared
in architecture docs but missing from implementation.

**2026-04-22** — All tasks completed. 50 tests passing. Implemented AuthProvider
and WorkspaceProvider. Fixed HTML docs. Created CLAUDE.md. Added __init__.py
re-exports. Example still boots 10 components successfully.
