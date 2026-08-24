# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [SemVer](https://semver.org/).

## [0.1.0] — unreleased

First release.

### The kernel

Contexts, fibers, reversible effects, five event dispatch modes, the plugin
registry, isolation scopes, intercept chains, the composition loader, and hot
module replacement. Implements Cordis; see `src/plugkit/VENDORED.md` for
provenance and the local corrections.

### Binding

- `provide()` — register a plain class as a service. The class needs no
  decorator, no base class and no import from plugkit.
- `@plugin` — mark a function as a plugin and carry its dependency list. With no
  explicit `inject`, the list is derived from a Protocol annotating the first
  parameter.
- `needs` accepts a list, a dict, or a Protocol, whose members are read with
  `typing.get_protocol_members`.
- Constructor arguments may come from `ctx.config`; with `ReactiveService`
  mounted, changing one rebuilds the component.

### Services

Each is an ordinary plugin with no privileged status.

- `ctx.points` — extension points: many plugins filling one named role. A
  contribution is removed when the *contributing* plugin unloads, and a consumer
  can read the set (`all`, `get`, `where`, `last`, `entries`) or be woken when it
  changes (`on_change`). `ctx.tools` holds its tools, guards and approvers here
  rather than in three hand-rolled registries.
- `ctx.config` — YAML, dict, env and pydantic loading. One Signal per dotted key,
  so a write wakes only the readers of that key. Runtime `set()` outranks every
  loader.
- `ctx.reactive` — `Signal` / `Computed` / `Effect` bound to fiber lifetime.
- `ctx.supervisor` — `one_for_one`, `one_for_all` and `rest_for_one` restart
  strategies with constant, linear or exponential backoff, escalating rather than
  looping.
- `ctx.tools` — a tool registry with a five-stage pipeline: a pre-execute veto
  (allow, deny or ask), monotonic guards, an execute waterfall for timeouts and
  metrics, post-execute result rewriting or rejection, and a frozen observation
  event. `Ask` fails closed.
- `ctx.loader` — an application as a YAML list, plugin names resolved as module
  paths. `load_app(root, "app.yml")`.

### Introspection

`describe(ctx)` returns a plain, JSON-serialisable snapshot of the running
system: every fiber with its state, what it provides, what it injects, and — for
a `PENDING` one — **which** injected service is missing. `format_tree` renders a
snapshot as a readable tree. Both are ordinary functions, so they work on a
context whose author never planned to inspect it. A plugin can contribute a
diagnostic the kernel cannot know through the `diagnostics` point.

### Standalone

`plugkit.signals` imports nothing from the kernel and works in a plain script.

### Verification

`test_conformance.py` holds 17 assertions traced to `vendor/cordis/src/*.ts` in
the harness tree. Every README and guide example is executed by the suite,
`test_docs_consistency.py` checks the claims that are not code, and
`test_typing.py` runs pyright over the typed-context patterns.

### Requires

Python 3.13+, for `typing.get_protocol_members`. Optional extras: `config`
(dependency-injector, for env and pydantic loading) and `hmr` (watchdog). Both
degrade rather than fail; the suite runs in both configurations.
