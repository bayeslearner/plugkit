---
title: "What plugkit does not replace"
subtitle: "An honest comparison against the four things it is closest to"
---

plugkit supersedes **none** of the three libraries below in feature count. It
replaces one thing in each: who owns a component's side effects. Everything else
they do, they still do better or exclusively.

## Pick by what you need

| If you need | Use |
|---|---|
| objects constructed and wired, with a fixed set decided at startup | `dependency-injector` |
| hook specifications that many plugins implement, pytest-style | `pluggy` |
| an OSGi platform: remote services, an HTTP service, a shell, LDAP service queries | `iPOPO` |
| plugins that load and unload at runtime, with their side effects undone | plugkit |

## `dependency-injector` — not superseded

It is a container. plugkit is not, and does not try to be.

| | `dependency-injector` | plugkit |
|---|---|---|
| Provider types | 12+: `Factory`, `Singleton`, `Resource`, `Callable`, `Coroutine`, `List`, `Dict`, `Configuration`, `Selector`, `Aggregate`, `Dependency`, `Object` | one construction policy, `provide()`, plus a sample factory policy |
| Wiring into functions | `@inject` with `Provide[...]` markers | none |
| Container overriding for tests | ✅ mature | `ctx.config.override()`, config only |
| Cython-compiled | ✅ | ❌ |
| Async resource init and shutdown | ✅ | via a plugin's disposer |
| Lifetime ownership of side effects | ❌ | ✅ |

For a fixed object graph it is fewer lines, synchronous, and better. plugkit's
`ConfigService` uses its `Configuration` provider rather than reimplementing it.

## `iPOPO` — not superseded, and larger

iPOPO is a Python OSGi implementation. It exceeds plugkit in most dimensions.

| | iPOPO | plugkit |
|---|---|---|
| Service lookup | **by class or LDAP filter** — `get_service_reference(clazz, ldap_filter)` | by name only |
| Dependency decorators | ~30, including `RequiresBest`, `RequiresMap`, `RequiresVarFilter`, `RequiresBroadcast`, `Temporal` | `inject`, a list of names |
| Remote services | ✅ RSA, discovery, transports | ❌ |
| HTTP service | ✅ | ❌ |
| Interactive shell | ✅ | ❌ |
| Service ranking and properties | ✅ | ❌ |
| Component lifecycle | ✅ `@Validate` / `@Invalidate` | ✅ fibers |
| asyncio-native | ❌ mostly synchronous | ✅ |
| Reversible effects | ❌ teardown is a method you write | ✅ the fiber owns it |
| Matches a documented reference implementation | ❌ | ✅ Cordis, DeepSeek Harness |

Its lifecycle model is the closest prior art in Python, which is why it is named
in the README as the exception to "Python does not have this". plugkit's
advantages over it are narrow: asyncio, the effect model, scoped service views,
and semantic compatibility with a large TypeScript codebase whose documentation
then applies here.

Those four are also the reason an agent runtime cannot be built on it.
[Why not build on iPOPO](why-not-ipopo.qmd) tests each one.

## `pluggy` — a different job

pluggy dispatches one call to many implementations. plugkit owns lifetimes. They
are not alternatives and a program can use both.

| | `pluggy` | plugkit |
|---|---|---|
| One call, many implementations | ✅ the entire design | partly, via `ctx.parallel` or a waterfall |
| Hook specifications with validation | ✅ | ❌ |
| `firstresult`, `hookwrapper`, `tryfirst` / `trylast` | ✅ | five dispatch modes, different shape |
| Discovery from entry points | ✅ `load_setuptools_entrypoints` | ❌ |
| Undoes a plugin's side effects | ❌ `unregister` removes hooks only | ✅ |

## Summary

plugkit is worth adopting for one property. A program that never needs a
component removed at runtime with its effects undone is better served by one of
the three above, and by a smaller commitment.
