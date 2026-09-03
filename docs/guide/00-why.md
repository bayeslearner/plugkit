---
title: "Why plugkit exists"
subtitle: "Unload the plugin. The route stays."
---

## The route that will not go away

Below is a component in a hypothetical framework that gives components a
lifecycle. The decorator names are invented for this example — iPOPO, Spring and
OSGi's Declarative Services all spell them differently — but the shape is the one
they share: declare what you provide, declare what you need, and write a pair of
lifecycle methods.

```python
@component("http-server")
@provides("IHttpServer")
class HttpServer:
    @lifecycle.activate
    def activate(self):
        self.app = FastAPI()
        self.app.add_route("/health", self.health)

    @lifecycle.deactivate
    def deactivate(self):
        self.app = None
```

A second component adds a route to that server:

```python
@component("admin-api")
@requires(server="IHttpServer")
class AdminApi:
    @lifecycle.activate
    def activate(self):
        self.server.app.add_route("/admin", self.handle)
```

Unload `AdminApi`. The `/admin` route stays.

Nothing recorded that `AdminApi` added it, so nothing removes it, and the route
now points at a method on a component that no longer exists. The author of
`AdminApi` can write a `deactivate` that removes it, and probably will, and will
get it right today and wrong in six months when someone adds a second route and
updates only one of the two lists.

**This is not a missing feature. It is a missing invariant.** Nothing *owns* the
undo of what a component did, so teardown is a promise each component makes
individually and the framework cannot check it.

Nor is it a defect in the invented framework: every real one named above behaves
this way. [Why not build on iPOPO](../design/why-not-ipopo.qmd) runs this exact
test against iPOPO, where the route survives `@Invalidate`.

## The one idea in plugkit

**Say the undo once, next to the thing it undoes, and let the lifetime own it.**

```python
def admin_api(ctx, config=None):
    ctx.server.add_route("/admin", handle)
    return lambda: ctx.server.remove_route("/admin")     # the undo

admin_api.inject = ["server"]
```

**You write that lambda.** `add_route` does not return a disposer; `Server` is
your class and no kernel can change what your methods return.

The *fiber* — the object representing this plugin's lifetime — holds it and calls
it on unload. It is a return value rather than a separate method, so there is
nothing to forget, and it sits on the line below the call it reverses rather than
in a `deactivate` twenty lines away.

Unload happens for more reasons than you would handle by hand:

- the `server` service was replaced by a different implementation
- a config value the plugin was built from changed
- a supervisor restarted the plugin after a failure
- the whole composition is shutting down
- the module was edited and hot-reloaded

`try/finally` covers one. A `deactivate` method covers the ones its author
thought of.

Several undos: `ctx.effect` collects each and starts them in reverse
registration order on unload.

```python
def admin_api(ctx, config=None):
    def route(path, handler):
        def install():
            ctx.server.add_route(path, handler)
            return lambda: ctx.server.remove_route(path)
        return install

    ctx.effect(route("/admin", admin_page))
    ctx.effect(route("/health", health_check))
```

Everything else follows from that:

**Hot reload is free.** Reloading a plugin is unload-then-apply. Unload is total,
so reload is clean. There is no reload mechanism to implement.

**Dependency-driven activation is free.** If a plugin can be stopped and started
cleanly, then "stop it when its database goes away, start it when a database
appears" is just calling those two operations at the right moment.

**Swapping an implementation is free.** Replace the database service and every
plugin holding a reference to the old one is rebuilt — not patched, rebuilt —
so nobody is left holding a stale object.

## Declarative versus imperative

| | lifecycle-only frameworks | plugkit |
|---|---|---|
| A component is | a class wearing decorators | a plain class |
| Registration happens | by the kernel, reading metadata | by the plugin, calling a method |
| Teardown happens | in a `deactivate` you write | by calling the disposers you returned |
| The unit of lifetime is | a component instance | a fiber |
| Vocabulary size | 12 decorators | 4 concepts |

A declarative framework asks you to *describe* your component and trusts itself
to do the right thing. plugkit asks you to *do* things and hands you the undo each
time, which is what lets it guarantee teardown instead of hoping for it.

## "But now my class imports the framework"

Correct, and that is the usual cost. A class covered in framework decorators
cannot be constructed in a test without the framework, and cannot be moved into
another project.

plugkit separates the component from its registration:

```python
# services/greeter.py — imports nothing. Not a framework file.
class Greeter:
    def __init__(self, database, prefix="hello"):
        self.database = database
        self.prefix = prefix

    def hello(self, name):
        return f"{self.prefix} {name}"
```

```python
# app.py — the only file that knows a kernel exists
from plugkit import provide
from services.greeter import Greeter

greeter = provide(Greeter, "greeter", needs=["database"], config={"prefix": "greeter.prefix"})
```

`Greeter` is testable as `Greeter(database=FakeDB(), prefix="hi")`. No kernel, no
container, no fixtures. That is [chapter 2](02-popo-components.qmd).

## Where this design comes from

The kernel is a port of **Cordis**, the plugin framework underneath
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) — DeepSeek's
open-source agent runtime, ~457,000 lines of TypeScript across ~219 packages,
every one of them a Cordis plugin.

plugkit matches Cordis's *semantics* — the same event names, the same dispatch
modes, the same lifetime rules — so dsh's documentation stays a working
specification for anything built here. Its 58-service catalogue, its five-stage
tool pipeline and its file-system policy events all describe a substrate that
means the same thing in this kernel.

`src/plugkit/VENDORED.md` records which port was vendored, why, and what was
changed in it.

## Is this not just a DI container?

For a fixed object graph, a container is better. `dependency-injector` wires the
quick-start example in fewer lines, synchronously, with no strings.

The difference appears when components come and go. Measured against
`dependency-injector` 4.x:

| | `dependency-injector` | plugkit |
|---|---|---|
| Request a service that does not exist | `AttributeError` | the dependent waits in `PENDING` until it appears |
| Swap an implementation | the existing dependent keeps the old object until `full_reset()` | dependents are rebuilt automatically; replaced objects' `close()` runs |
| Remove a service | no API for it | dependents stop, their `close()` runs, the name disappears |
| Undo what a component registered elsewhere | not tracked | the fiber owns it |

The first three rows are what a container does not attempt. The fourth is the
one described above, and is the reason the other three are possible.

## What to read next

- [First plugin](01-first-plugin.qmd) — inject, effects, and watching unload be total
- [Plain components](02-popo-components.qmd) — `provide()`, and a typed `ctx`
- [Tools](03-tools.qmd) — the five-stage pipeline, and why a guard has no "allow"
