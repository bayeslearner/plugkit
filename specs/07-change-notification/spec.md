---
spec_id: 07-change-notification
status: CLOSED
closed_as: SHIPPED
since: 2026-08-24
until: null
epic: services
features: [config-watch, points-watch]
supersedes: []
superseded_by: null
depends_on: [03-extension-points]
anchors: [kernel-architecture]
---

# Change notification is a watcher, not an effect

# 1 · Requirements

## Introduction

An application built on plugkit reached for `ctx.reactive.effect` thirteen times.
Triaged, the thirteen come to nearly nothing: four subscribe to a key that never
existed, three want "the contributions to this point changed", about five are
read-caches whose invalidation is really a lifetime, and **one** wants "this
config value changed, do something". Every one of those needs is a subscription
with a callback and an undo. None of them needs dependency tracking.

`ctx.points.watch` — `on_change` at the time of writing — is already exactly that
subscription: a callback, a disposer owned by the calling plugin, and a `Signal`
hidden inside where the caller cannot see it. `ctx.config` has no counterpart. Its
surface is `get / require / peek / all / signal_for / set / load_* / override`,
and that single absence is the only reason an effect appears in application code
at all.

**The reference has already decided this.** DeepSeek Harness's settings service
exposes change notification as

```ts
watch(callback: (next: T, prev: T) => void | Promise<void>): () => void
```

(`packages/settings/settings/src/index.ts:115` in the harness tree) — a callback
subscription returning its own disposer, with nothing reactive on the surface.
Its consumers call their `onChange` hook once explicitly at attach and then
`watch(...)` for subsequent changes (~line 885). No signal, no effect, no
run-once-on-registration.

So this spec is not a design exercise. It is: match that.

## Glossary

- **Watcher** — a callback registered against a source of change, plus the
  disposer that unregisters it. Owned by the fiber that registered it.
- **Serialized invocation** — one watcher's callback never runs concurrently with
  itself; a second change waits for the first invocation to settle.

## Mental model & invariants

1. **Signals are an implementation detail of the services that use them.** A
   caller of `points.watch` today needs to know nothing about `Signal`, `Effect`,
   or whether `ReactiveService` is mounted. Config must be the same. The reactive
   library stays available for what it is for — derived values inside a plugin —
   and stops being the price of hearing about a change.
2. **The reference decides shape questions.** Where DSH has an API for this,
   plugkit's differs only where Python forces it to.

**Invariants:**

- **I1** A watcher registered by a plugin stops being called when that plugin
  unloads, with no teardown written by the caller.
- **I2** Writing key `a` wakes no watcher of key `b`.
- **I3** Nothing in a watcher's signature, docstring or guide text requires the
  caller to know that signals exist, and no watcher requires `ReactiveService`
  to be mounted.
- **I4** One watcher's callback never interleaves with itself, and a disposed
  watcher is not called — including for a change that lands while it is being
  disposed.

## Requirements

### Requirement 1: `ctx.config` has a watcher

**User story:** As a plugin author, I want to be told when a config value
changes, so that I do not have to mount a reactive service and learn its
dependency-tracking rules to hear about one key.

1. WHEN a plugin calls `ctx.config.watch(key, callback)`, THE service SHALL
   return a disposer, and SHALL register it against the calling plugin's fiber.
2. WHEN the value behind `key` changes, THE service SHALL call `callback` with
   the new and previous values, in that order, matching the reference's
   `(next, prev)`.
3. WHEN the watcher is registered, THE service SHALL NOT call `callback`. A
   caller that wants a first application does it explicitly, as the reference's
   consumers do.
4. WHEN `callback` is a coroutine function, THE service SHALL await it and SHALL
   NOT begin a second invocation of the same watcher before the first settles.
5. WHEN the registering plugin unloads, THE service SHALL stop calling
   `callback`, including for a change that lands during teardown.
6. THE service SHALL require no other service to be mounted for this to work.

### Requirement 2: the extension-point watcher matches it

**User story:** As a reader, I want one name for one idea, so that I do not have
to remember which service calls it what.

1. THE method currently named `points.on_change` SHALL be named `points.watch`.
   0.1.0 is unreleased, so there is no compatibility path to keep and none SHALL
   be added.
2. THE points watcher SHALL keep its nullary callback: a point's value is a set
   the caller re-reads, and there is no single "next" to hand it.
3. THE points watcher SHALL gain the serialization rule of R1.4.

### Requirement 3: the docs teach the watcher, not the effect

1. WHEN chapter 5 shows how to react to a config change, IT SHALL show
   `ctx.config.watch`, and SHALL show `ctx.reactive.effect` only for derived
   values within a plugin.
2. THE guide SHALL state that a watcher needs no `ReactiveService`.

### Non-functional

- **NF1** `prev` when the key was previously unset. Config stores a `MISSING`
  sentinel to keep "unset" distinct from "set to `None`"; a watcher must not leak
  that sentinel to a caller. Decide during design: most likely both values pass
  through the same default resolution `get()` applies.
- **NF2** Every renamed call site — `points.on_change` appears in the guide, the
  API reference, the CHANGELOG and the tests — moves with the rename, and
  `test_docs_consistency.py` is what catches a missed one.

## Out of scope

- **Async construction.** `provide()` registering an unawaited coroutine is a
  separate finding with a separate upstream answer (`[Service.init]`, which the
  vendored kernel already implements as `__cordis_init__`). It gets its own spec.
- **Deleting the reactive service.** Nothing here argues signals should go. They
  stop being the *interface* for change notification; they remain the mechanism
  underneath, and remain available for derived values.
- **Watching a key prefix or glob.** One key per watcher, as the reference does
  one setting per watcher.

# 2 · Design

To be written at `/spec-plan 07-change-notification refine`. The expected shape:
`ConfigService.watch` built on the per-key `Signal` it already holds, wrapped the
way `points.on_change` wraps its own — an `Effect` created inside `ctx.effect`,
so the disposer belongs to the caller's fiber — plus a per-watcher invocation
chain for R1.4, which `points.on_change` does not have today and which the
reference implements as a promise tail per watcher
(`packages/settings/settings/src/index.ts` ~748-768).

# 3 · Tasks

## Status marks
<!-- [ ] pending | [x] done | [!] BLOCKED: reason | [-] DROPPED: <reason> | [>] → <spec_id> -->

## Tasks

- [x] 1. `ctx.config.watch`
  - [x] 1.1 The method, on the per-key signal, owned by the caller's fiber
    - **Requirements**: 1.1, 1.2, 1.3, 1.6
    - **Pillar**: Code
    - `ConfigService.watch(key, callback, default=None)`. Built the way
      `points.watch` is: an `Effect` created inside `self.ctx.effect(...)`, so
      the disposer belongs to the caller's fiber. NF1 resolved as the design
      note expected — both values pass through the same default resolution
      `get()` applies, so `MISSING` never reaches a caller.
  - [x] 1.2 Serialized invocation, and no call after dispose
    - **Depends**: 1.1
    - **Requirements**: 1.4, 1.5
    - **Pillar**: Code
    - `services/_watch.py`, shared by both services. A task chain per watcher
      (upstream chains a promise tail), an `active` flag re-checked *after* the
      await, and a raising callback logged rather than propagated into a config
      write.
  - [x] 1.3 Tests asserting the properties, not the surface
    - **Depends**: 1.1
    - **Requirements**: 1.1-1.6
    - **Pillar**: Test
    - Eight in `test_config_graft.py`: next/prev, no fire on registration,
      per-key, no `ReactiveService` needed, dies with its plugin, the default
      for an absent key, serialization under three rapid writes, and a raising
      watcher neither stopping the write nor stopping itself.

- [x] 2. `points.on_change` → `points.watch`
  - [x] 2.1 Rename, with the serialization rule applied
    - **Requirements**: 2.1, 2.2, 2.3
    - **Pillar**: Code
    - Callback stays nullary (R2.2); now goes through the same `Watcher`.
  - [x] 2.2 Every call site and doc mention moves with it
    - **Depends**: 2.1
    - **Requirements**: 2.1
    - **Pillar**: Documentation, Test
    - Guide 04, CHANGELOG, `test_points.py`, `test_guide_examples.py`. No
      compatibility alias, per R2.1.

- [x] 3. The guide teaches watching
  - [x] 3.1 Chapter 5 leads with `watch`; effects are for derived values
    - **Depends**: 1.1
    - **Requirements**: 3.1, 3.2
    - **Pillar**: Teaching
    - Chapter 5 opens change notification with `ctx.config.watch`, states the
      three rules and that `ReactiveService` is not required, and `ctx.reactive`
      now introduces itself as the derived-value tool. The "which to use" table
      routes each case. The example is executed by
      `test_the_watch_example_applies_once_then_follows`.

## Notes

The triage that produced this spec was done against an application repository,
not this one, so the thirteen sites and their disposition are recorded here as
the motivation rather than as a checkable claim.

## Log

**2026-08-24** — Opened. Shape taken from `settings.watch` in the harness tree
rather than derived here.

**2026-08-24** — Implemented and closed SHIPPED. `ctx.config.watch` and
`ctx.points.watch`, sharing `services/_watch.py` for the serialization and
liveness rules. Ten new tests. One deviation from the reference, forced by the
language: upstream's watcher is registered on a settings *entry* object, while
config here is key-addressed, so the key is the first argument.

**2026-08-24** — Correction to a premise in R2.1 and in the introduction: 0.1.0
was *not* unreleased. It was tagged at `8d5ccbc` earlier the same day, before
this spec was written, so `points.on_change` had shipped. The decision does not
change — no alias — but its justification does: this is a breaking change
against a tagged version, carried by the 0.2.0 bump rather than by being free.
