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

## `ctx.reactive`

Reading config is not enough on its own — you also want to *act* when it changes.
Mount `ReactiveService` and register an effect:

```python
from plugkit import ReactiveService, plugin

await root.plugin(ReactiveService)


@plugin
def http(ctx: HttpDeps, config=None) -> None:
    ctx.reactive.effect(
        lambda: ctx.client.set_timeout(ctx.config.get("http.timeout", 30))
    )
```

The effect runs once immediately, then again on every change to anything it read.
It is registered against `http`'s fiber, so it stops when `http` unloads. You
write no teardown and no subscription bookkeeping.

**One Signal per key.** `set("http.timeout", 60)` wakes the readers of
`http.timeout` and nobody else. A single Signal over the whole config would wake
every config reader on every write, which is the behaviour this design avoids.

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
| the value is read on every use | `ctx.config.get(...)` inside the method — no effect needed |
| something must *happen* on change | `ctx.reactive.effect(...)` |
| the value was a constructor argument | `provide(..., config={...})` and let it rebuild |

## Next

[Composition from a file](06-composition-from-a-file.qmd) — an application as
YAML.
