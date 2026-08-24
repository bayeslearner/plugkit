---
title: "Plain components"
subtitle: "provide(), and a ctx your type checker understands"
---

## The problem with framework decorators

Most Python DI frameworks ask you to mark up your classes:

```python
@ComponentFactory("greeter-factory")           # iPOPO
@Provides("greeter.service")
@Requires("_db", "database.service")
class Greeter: ...
```

```python
@inject                                        # dependency-injector
def __init__(self, db: Database = Provide[Container.db]): ...
```

Both work. Both cost you the same three things:

1. **Your class now imports the framework.** It cannot be moved to another
   project without bringing the framework along.
2. **You cannot construct it in a test.** `Greeter()` needs a kernel, a
   container, or a fixture that stands one up.
3. **The wiring is scattered.** To learn how the app fits together you read
   every class, because each one declares its own part.

plugkit's answer is to move the wiring out of the class.

## `provide()`

The component is a plain class with a normal constructor:

```python
# services/greeter.py — this file imports nothing from plugkit
class Greeter:
    def __init__(self, database, prefix="hello"):
        self.database = database
        self.prefix = prefix

    def hello(self, name):
        return f"{self.prefix} {name}"

    def close(self):
        self.database = None
```

One line elsewhere says how to wire it:

```python
# app.py — the only file that knows a kernel exists
from plugkit import provide
from services.greeter import Greeter

greeter = provide(Greeter, "greeter", needs=["database"])
await root.plugin(greeter)
```

`provide()` returns a plugin. When its dependencies exist it constructs
`Greeter(database=<the database service>)`, registers the result under
`greeter`, and arranges for `close()` on unload.

And the test needs nothing:

```python
def test_greeting():
    greeter = Greeter(database=FakeDB(), prefix="hi")
    assert greeter.hello("world") == "hi world"
```

No kernel. No fixture. No `async`.

## Naming the service — and why you must

The second argument to `provide()` is the name the result is registered under:

```python
provide(Greeter, "greeter")
```

The second argument is required. There is no default, and that is deliberate,
because the alternative caused exactly the confusion you might be having now.

**Nothing here is type-based.** `needs=["database"]` does not mean "find me a
`Database`". It means "the service registered under the string `database`". The
class is never consulted. The link between provider and consumer is a string,
written in two places:

```python
provide(PostgresDatabase, "database")            # registers under "database"
provide(Greeter,  "greeter", needs=["database"]) # asks for "database"
```

Deriving the name from the class would break silently. Rename `Database` to
`PostgresDatabase` and the service becomes `postgres_database`, so `Greeter`
waits forever for a `database` that never arrives — with **no error**, because a
plugin waiting on an absent service is indistinguishable from one that is not
needed yet.

Naming it yourself also pushes you toward the *role* rather than the
implementation, which is what makes swapping possible:

```python
provide(PostgresDatabase, "database")     # today
provide(SqliteDatabase,   "database")     # tomorrow — no dependent changes
```

### Two spellings for the same lookup

Attribute access and subscript access resolve identically:

```python
root.database        # attribute
root["database"]     # subscript — identical lookup, same rules
"database" in root   # can this fiber read it?
```

The subscript form is the honest one: it puts the string on screen, so nobody
reads it as type-based injection. It is also the only way to reach a service
whose name is not a valid Python identifier — `root["db.primary"]`.

### And `needs` is strings too

`needs` maps a constructor keyword to a service name:

```python
needs=["database"]              # kwarg `database` gets service `database`
needs={"db": "database"}        # kwarg `db`       gets service `database`
```

The list form is shorthand for "the constructor parameter and the service happen
to share a name". When they differ, use the dict.

## Saying what it needs

Three forms, increasingly good:

```python
provide(Greeter, "greeter", needs=["database"])                  # kwarg name == service name
provide(Greeter, "greeter", needs={"db": "database"})            # kwarg `db` gets `ctx.database`
provide(Greeter, "greeter", needs=GreeterDeps)                   # a Protocol
```

The third removes a duplication the other two have.

```python
from typing import Protocol

class GreeterDeps(Protocol):
    database: Database
    cache: Cache

provide(Greeter, "greeter", needs=GreeterDeps)     # inject == ["cache", "database"]
```

`typing.get_protocol_members` (Python 3.13+) reads the member names straight off
the Protocol. So the Protocol is simultaneously:

- what the **kernel** injects at runtime, and
- what your **type checker** validates the constructor against.

One declaration, not a typed one and a string list that drift apart.

## Config

Constructor arguments can come from config instead of from services:

```python
provide(
    Database,
    "database",
    config={"dsn": ("db.dsn", "sqlite://")},   # (key, default)
)
```

That reads `ctx.config.get("db.dsn", "sqlite://")` and passes it as `dsn=`.

**What happens when the config changes?** A constructor argument cannot be
changed after construction, so the honest answer is a new object — and that is
what happens, provided `ReactiveService` is mounted:

```python
from plugkit import ConfigService, ReactiveService

await root.plugin(ReactiveService)
await root.plugin(ConfigService, {"dict": {"db": {"dsn": "one://"}}})
await root.plugin(provide(Database, "database", config={"dsn": ("db.dsn", "sqlite://")}))

first = root.database                 # dsn == "one://"
root.config.set("db.dsn", "two://")

root.database is first                # False — rebuilt
first.closed                          # True  — the old one was closed
```

Without `ReactiveService` mounted the binding still works; it just doesn't
rebuild. Reacting to config is opt-in, and a minimal composition still boots.

## Teardown

`provide()` looks for `close`, `aclose`, `shutdown`, or `dispose`, or the context
manager protocol, and calls whichever it finds on unload:

```python
provide(Server, "server", close="stop")     # name a different method
provide(Cache, "cache", close=False)       # nothing to tear down
```

## A `ctx` your type checker understands

Cordis resolves by name only — `reflect.get(name: string)` is the whole lookup.
Its code looks type-aware because TypeScript merges declarations into the shared
`Context` interface at compile time (`interface Context { tools: ToolRuntime }`),
while the runtime still reads `store['tools']`.

Python has no declaration merging. plugkit makes the same split with a Protocol
on your own parameter: typed at check time, resolved by name at run time.

`Context.__getattr__` returns `Any`, because which services exist depends on
which plugins are mounted, and no type system knows that at import time.

You do not need the *Context* typed. You need **your plugin's parameter** typed,
and that is an ordinary Protocol:

```python
from typing import Any, Protocol
from plugkit import plugin

class Tools(Protocol):
    def register(self, tool: Any) -> Any: ...

class ReportDeps(Protocol):
    database: Database
    tools: Tools
    def on(self, event: str, listener: Any) -> Any: ...   # a ctx method you call

@plugin
def report(ctx: ReportDeps, config=None) -> None:
    rows = ctx.database.query("SELECT 1")   # typed: list[dict]
    ctx.tools.register(...)
    ctx.databse                             # pyright: Cannot access attribute
```

Verified against pyright, not asserted:

| approach | result |
|---|---|
| pass a raw `Context` where a Protocol is wanted | **rejected** — `__getattr__ -> Any` does not satisfy protocol members |
| annotate your own parameter with a Protocol | **works** — completion, typo detection |
| `get[T](token: type[T]) -> T` token lookup | works |
| `cast(MyDeps, ctx)` once at the top | works |

`src/plugkit/tests/test_typing.py` runs pyright over these and fails if any
stops being true.

### Why `@plugin` and not `fn.inject = [...]`

Cordis's own idiom is attribute assignment on a function. It runs fine and
pyright rejects it — you cannot assign arbitrary attributes to a function. So
`@plugin` returns a mapping instead, and with no explicit `inject` it derives the
list from the Protocol on the first parameter, dropping the context's own methods
(`on`, `effect`, `emit`, …) so they don't become phantom service dependencies.

```python
@plugin                       # inject derived from ReportDeps
def report(ctx: ReportDeps, config=None): ...

@plugin(inject=["database"])  # or be explicit
def report(ctx, config=None): ...
```

## Other frameworks do this differently, and it is a real trade-off

Not every Python DI framework uses strings.

- **`injector`** and **`lagom`** resolve by **type annotation**:
  `def __init__(self, db: Database)` and the container finds a `Database`.
- **`dependency-injector`** uses a marker referencing a container attribute:
  `db: Database = Provide[Container.db]`. The annotation is for your type
  checker; the resolution is the marker.

Type-based resolution has a genuine advantage plugkit does not have: **it is
rename-safe and IDE-navigable.** Rename the class and your editor updates every
reference. There is no string to typo.

The cost is coupling. Asking for a `Database` by type means **importing**
`Database`, so the consumer imports the provider. A plugin system needs the
opposite: `admin_api` asks for `server` without knowing which package supplies
it, or whether one is loaded at all. Late binding needs a late-bound key, and a
string is one.

There is a second reason here specifically: Cordis is string-keyed, and matching
it is what keeps dsh's documentation valid for this kernel. A type-based layer
could be built on top — `provide()` is one small module, and
[a rival policy](#where-the-kernelplugin-line-falls) is an ordinary plugin — but
it would be a second lookup mechanism, and two ways to find a service is worse
than one imperfect way.

What plugkit does to soften the cost:

| The problem with strings | What helps |
|---|---|
| typos are silent — the plugin just never activates | declare `needs` as a Protocol; the same names type-check your constructor |
| you cannot see what is available | `"database" in root`, and `root.registry` lists the mounted plugins |
| it reads like type-based injection | `root["database"]`, which cannot be misread |
| renaming a class silently rewires | it does not — the service name is explicit and separate from the class name |

## Where the kernel/plugin line falls

A fair question at this point: if components are plain and `provide()` is just a
function, **is dependency injection itself a plugin?**

Mostly yes. The split falls in a specific place.

**Below the line — the kernel, and it cannot be a plugin.** Resolution (`ctx.database`
finding the object) and lifetime (when your plugin runs, and unwinding it when a
dependency leaves). You cannot mount a plugin that provides the ability to mount
plugins.

**Above the line — everything usually meant by "a DI framework".** Construction
strategy, scopes, per-object lifetimes, config binding, the wiring format. All
policy, and policy is a plugin.

`provide()` is one such policy, and it reaches the kernel through a small,
countable surface:

| It calls | To |
|---|---|
| `ctx.provide(name, obj)` | register the constructed component |
| `ctx.inject(names, child)` | watch config *only if* `ReactiveService` is mounted |
| `ctx.fiber.restart()` | rebuild when a constructor argument's config key changes |
| returning a disposer | hand teardown to the fiber |

That is the whole contract. Anything that can call those can be a rival.
`plugkit/examples/alternative_binding.py` is one:
`provide_factory()` registers a *maker* instead of an instance, so every caller
gets its own object — `Factory` rather than `Singleton`, in dependency-injector's
vocabulary — and it needed no kernel change to exist.

```python
await root.plugin(provide_factory(RequestScope, "request"))
a, b = root.request(), root.request()      # different objects
```

Instances it made are still owned by its fiber, so a per-call policy loses none
of the teardown guarantee a per-instance one has.

## Next

[Tools](03-tools.qmd) — the five-stage pipeline, and why a guard has no "allow".
