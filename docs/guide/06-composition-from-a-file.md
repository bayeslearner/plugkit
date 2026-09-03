---
title: "Composition from a file"
subtitle: "An application as a YAML list"
---

Everything so far mounted plugins in Python. For an application whose shape
differs per deployment, the list of plugins is itself configuration.

## A plugin as a module

A module is a plugin when it has `apply(ctx, config)`. It may also carry `name`
and `inject`:

```python
# myapp/database.py
name = "database"


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn


def apply(ctx, config=None):
    ctx.provide("database", Database((config or {}).get("dsn", "sqlite://")))
```

```python
# myapp/greeter.py
name = "greeter"
inject = ["database"]


def apply(ctx, config=None):
    prefix = (config or {}).get("prefix", "hello")
    ctx.provide("greeter", lambda who: f"{prefix} {who} via {ctx.database.dsn}")
```

## The file

```yaml
# app.yml
- id: db
  name: myapp.database
  config:
    dsn: postgres://prod

- id: greet
  name: myapp.greeter
  config:
    prefix: hi
```

`name` is an importable module path. `id` labels the entry. `config` becomes the
plugin's second argument.

## Loading it

```python
from plugkit import Context, load_app

root = Context()
await load_app(root, "app.yml")

root.greeter("world")        # 'hi world via postgres://prod'
```

## Order in the file means nothing

Put the greeter first, before the database it needs:

```yaml
- id: greet
  name: myapp.greeter
- id: db
  name: myapp.database
```

The result is identical. `inject` decides activation, so the greeter waits until
the database exists. There is no boot sequence to get right, which is the point
of listing plugins rather than calling them in order.

## A missing dependency is not an error

Delete the database entry entirely:

```yaml
- id: greet
  name: myapp.greeter
```

Nothing raises. `greeter` stays `PENDING` and is not available. That is
the correct behaviour for an optional feature whose backing service is not
deployed in this environment.

```python
await load_app(root, "app.yml")
"greeter" in root      # False
```

## Naming an attribute

A module holding several plugins can name one directly:

```yaml
- name: myapp.plugins:database
```

That imports `myapp.plugins` and takes `database` off it.

## Substituting a plugin

`FileLoader.register` resolves a name without importing:

```python
from plugkit import FileLoader

await root.plugin(FileLoader)
root.loader.register("myapp.database", fake_database_module)
await root.loader.create({"name": "myapp.database"})
```

::: {.callout-warning}
## This does not reach entries inside an included file

`register()` applies to entries created through `loader.create()`. Entries listed
in a YAML file resolve through the include's own tree and fall through to the
import path, so registering `myapp.database` will not substitute the `myapp.database`
line in `app.yml`.

To substitute a plugin the file names, give a module that name and put it on the
import path ahead of the real one.
:::

## What this is under the hood

`load_app` is three steps:

```python
await ctx.plugin(FileLoader)          # provides ctx.loader
await settle()                        # let its child fibers register listeners
loader.register("__include__", Include)
await loader.create({"name": "__include__", "config": {"path": path}})
```

The `settle()` is required. The loader mounts child fibers that register
`loader/entry-init`, and creating an entry before those exist raises
`KeyError('_isolate')` — entry-init is what puts the isolate map on the entry's
context. `load_app` handles it; the note is here because a hand-rolled version
hits it.

## Next

[Testing](07-testing.qmd) — what components being plain objects buys you.
