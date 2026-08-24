# `plugkit/cordis` — derived code and local changes

## Provenance

`src/plugkit/cordis/` implements **Cordis**, the plugin framework underneath
[deepseek-ai/deepseek-harness][dsh] (MIT). The Python implementation derives from
[geohotstan/cordis-py][port] (MIT). Reference for every semantic decision is
`vendor/cordis/src/*.ts` in the harness tree.

The upstream test suite is vendored alongside, under `plugkit/tests/`.

[dsh]: https://github.com/deepseek-ai/deepseek-harness
[port]: https://github.com/geohotstan/cordis-py

## Local changes

Four, listed with the reasoning, because a fork whose diffs are unexplained
cannot be resynced.

### 1. The dispatch carrier is ambient, not a leading positional argument

Cordis binds the carrier as a listener's `this` (`events.ts:dispatch` calls
`hook.callback.bind(thisArg)`), so a listener's parameters are exactly the event
arguments. The upstream port passed the carrier as a leading positional to every
listener in all five dispatch modes. That changes the arity of every listener
away from what DeepSeek Harness's plugins and documentation assume —
`ctx.on('tools/pre-execute', cb)` would need `cb(this, exec)`.

Replaced with a `ContextVar`:

- `cordis/utils.py` — added `this_()`, `call_listener()`, `acall_listener()`
- `cordis/events.py` — all five modes route through them; `internal_listener` and
  `internal_update` read the carrier with `this_()`
- `cordis/reflect.py` — `bind()` traces every argument instead of treating
  `args[0]` as `this`
- `cordis/loader.py`, `cordis/include.py` — seven internal listeners updated
- `plugkit/tests/` — 16 listeners updated to the Cordis signature

`this_()` is valid during a listener's synchronous body, and across an async
listener's own awaits. A coroutine returned from a sync dispatch mode and awaited
elsewhere does not keep it.

### 2. The innermost waterfall default is called with no arguments

TypeScript calls the dispatching service's own default with the full argument
list and lets it ignore the extras. Python has no such tolerance, and every real
Cordis default is nullary (`() => ({ kind: 'allow' })`), so `events.waterfall`
calls `inner()`.

### 3. `Traceable` rebinds methods only, not callable data

`Traceable.__getattr__` rebinds a service's methods onto the caller's view, which
is what makes `ctx.tools.register(t)` owned by the caller's fiber. JavaScript
reaches this through `value.bind(...)`, where only functions are callable. Python
has callable *data* — a `dependency-injector` Configuration, a `functools.partial`,
a client with `__call__`, a lambda held as an attribute — and rebinding it
produces a function invoked with the view as its first argument.

The discriminator is where the attribute lives: anything in the instance
`__dict__` is data. `inspect.getattr_static` alone is not enough, because a
lambda stored as instance data is still `isfunction`.

### 4. Subscript access on `Context`

Added `__getitem__`, `__setitem__` and `__contains__` in `cordis/context.py`,
delegating to the same attribute path. Not upstream, and not a semantic change:
`ctx["database"]` resolves exactly as `ctx.database` does, under the same
isolation scope and the same refusal to read an uninjected service.

The reason is teaching. Attribute access reads like type-based injection, and
this kernel resolves by string name only. It also reaches service names that are
not valid Python identifiers.

### Packaging

Imports are `plugkit.cordis`. Fixture module paths in `tests/fixtures/base.yml`
and the `create.py` scaffold template follow.

## Keeping it in sync

Upstream is a single-author pre-1.0 repository with no releases. This is a fork,
not a dependency. To take upstream changes, diff against the vendored state,
re-apply the four changes above, and run `tests/test_conformance.py` — thirteen
assertions traced to the TypeScript, which is what says whether the port still
holds.
