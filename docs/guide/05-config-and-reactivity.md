---
title: "Config and reactivity"
subtitle: "Values that change while the program runs"
---

Two mechanisms handle change, at deliberately different granularity.

| What changed | Mechanism | Why |
|---|---|---|
| a service **provider** was replaced | the fiber's epoch: unload and re-apply its dependents | the object identity changed, so a dependent holding the old one holds a stale reference |
| a config **value** | one Signal per key: re-run the effects that read it | reloading a plugin to observe a new timeout is a sledgehammer |
| a config value a **constructor argument** was built from | the binding restarts its own fiber | a constructor argument cannot be mutated, so a new object is the only correct answer |

## `ctx.config`

Mount `ConfigService` and read dotted keys:

```python
from plugkit import ConfigService

await root.plugin(ConfigService, {"yaml": "app.yml", "dict": {"http": {"timeout": 30}}})
root.config.get("http.timeout")            # 30
root.config.get("http.retries", 3)         # 3 — the default, applied at read time
root.config.require("http.host")           # KeyError if absent
```

Loading order is: YAML files in the order given, then `dict`. Then a layer above
all of them holds anything set at runtime.

```python
root.config.set("http.timeout", 60)
root.config.load_yaml("other.yml")         # does not revert the 60
```

`set()` writes to an override layer that every loader sits below, so reloading a
file cannot undo a runtime value.

### Loading from elsewhere

```python
root.config.load_yaml("prod.yml", required=True)
root.config.load_dict({"http": {"timeout": 5}})
root.config.load_env("db.password", "DATABASE_PASSWORD")
root.config.load_pydantic(Settings())
```

`load_env` and `load_pydantic` need the `config` extra
(`pip install "plugkit[config]"`), which brings in `dependency-injector`. Without
it those raise a named error and the rest still works.

### For tests

```python
with root.config.override({"http": {"timeout": 1}}):
    ...        # timeout is 1 in here, restored on exit
```

Readers are woken in both directions, so an effect under test sees the override
arrive and leave.

## `ctx.config.watch` — being told a value changed

Reading config is not enough on its own — you also want to *act* when it changes.
That is a subscription, and it needs nothing reactive:

```python
from plugkit import plugin


@plugin
def http(ctx: HttpDeps, config=None) -> None:
    client.set_timeout(ctx.config.get("http.timeout", 30))          # apply once
    ctx.config.watch("http.timeout", lambda next_, prev: client.set_timeout(next_))
```

`watch` returns a disposer registered against `http`'s fiber, so the watcher
stops when `http` unloads. You write no teardown.

Three rules worth knowing:

- **It does not fire on registration.** You have just read the value; applying it
  once yourself, as above, is clearer than a first call you did not ask for.
- **One key, one watcher.** `set("http.timeout", 60)` calls the watchers of
  `http.timeout` and nobody else.
- **An async callback is awaited, and one watcher never overlaps itself.** A
  change landing while the previous invocation is still running waits for it.

`ctx.points.watch` is the same method over a different source of change — the
contributions to a point — and behaves identically, except that its callback
takes no arguments because a point's value is a set you re-read.

**`ReactiveService` is not required for any of this.** Signals are what the
config service uses underneath; they are not something a caller has to adopt to
hear about a change.

## `ctx.reactive`

The reactive service is for a different job: a value *derived* from several
others, recomputed when any of them moves. Mount it and the derivation tracks its
own inputs.

```python
from plugkit import ReactiveService

await root.plugin(ReactiveService)
```

### `computed` for derived values

```python
base = ctx.reactive.computed(lambda: ctx.config.get("api.host") + "/v1")
base.get()        # cached; recomputes only when api.host changes
```

### Signals without the kernel

`plugkit.signals` imports nothing from the kernel and works in a plain script:

```python
from plugkit import Signal, Computed, Effect, batch

a, b = Signal(1), Signal(2)
total = Computed(lambda: a.get() + b.get())

watcher = Effect(lambda: print(total.get()))     # prints 3

with batch():
    a.set(10)
    b.set(20)                                    # prints 30 once, not twice

watcher.dispose()
```

`ctx.reactive` is the plugin that binds this library to fiber lifetime. The
library is the library.

## Rebuilding a component on a config change

`provide()` can read a constructor argument from config:

```python
from plugkit import provide

provide(Database, "database", config={"dsn": ("db.dsn", "sqlite://")})
```

With `ReactiveService` mounted, changing `db.dsn` disposes the old `Database`,
calls its `close()`, and constructs a new one. A constructor argument cannot be
changed after construction, so a new object is the only honest response.

Without `ReactiveService` the binding still works and does not rebuild. Reacting
to config is opt-in.

## Which to use

| | |
|---|---|
| the value is read on every use | `ctx.config.get(...)` inside the method — nothing else needed |
| something must *happen* on change | `ctx.config.watch(key, callback)` |
| the set of contributions to a point changed | `ctx.points.watch(point, callback)` |
| a value is derived from several others | `ctx.reactive.computed(...)` |
| the value was a constructor argument | `provide(..., config={...})` and let it rebuild |

Reach for `ctx.reactive.effect` when a body reads several sources and you would
rather not enumerate them. For "this key changed, do this", `watch` says it
plainly and costs no second concept.

## Next

[Composition from a file](06-composition-from-a-file.md) — an application as
YAML.
