---
title: "Testing"
subtitle: "What plain components buy you"
---

Most of what you write needs no kernel in its tests.

## Test the component directly

A component bound with `provide()` is a plain class with a normal constructor.
Construct it:

```python
def test_greeting():
    greeter = Greeter(database=FakeDB(), prefix="hi")
    assert greeter.hello("world") == "hi world"
```

No kernel, no container, no fixtures, no `async`. This is the whole reason
[chapter 2](02-popo-components.qmd) separates the component from its
registration. If a test of your business logic needs a kernel, something has
leaked.

## Test a plugin with a real context

A plugin function does need a context, because taking one is what makes it a
plugin. Build a root and mount it:

```python
import asyncio
from plugkit import Context, provide


async def test_admin_registers_its_route():
    root = Context()
    await root.plugin(provide(Server, "server"))
    await root.plugin(admin_api)

    assert "/admin" in root.server.routes
```

`pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml` lets you write
`async def test_...` with no decorator. plugkit's own suite uses that.

## Waiting correctly

`await root.plugin(p)` waits for **p's own load**. When mounting one plugin
unblocks another, await the second one's fiber:

```python
greeter = await root.plugin(provide(Greeter, "greeter", needs=["database"]))
await root.plugin(provide(Database, "database"))
await greeter                       # now the greeter has loaded
```

For a test touching several plugins, a settle helper is simpler than tracking
each fiber:

```python
async def settle(n=15):
    for _ in range(n):
        await asyncio.sleep(0)
```

`await fiber.dispose()` needs no settle — it waits for the unload it cascades to
dependents.

## Substituting a dependency

Services are found by name, so substitution is mounting a different class under
the same name:

```python
async def test_with_a_fake_database():
    root = Context()
    await root.plugin(provide(FakeDatabase, "database"))     # not the real one
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    assert root.greeter.hello("x") == "hello x"
```

Nothing needs a mocking library and nothing patches an import.

## Overriding config

```python
with root.config.override({"http": {"timeout": 1}}):
    assert root.config.get("http.timeout") == 1
assert root.config.get("http.timeout") == 30
```

Readers wake in both directions, so an effect under test sees the value arrive
and leave.

## Testing that teardown happened

Assert that unload removed everything:

```python
async def test_unload_is_total():
    root = Context()
    await root.plugin(provide(Server, "server"))
    fiber = await root.plugin(admin_api)
    assert "/admin" in root.server.routes

    await fiber.dispose()
    assert "/admin" not in root.server.routes
```

If you write a plugin that registers something, write this test for it. It is
the one that fails when someone later adds a second registration and forgets its
disposer.

## Observing a failure instead of raising

`await root.plugin(p)` re-raises whatever `p` raised. Catch it, then read the
state:

```python
from plugkit import FiberState

fiber = root.plugin(might_fail)
with pytest.raises(RuntimeError):
    await fiber
assert fiber.state is FiberState.FAILED
```

Do not reach for `await asyncio.sleep(0)` here. A failed load takes three ticks
to settle — `LOADING`, then `UNLOADING`, then `FAILED` — and counting ticks is a
test that breaks when the transition changes. Awaiting waits for the transition
itself.

## Testing a tool through the pipeline

`ctx.tools.execute` never raises. Assert on the result:

```python
from plugkit import PointsService, ToolsService


async def test_the_guard_denies():
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(register_delete_tool)
    await root.plugin(safety_guard)
    await settle()

    result = await root.tools.execute("delete", {"path": "/"})
    assert result.ok is False
    assert result.error["code"] == "DENIED"
```

## Asking the system what it is doing

When a test fails and the reason is not obvious, the usual cause is a plugin that
never ran. `describe` returns a plain snapshot, and `format_tree` renders it:

```python
from plugkit import describe, format_tree

print(format_tree(describe(root)))
```

```
plugkit — 6 fibers, 4 services
├─ [1] PointsService  ACTIVE    provides points
├─ [2] db             ACTIVE    provides database
├─ [3] ToolsService   ACTIVE    provides tools
├─ [4] greeter        ACTIVE    provides greeter
├─ [5] waiting        PENDING   waiting for: cache, queue
└─ [6] broken         FAILED    ConnectionRefusedError: [Errno 61] Connection refused
```

Line 5 is the one that saves time. A `PENDING` plugin is the kernel working
correctly — it waits for a service that is not there — but from outside it looks
identical to a plugin that never mounted. The snapshot names what it is waiting
for.

`describe` is a plain function, not a service. You do not mount it, and it works
on any context, including one written before this existed. That matters because
you reach for it when something is already wrong.

Assert on the snapshot directly when the shape is the thing under test:

```python
def by_name(snapshot, name):
    return next(f for f in snapshot["fibers"] if f["name"] == name)


async def test_the_greeter_waits_for_its_database():
    root = Context()
    await root.plugin(provide(Greeter, "greeter", needs=["database"]))
    await settle()

    entry = by_name(describe(root), "greeter")
    assert entry["state"] == "PENDING"
    assert entry["missing"] == ["database"]
```

Everything in a snapshot is a string, number, list or dict, so `json.dumps` works
on it and a snapshot captured in CI renders the same later.

### Telling it something it cannot know

The kernel knows a fiber's state. It does not know a connection pool's size.
Contribute a diagnostic:

```python
from plugkit import DIAGNOSTICS


def database(ctx, config=None):
    pool = Pool()
    ctx.provide("database", pool)
    ctx.points.add(DIAGNOSTICS, lambda: {"pool_size": pool.size}, key="database")

database.inject = ["points"]
```

```python
describe(root)["diagnostics"]["database"]      # {'pool_size': 4}
```

It needs `PointsService` mounted, it disappears when the plugin unloads, and a
diagnostic that raises is reported as an error string rather than breaking the
snapshot.

## What plugkit's own suite does

Four tests in plugkit's own suite are patterns you can copy:

| Test | Pattern |
|---|---|
| `test_component_needs_no_kernel_at_all` | asserts the components in the file import nothing from plugkit |
| `test_every_export_is_exercised_somewhere` | walks `__all__` and fails on a name no test mentions |
| `test_readme_examples.py` | runs every README example and asserts the prose's claims |
| `test_bare_install.py` | imports every module with the optional packages hidden |

The third exists because the README once documented an API that did not exist. If
your docs make a claim, run it.

The fourth exists because the development environment has every optional package
installed, so the suite cannot see an import that a plain `pip install` would
break. It hides `yaml` and `watchdog` from a subprocess and imports the package
as a bare user would.
