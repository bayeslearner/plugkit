---
title: "Extension points and your own services"
subtitle: "Many plugins filling one role"
---

[Chapter 2](02-popo-components.qmd) covered `provide()`, which registers a plain
class under a name — **one** provider, **one** name. This chapter covers the
other direction: **many** plugins contributing to **one** role.

## Three kinds of service

| Kind | Example | Use |
|---|---|---|
| A component others **call** | a database, an HTTP client, a greeter | `provide()` — a plain class |
| A collection others **add to** | routes, middleware, health checks, codecs | `ctx.points` |
| A collection with **behaviour** | a tool registry that also runs a permission pipeline | `Service` subclass |

Most of the second row does not need a class at all.

## `ctx.points` — the extension point

Mount `PointsService`, then any plugin can contribute to any named role:

```python
from plugkit import PointsService

await root.plugin(PointsService)
```

```python
def admin(ctx, config=None):
    ctx.points.add("http.routes", admin_page, key="/admin", order=10)

admin.inject = ["points"]
```

A **point** is a named role. A **contribution** is one plugin's entry in it. The
point is never declared — it exists once something is contributed to it.

`add` returns a disposer, and it is registered against **`admin`'s** fiber. Unload
`admin` and the route goes. You write no teardown.

The consumer reads the set:

```python
ctx.points.all("http.routes")               # every value, in order
ctx.points.get("http.routes", "/admin")     # one, by key
ctx.points.where("http.routes", method="GET")   # filtered on properties
ctx.points.last("tools.approvers")          # the most recent
ctx.points.names()                          # every point with something in it
```

and can be woken when it changes:

```python
ctx.points.on_change("http.routes", rebuild_router)
```

That disposer is owned by the **consumer**, so a consumer that unloads stops
being called. Contributors and consumers each own their own half, and neither
imports the other.

### `last()` and the reason it exists

For a role where only the newest contribution matters — the current approver, the
active theme, the selected backend — use `last()`:

```python
ctx.points.add("tools.approvers", my_approver)
ctx.points.last("tools.approvers")          # the newest
```

The obvious alternative is one variable, saved and restored on dispose. That is
only correct if plugins unload in reverse mount order. They do not: unload is
driven by dependencies disappearing, so an *older* registration can go first, and
restoring its saved value would resurrect it over a newer one. `last()` reads by
registration sequence and removes by identity, so no ordering assumption exists
to break.

This was a real bug in `ctx.tools.set_approver` before extension points existed.

## When you still write a `Service`

When the collection has behaviour attached — `ctx.tools` holds tools *and* runs
the five-stage pipeline over them. Then you write a class, and hold the
collection in a point rather than a private dict:

```python
from plugkit import Service


class Router(Service):
    provide = "router"
    inject = ["points"]

    def add(self, path: str, handler: object):
        return self.ctx.points.add("http.routes", handler, key=path, unique=True)

    async def dispatch(self, path: str, request):
        handler = self.ctx.points.get("http.routes", path)
        if handler is None:
            return not_found(request)
        return await handler(request)
```

`unique=True` rejects a second contribution under the same key, which is what you
want for a route table and not what you want for middleware.

## The rebinding rule

Notice `self.ctx.points.add(...)` in `Router.add`, and that the contribution
ends up owned by the *caller*. That works because of one property a `Service`
subclass has and a plain class does not: **when another plugin reads it,
`self.ctx` inside its methods is that plugin's context.**

```python
def admin(ctx, config=None):
    ctx.router.add("/admin", admin_page)     # self.ctx is admin's context
```

Unload `admin` and `/admin` disappears, even though the point belongs to
`PointsService` and the method belongs to `Router`. Without rebinding, every
registry would have to track which plugin added what and clean up itself, and
each would get it subtly wrong in its own way.

`ctx.points.add`, `ctx.tools.register` and `ctx.reactive.effect` all work like
this.

## The rule this creates

**Return an effect from any method that registers something.** A method that
mutates the service's own state on behalf of a caller and returns nothing has
created something nobody owns — the route from [chapter 0](00-why.qmd), one level
down.

```python
def add(self, path, handler):
    self._table[path] = handler          # nobody owns this
```

Using `ctx.points` makes this hard to get wrong, because `add` returns the
disposer and there is no private dict to write to instead.

## Callable data is not rebound

The rebinding walks attribute access, so it distinguishes a *method* from *data
that happens to be callable*. Storing a callback on the instance is fine:

```python
class Approvals(Service):
    provide = "approvals"

    def __init__(self, ctx, config=None):
        super().__init__(ctx)
        self._answer = None        # a callable held as data — not rebound
```

This was got wrong twice in this kernel's history. The discriminator is whether
the name lives in the instance `__dict__`. You do not have to do anything; it is
noted because the symptom, if it regresses, is a stored callback being invoked
with an extra first argument.

## Mounting it

A `Service` subclass is a plugin. Mount it like any other:

```python
await root.plugin(Router)
```

`provide = "router"` is the class attribute that names it. Declare dependencies
with `inject` on the class:

```python
class Router(Service):
    provide = "router"
    inject = ["points", "logger"]
```

## Choosing between the three

| | |
|---|---|
| nothing registers into it | `provide()` — a plain class, no kernel import |
| a collection, nothing more | `ctx.points` — no class at all |
| a collection plus behaviour over it | a `Service` subclass holding its collection in a point |

A `Service` subclass imports the kernel, which is the cost
[chapter 2](02-popo-components.qmd) exists to avoid. Pay it only when there is
behaviour to attach.

## Where this sits among other systems

The pattern has several names. OSGi calls it the **whiteboard** and recommends it
over the listener pattern, because listeners leak — nobody unregisters them.
Eclipse calls it an **extension point**. Spring spells it `List<Handler>`
injection; .NET MEF spells it `ImportMany`.

plugkit's version is small because fiber ownership already solves the leak that
made OSGi write its recommendation. A contribution is removed when its
contributor unloads, so there is no unregister for anyone to forget.

## Next

[Config and reactivity](05-config-and-reactivity.qmd) — values that change while
the program runs.
