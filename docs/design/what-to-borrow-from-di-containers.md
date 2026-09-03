---
title: "What to borrow from a DI container"
subtitle: "Which dependency-injector ideas belong here as plugins, which already exist under another name, and which would undo the design"
---

[What plugkit does not replace](what-it-does-not-replace.md) counts the gap
honestly: `dependency-injector` ships twelve-plus provider types, plugkit ships
one construction policy. That comparison stops at the count. This page asks the
next question — of those twelve, which are *missing capability* and which are
merely *a different spelling of something here* — because the answer decides what
is worth building and what would be a regression dressed as a feature.

The test applied throughout: a borrowed idea has to survive the invariant. If a
feature hands out something whose undo nobody holds, it does not belong here at
any price.

## Already here, under another name

| `dependency-injector` | plugkit | note |
|---|---|---|
| `Singleton` | `provide(Thing, "thing")` | one instance per fiber, rebuilt when the fiber reloads |
| `Configuration` | `ConfigService` | which uses that very provider under the `config` extra rather than reimplementing it |
| `Resource` (sync generator / context manager) | `provide()` enters it | `_setup_teardown` enters the manager and registers what `__enter__` returned |
| `List` / `Dict` / `Aggregate` | `ctx.points` | and better: each contribution is removed when its contributor unloads |
| `Object` | `extra={...}` on `provide()` | literal constructor arguments, passed through |
| Container overriding for tests | mount a different provider under the same name; `ctx.isolate` for a subtree | the composition root is the override point |
| `container.check_dependencies()` | `describe()` / `format_tree()` | shows each fiber's state and what it is waiting for, at runtime rather than at wiring time |
| Declarative container class | `load_app()` and the YAML loader | the application as a list of plugins |

Nothing on this list is worth building twice.

## Not a borrow: async construction

There is a real problem here, and `dependency-injector` is the wrong place to
look for its answer. The problem:

```python
async def connect(dsn="..."):        # asyncpg, httpx, aiobotocore — the normal shape
    ...

await root.plugin(provide(connect, "db"))
root.db          # <coroutine object connect> — and a RuntimeWarning, nothing else
```

The service registers, the fiber goes `ACTIVE`, `describe()` reports a healthy
system, and the only signal is a warning about a coroutine nobody awaited.

But the reference has an answer already, and it is not `Coroutine` providers. In
Cordis a service registers itself **synchronously**, from its constructor
(`vendor/cordis/src/service.ts`) — a JavaScript constructor cannot be async, so
the case above is unreachable there — and anything needing an await goes in the
init hook, which the fiber awaits (`fiber.ts:257`). Dependents are not released
until it settles; the vendored `test_pending_inject` is that guarantee. DSH's own
services do exactly this: `host/webserver`, `session-persistence-sqlite`,
`workspace` and `credentials-local` all define `async [Service.init]()`.

plugkit already implements that hook as `__cordis_init__`, for `Service` classes.
What is unresolved is how a *plain* component bound with `provide()` reaches it,
since a plain class has no kernel symbol to hang it on. That is a design question
with an upstream shape to match, not a feature to import from a container, and it
belongs in a spec rather than on this page.

## Worth shipping, in this order

### 1. `Selector` — the implementation named by a config value

```python
provide_selected("database", key="db.kind", options={"pg": PostgresDatabase, "sqlite": SqliteDatabase})
```

Today the `if` goes in your composition root, where it is evaluated once at boot
and a config change does not move it. As a plugin this is genuinely better here
than in a container: the losing implementation is not just unused, it is
*unloaded*, so the routes, subscriptions and connections it registered are undone
— and the machinery to do it is the config-watch restart `provide()` already
performs. A container cannot make that offer, because it has no lifetime to end.

### 2. `Factory` — promoted from example to shipped

`plugkit.examples.alternative_binding.provide_factory` already exists and already
has tests: it registers a maker instead of an instance, and — the part
`dependency-injector`'s `Factory` has no answer for — the fiber closes every
instance the maker handed out
(`test_a_factory_policy_still_unwinds_with_its_fiber`). It is an example rather
than a supported name. Since it is the strongest demonstration that a rival
construction policy needs no kernel change, leaving it in `examples/` understates
it.

## Not to borrow

**Wiring markers** — `@inject` with `Provide[...]` in call signatures. This is the
one to refuse loudly. It puts the framework back inside the component, which
[Plain components](../guide/02-popo-components.md) exists to prevent, and it
does not merely offend taste: a dependency resolved at call time has no fiber to
attribute its registrations to and no way to be invalidated when its provider is
replaced. It would be an injection path the kernel cannot see, and every
guarantee here rests on the kernel seeing them all.

**Cython-compiled providers, container specialization.** Performance engineering
for a container's hot path. There is no idea in it to take.

## Left open on purpose

**Per-request scope.** `ctx.isolate` gives a subtree its own view of a service,
which is a different axis from "each request gets its own objects". A handler
that needs the latter can mount a short-lived fiber and dispose it, which is
correct and costs a fiber per request. Whether that cost matters is an empirical
question, and no application here has asked it yet. Building a scope mechanism
before one does would be guessing at the shape.

## Related

- [What plugkit does not replace](what-it-does-not-replace.md) — the feature-by-feature count
- [Plain components](../guide/02-popo-components.md) — `provide()` and the rebinding rule
- [Config and reactivity](../guide/05-config-and-reactivity.md) — the restart path a selector would reuse
