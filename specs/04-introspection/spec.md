---
spec_id: 04-introspection
status: CLOSED
closed_as: SHIPPED
since: 2026-08-23
until: null
epic: meta-layer
features: [introspection]
supersedes: []
superseded_by: null
depends_on: [01-plugkit-kernel, 03-extension-points]
anchors: [kernel-architecture]
---

# The system describes itself

# 1 · Requirements

## Introduction

A running plugkit application knows a great deal about itself — which plugins are
mounted, what each provides, what each is waiting for, what it registered, and
why it failed. None of it is reachable.

This spec makes it reachable as plain data.

## Glossary

- **Snapshot** — a plain, JSON-serialisable description of the running system at
  one moment. No kernel objects, so it survives being logged, sent over a socket,
  or compared in a test.
- **Diagnostic** — a callable a plugin contributes to say something extra about
  itself that the kernel cannot know.

## The gap

Cordis has one line of runtime introspection, in `registry.ts:193`: the registry
"exposes map-like inspection over active plugin callbacks." That is the set of
mounted plugins and nothing about their state.

DSH does have an inspect catalogue, and it is **build-time codegen**:
`scripts/gen-cordis-inspect-catalog.ts` reads the TypeScript and emits
`api-catalog.ts`, a list of services and methods. It answers "what could be
called", never "what is running".

So neither answers any of these:

- which plugins are mounted, and in what state
- which one is `PENDING`, and on which missing service
- which failed, and with what error
- which fiber provides `database`
- what a given plugin registered

## Who reads it

Four consumers, and the fourth is the one that changes the design.

| Consumer | Wants |
|---|---|
| a developer at 3am | a readable tree |
| a test | a value to assert on |
| an operator's endpoint or CLI | something serialisable |
| **the agent itself** | a list of the services and tools it may use |

The fourth is specific to what this kernel gets built into. DSH generates its
catalogue precisely so the model knows what it can call — but a generated
catalogue cannot say which plugins are *loaded right now*, and in an application
whose plugin set changes per session, that is the question worth answering.

Plain data serves all four. A tree renderer serves the first and last.

## Requirements

**R1.** A snapshot can be taken of any context, including one whose author never
planned for it — no mounting, no configuration, no prior registration.

**R2.** The snapshot is JSON-serialisable: `json.dumps(describe(root))` succeeds.

**R3.** It reports, per fiber: uid, name, state, parent, what it provides, what
it injects, its registered effects, and its error if it failed.

**R4.** It reports which fiber provides each service name.

**R5.** A `PENDING` fiber reports **which** injected services are missing. That
is the single most useful fact in the snapshot, because a plugin silently not
running is this kernel's characteristic confusion.

**R6.** A plugin can contribute a diagnostic about itself, collected into the
snapshot.

**R7.** A human-readable tree can be rendered from a snapshot, without re-reading
the live system.

## Not in scope

**Metrics and tracing.** A snapshot is a point in time. Counters over time are a
different facility with different storage.

**Mutation.** Nothing here restarts, unloads, or reconfigures. Read-only, so it
is safe to call from a signal handler or an HTTP endpoint.

# 2 · Design

## A function, not a service

```python
from plugkit import describe

snapshot = describe(root)
```

`PointsService` and `ToolsService` are services because something registers into
them. Nothing registers into a snapshot; it is a pure read of state the kernel
already holds.

The deciding argument is R1. **You need to inspect a system that did not plan to
be inspected.** A debugging facility you must remember to mount is unavailable at
exactly the moment it is wanted — a production process that is misbehaving now.
`describe` is an ordinary function over a context, so it works on any context,
including one built by a test three releases ago.

## The snapshot

```python
{
  "fibers": [
    {
      "uid": 1,
      "name": "database",
      "state": "ACTIVE",
      "parent": None,
      "provides": ["database"],
      "injects": ["config"],
      "missing": [],
      "effects": ['ctx.provide("database")'],
      "error": None,
    },
    {
      "uid": 2,
      "name": "greeter",
      "state": "PENDING",
      "parent": None,
      "provides": [],
      "injects": ["database", "cache"],
      "missing": ["cache"],          # <- R5
      "effects": [],
      "error": None,
    },
  ],
  "services": {"database": 1},
  "points": {"http.routes": 2},      # only when PointsService is mounted
  "diagnostics": {"database": {"pool_size": 4}},
}
```

Every value is a string, number, list, dict or None. `state` is the enum's name,
not the enum. `parent` and the `services` values are uids, not fiber objects — a
snapshot must not keep the system alive by holding references to it.

## R5, the missing-service report

```python
missing = [name for name in fiber.inject if name not in reachable]
```

where `reachable` is read from `ctx.reflect.store`. A `PENDING` fiber is the
kernel behaving correctly — a plugin whose dependency is absent should wait — but
it is indistinguishable from a plugin that is not needed, and that
ambiguity is documented in the guide as intended behaviour. Naming the missing
service converts a silent wait into a readable one without changing it.

## R6, diagnostics as the second consumer of extension points

The kernel knows a fiber's state. It cannot know a connection pool's size.

```python
def database(ctx, config=None):
    pool = Pool()
    ctx.provide("database", pool)
    ctx.points.add("diagnostics", lambda: {"pool_size": pool.size}, key="database")

database.inject = ["points"]
```

`describe` collects the `diagnostics` point if `PointsService` is mounted, calls
each contribution, and files the result under its key. A contribution that raises
is reported as an error string rather than failing the snapshot — a broken
diagnostic must not break the debugging tool.

This is the shape DSH uses for `runtime-diagnostics/invariants`: each package
owns a companion that registers what it knows, and a central service collects
them. It is also the second consumer of spec 03, which said it would be the test
of whether that facility earns its 120 lines.

## R7, the tree

```python
from plugkit import describe, format_tree

print(format_tree(describe(root)))
```

```
plugkit — 4 fibers, 3 services
├─ [1] database          ACTIVE    provides database
├─ [2] greeter           PENDING   waiting for: cache
├─ [3] http              ACTIVE    provides server
└─ [4] admin             FAILED    ConnectionRefusedError: [Errno 61]
```

Takes a snapshot rather than a context, so a snapshot captured earlier, or read
from a log, renders the same.

# 3 · Tasks

- [x] **T1** — `plugkit/introspect.py`: `describe`, `format_tree`, `DIAGNOSTICS`
- [x] **T2** — all three exported
- [x] **T3** — `tests/test_introspect.py`, 23 tests across R1-R7
- [x] **T4** — chapter 07 section "Asking the system what it is doing", with both examples run by `test_guide_examples.py`
- [x] **T5** — `kernel-architecture.md`: why it is a function, the `fiber.store` trap, and diagnostics as the second consumer of `ctx.points`
