---
title: "Why not build on iPOPO"
subtitle: "The closest prior art in Python, tested against what an agent runtime needs"
---

[iPOPO](https://ipopo.readthedocs.io) is a Python implementation of OSGi. It is
the closest thing in Python to what plugkit does, it is older, larger and better
tested, and [What plugkit does not replace](what-it-does-not-replace.qmd) says it
exceeds plugkit in most dimensions.

So it is a fair question why an agent runtime would not use it. Four
findings, each from running iPOPO rather than reading about it.

## 1. Its lifecycle cannot be async

An agent loop is asynchronous end to end: streaming model responses, concurrent
tool calls, subprocess and network I/O. A component that opens an HTTP session or
connects to a database needs to `await` during startup.

iPOPO cannot.

```python
@ComponentFactory("async-factory")
@Provides("async.svc")
class AsyncComponent:
    @Validate
    async def validate(self, context):
        await asyncio.sleep(0.01)
        log.append("async validate finished")
```

```
RuntimeWarning: coroutine 'AsyncComponent.validate' was never awaited
log = []
component state = 1        # 1 = INVALID
```

The coroutine is called, never awaited, and discarded. The component ends
**INVALID** — it silently fails to start, with a warning as the only signal.

This is not an oversight. `pelix.internals.registry` synchronises with
`threading.Lock` and `threading.RLock`; iPOPO is a thread-based framework. The
only asyncio module pelix ships is `pelix.http.basic_async`, an HTTP server, not
the core.

Using it under an async agent means running the kernel on threads and the agent
on an event loop, and bridging every service call across that boundary.

## 2. It does not undo a component's side effects either

This is the same test the README applies to `pluggy`.

```python
@ComponentFactory("admin-factory")
class AdminApi:
    @Validate
    def validate(self, context):
        shared_routes["/admin"] = "handler"

    @Invalidate
    def invalidate(self, context):
        ...             # no cleanup written
```

```
after instantiate : {'/admin': 'handler'}
after kill        : {'/admin': 'handler'}
```

`@Invalidate` runs. The route stays. iPOPO has a **lifecycle** — states,
validate, invalidate — but not lifetime **ownership**: nothing records what the
component did, so cleanup is a method you write and remember to keep in step.

That is the one gap plugkit exists to close.

## 3. No scoped service views

Two agents in one process usually need different tool sets, different
credentials, sometimes different model adapters, under the same service names.

iPOPO's service registry is framework-global. `BundleContext` has no method that
creates a child or scoped view:

```
scoping methods on BundleContext: none
```

Its answers are service ranking, properties and LDAP filters, which select
*among* registered services. Every consumer still queries one registry, so
"agent A sees this tool set and agent B sees that one" has to be encoded in
filters that every consumer must remember to apply.

plugkit inherits Cordis's `ctx.isolate(name)`, which gives a subtree its own
binding for a service name. Nothing below it can see or reach the parent's.

## 4. No dispatch mode that can wrap or veto

A tool permission pipeline needs a listener that runs *around* the call: inspect
the arguments, deny, or pass control to the next listener and then modify what
comes back. That is middleware, and as an event it is a waterfall.

iPOPO's listener API is notification only:

```
add_bundle_listener, add_service_listener, add_framework_stop_listener
```

Listeners are told that something happened. None can wrap another, none can
return a value that changes the outcome. Building a five-stage permission
pipeline on top means writing the chain yourself and routing every tool call
through it by convention, which leaves the gate bypassable from any caller that
does not use it.

Cordis has five dispatch modes; `waterfall` is the one that makes
[the tool pipeline](../guide/03-tools.qmd) possible without a bespoke chain.

## Where this leaves iPOPO

Nothing above says iPOPO is bad. It is a mature OSGi implementation and for a
synchronous, single-scope, plugin-loading application it is the better choice: it
has remote services, an HTTP service, a shell, and a dependency model far richer
than a list of names.

The four findings are all consequences of the same two facts. It was designed for
threads, in 2012, before asyncio existed. And it inherits OSGi's model, in which
a component's lifecycle is a pair of callbacks rather than an owned set of
reversible effects.

An agent runtime needs async lifecycles, per-agent scopes, and a wrapping
dispatch. Those are not features to add to iPOPO; the first is a rewrite of its
core.
