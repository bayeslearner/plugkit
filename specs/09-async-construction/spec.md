---
spec_id: 09-async-construction
status: CLOSED
closed_as: SHIPPED
since: 2026-08-24
until: null
epic: kernel
features: [init-hook]
supersedes: []
superseded_by: null
depends_on: [01-plugkit-kernel]
anchors: [kernel-architecture]
---

# Construction that needs an await

# 1 · Requirements

## Introduction

`provide()` called the factory and registered whatever came back. For an
`async def` factory that was the coroutine object: the service existed, every
dependent got it, the fiber went `ACTIVE`, `describe()` reported a healthy
system, and the only signal was a `RuntimeWarning` about a coroutine nobody
awaited.

That is the failure `docs/design/why-not-ipopo.md` opens with — quoted from
iPOPO, as reason #1 not to build on it — reproduced inside plugkit's own binding
layer, in a project whose README claims async lifecycles as its advantage over
iPOPO.

An earlier attempt fixed it by awaiting the factory. That was wrong, not because
it failed but because it invented a second mechanism for something the reference
does with one, and was reverted before this spec was written.

## Glossary

- **Init hook** — a method run after the service is registered, awaited by the
  fiber. Cordis spells it `[Service.init]`; the vendored kernel spells it
  `__cordis_init__`; a plain component names one with `init=`.

## Mental model & invariants

1. **Construction is synchronous; readiness is a second phase.** Cordis registers
   a service from its constructor (`vendor/cordis/src/service.ts`), and a
   JavaScript constructor cannot be async, so everything needing an await lives
   in the init hook the fiber runs afterwards (`fiber.ts:257`). DSH's own
   services — `host/webserver`, `session-persistence-sqlite`, `workspace`,
   `credentials-local` — are all written that way.
2. **The half-built window is closed by the fiber, not by ordering.** The service
   is registered *before* the hook runs, so what stops a dependent seeing it is
   that the fiber is still loading: `reflect._get_impl` will not resolve a name
   whose fiber is not `ACTIVE`. The vendored `test_pending_inject` is upstream's
   statement of the same property.
3. **A plain component stays plain.** The hook is an ordinary method with an
   ordinary name; the binding knows which one, the class does not.

**Invariants:**

- **I1** No code path registers a coroutine object as a service.
- **I2** A dependent never observes a component whose init hook has not settled.
- **I3** `init=` changes setup only. Teardown stays with `close=` and the
  `close`/`aclose`/`shutdown`/`dispose` detection.

## Requirements

### Requirement 1: an init hook on the binding

**User story:** As someone binding a client whose readiness needs an await
(`asyncpg`, `httpx`, an MQTT connection), I want a supported place to put it, so
that I do not have to write a plugin function by hand around a plain class.

1. WHEN `provide(..., init="connect")` is given, THE binding SHALL call that
   method after registering the service, and SHALL await it if it is awaitable.
2. WHILE the hook has not settled, THE service SHALL NOT resolve for a plugin
   injecting it.
3. WHEN the hook raises, THE fiber SHALL fail, so that supervision and
   `describe()` see it as any other load failure.
4. WHEN `init=` names a method the component does not have, THE binding SHALL
   raise, naming it — as `close=` already does.
5. A synchronous method SHALL be accepted as an init hook.

### Requirement 2: an async factory is refused, loudly

1. WHEN a factory returns an awaitable, THE binding SHALL raise a `TypeError`
   whose message names `init=`.
2. THE refused coroutine SHALL be closed, so the caller sees the error rather
   than a `RuntimeWarning` about a coroutine that was never awaited.

### Non-functional

- **NF1** No second construction path. `apply` stays one function with one
  ending for the synchronous case and one for the awaited-hook case.

## Out of scope

- **Collecting a disposer from the hook.** Upstream's `[Service.init]` may be an
  async generator yielding disposers, which the fiber collects. Here teardown has
  a home already (`close=`), and two answers to "what undoes this" would be worse
  than the small loss of fidelity.
- **Async context managers as factories.** Still refused, with the message that
  already names `close=`.

# 2 · Design

`provide()` gains `init: str | None`. `apply` refuses an awaitable factory
result, registers the component, then calls the named hook; if the hook returns
an awaitable, `apply` returns a coroutine that awaits it and yields the binding's
disposer. The fiber awaits an awaitable effect and collects what it returned, so
that keeps the fiber `LOADING` for exactly as long as the hook runs — no new
kernel machinery.

# 3 · Tasks

## Status marks
<!-- [ ] pending | [x] done | [!] BLOCKED: reason | [-] DROPPED: <reason> | [>] → <spec_id> -->

## Tasks

- [x] 1. The hook
  - [x] 1.1 `init=` runs after registration and is awaited
    - **Requirements**: 1.1, 1.2, 1.5
    - **Pillar**: Code
    - `_init_hook` and `_initialised` in `binding.py`.
  - [x] 1.2 Refuse an async factory, naming `init=`
    - **Requirements**: 2.1, 2.2
    - **Pillar**: Code
    - `_refuse_coroutine`, which closes the coroutine first.
  - [x] 1.3 Tests for the properties
    - **Depends**: 1.1
    - **Requirements**: 1.1-1.5, 2.1
    - **Pillar**: Test
    - Six in `test_binding.py`, including the half-built-window test built the
      way upstream builds `test_pending_inject`: mount without awaiting, hold the
      hook on an event, and assert the dependent has not run.

- [x] 2. The guide says where async construction goes
  - [x] 2.1 Chapter 2 gains the section
    - **Depends**: 1.1
    - **Pillar**: Teaching

## Notes

The reverted attempt is preserved in the history as `ba08bf6` and its revert
`0f4f5a6`. Worth keeping visible: the fix that "works" and the fix that matches
the reference were different fixes, and the difference was only visible after
reading `service.ts` and the DSH services that use the hook.

## Log

**2026-08-24** — Opened and closed SHIPPED.
