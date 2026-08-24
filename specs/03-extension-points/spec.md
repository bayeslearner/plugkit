---
spec_id: 03-extension-points
status: CLOSED
closed_as: SHIPPED
since: 2026-08-23
until: null
epic: meta-layer
features: [extension-points]
supersedes: []
superseded_by: null
depends_on: [01-plugkit-kernel, 02-package-installability]
anchors: [kernel-architecture]
---

# Extension points — many plugins filling one role

# 1 · Requirements

## Introduction

Spec 01 gave the kernel one component's lifetime. This spec covers the first
facility built *on top of* that: letting many components fill one named role, and
letting a consumer read the set.

A **meta-layer** facility is one that works the same on a database plugin, an
HTTP server and a model adapter, and never names a domain concept. That test
sorts the shelves from the books. `services/tools.py` fails it — it names `Tool`,
`ToolExecution`, allow and deny — so it is a book. This spec builds the shelf it
should have been standing on.

## Glossary

- **Extension point** — a named role that any number of plugins may fill. OSGi
  calls the pattern the *whiteboard*; Eclipse calls it an *extension point*;
  Spring spells it `List<Handler>` injection.
- **Contribution** — one plugin's entry in a point: a value, its properties, and
  the fiber that owns it.
- **Invoke half** — running the contributions. `ctx.on` plus the five dispatch
  modes.
- **Enumerate half** — reading the contributions without running them.

## The gap, measured

Cordis has the invoke half and not the enumerate half.

`cordis/events.py` gives a named role with many contributors, each owned by the
contributing fiber (`register()` wraps every listener in `ctx.fiber.effect`),
ordered by prepend/append, and filtered by context in `_resolve`. What it does
not give:

| | `ctx.on` + dispatch | needed |
|---|---|---|
| many contributors, one role | yes | |
| removed when the contributor unloads | yes | |
| isolation-aware | yes | |
| ordering | prepend/append only | explicit |
| **read the contributors** | no — `_hooks` is private | `all()` |
| **look one up by key** | no — the chain is anonymous | `get(key)` |
| **properties on a contribution** | no | `where(**props)` |
| **wake when the set changes** | no | `on_change` |

Those bottom four get hand-written wherever they are needed.

**In DeepSeek Harness**: eleven packages define their own `register()` —
`ui-slots`, `core/agent`, `core/tools`, `webserver`, `commands`, `invariants`,
`session-title`, `shell-env`, `skill`, `storage/registry`,
`subagent/activation-setup-registry` — and 263 files call one. `core/tools` is
1,946 lines and emits its own `tools/change` by hand at line 813. Only
`typert/registry` generalised it, and its contract is exactly the missing half:
`register → disposer`, `get(key)`, `resolve(key)`, `list(filter?)`.

**In plugkit**: `services/tools.py` builds it three times in one file —
`_tools` (a dict, unique keys, `get`/`list`/`names`), `_guards` (a list, all
called), `_approvers` (a list, last wins). Roughly 70 lines of identical
plumbing: append inside `ctx.effect`, return a remover.

## What is *not* in scope

**Interception.** A listener that wraps a call, inspects arguments, and vetoes by
declining to call `next()` is a solved problem: `waterfall` does it, and DSH's
guards are plain `ctx.on('tools/execute', ...)` listeners with no registry at
all. `guard/timeout-policy/src/index.ts` is one file and contains no plumbing.
Nothing to build.

**Start levels / load phases.** DSH states the rule in
`bundle/base/cordis.patch.yml`: *"Row order carries no load semantics (activation
is service-availability driven)."* Dependency order already covers it.

**Readiness.** Every `health`/`readiness` hit in DSH is domain-level. No
evidence a kernel facility is wanted.

## Requirements

**R1.** Any plugin can contribute a value to a named point without knowing who
reads it, and without the point having been declared.

**R2.** A contribution is removed when the **contributing** plugin unloads. The
contributor writes no teardown.

**R3.** A consumer can read the contributions: all of them in order, one by key,
or a subset filtered on properties.

**R4.** A consumer can register a callback that runs when the set changes, owned
by the **consumer's** fiber.

**R5.** A point may require keys to be unique, and adding a duplicate raises.

**R6.** Two isolated subtrees see different contributions under the same point
name.

**R7.** `services/tools.py` is rewritten on this facility, and its three
hand-rolled registries become one. If it does not get shorter, the design is
wrong.

# 2 · Design

## `PointsService` — an ordinary plugin

No privileged status, consistent with the rule that config and logging are
plugins too. Provides `ctx.points`.

```python
from plugkit import PointsService
await root.plugin(PointsService)
```

### Contributing

```python
def admin(ctx, config=None):
    ctx.points.add("http.routes", handler, key="/admin", order=10, methods=["GET"])

admin.inject = ["points"]
```

`add` returns a disposer, and — through the rebinding rule from spec 01 — it is
registered as an effect of **`admin`'s** fiber, not of `PointsService`'s. Unload
`admin` and the contribution goes. This is the same mechanism `ctx.tools.register`
already uses; the point is that it stops being re-implemented per registry.

### Reading

```python
ctx.points.all("http.routes")              # [handler, ...] in order
ctx.points.get("http.routes", "/admin")    # one, by key, or None
ctx.points.where("http.routes", methods="GET")   # filtered on properties
ctx.points.entries("http.routes")          # [Contribution(value, key, order, props)]
ctx.points.last("http.routes")             # most recently added, or None
ctx.points.names()                         # every point with a contribution
```

Ordering is `order` ascending, then registration order. A flat API rather than a
view object returned from `ctx.points(name)`: a `__call__` on a service goes
through the `Traceable` proxy, and that path already caused two bugs.

### Reacting

```python
ctx.points.on_change("http.routes", rebuild)     # -> disposer, owned by the caller
```

Backed by one `Signal` per point, so `plugkit.signals` and `ctx.reactive.effect`
both compose with it — reading `all()` inside a reactive effect re-runs that
effect when the set changes, with no explicit subscription.

### Uniqueness

```python
ctx.points.add("tools", tool, key=tool.name, unique=True)
```

`unique=True` raises `ValueError` if the key is taken. Declared per contribution
rather than per point, because a point is never declared — it exists when
something is contributed to it. A point where any contributor asks for uniqueness
enforces it for that key.

### Isolation

No new mechanism. `ctx.isolate("points")` already gives a subtree its own
`PointsService` instance with its own storage, which satisfies R6. Adding a
second filtering layer inside the service would duplicate what the kernel does.

## Rewriting `tools.py`

| was | becomes |
|---|---|
| `self._tools: dict` + `register()` + `get`/`list`/`names` | `add("tools", tool, key=name, unique=True)`, `get`, `all` |
| `self._guards: list` + `guard()` | `add("tools.guards", guard)` |
| `self._approvers: list` + `set_approver()` + `_approver` property | `add("tools.approvers", a)` + `last(...)` |

The approver stack is the clearest gain. Its current comment explains at length
why it is a stack and not a save/restore slot — a real bug, fixed once, in one
registry. `last()` gives every point that property.

`ToolsService` gains `points` in its `inject`. Tool validation (`name` is a
non-empty string, `execute` is callable) stays in `tools.py`: it is domain
knowledge, and the shelf must not learn what a tool is.

## What this does not become

A second lookup mechanism for services. `ctx.database` resolves one provider
under one name; `ctx.points` holds many values under one role. Keeping them
separate is deliberate — two ways to find a service is worse than one imperfect
way.

# 3 · Tasks

- [x] **T1** — `services/points.py`: `PointsService`, `Contribution`
- [x] **T2** — exported from `plugkit/__init__.py`
- [x] **T3** — `tests/test_points.py`, 32 tests across R1–R6
- [x] **T4** — `tools.py` rewritten on it
- [x] **T5** — R7 measured, below
- [x] **T6** — chapter 04 rewritten as "Extension points"; six new examples in `test_guide_examples.py`; chapter 03 and 07 updated to mount `PointsService`

- [x] **T7** — `kernel-architecture.md` gains a "meta layer" section: the shelf/book test, the invoke/enumerate split, and what deliberately has no facility

## R7, measured

```
tools.py code lines: 255 -> 239  (16 fewer)
points.py code lines: 120
hand-rolled register/dispose blocks left in tools.py: 0
```

**The first consumer does not pay for the facility.** 16 lines saved against 120
written is a loss, and R7 as I wrote it — "if it does not get shorter, the design
is wrong" — was too weak a test to notice that. Stating the real justification
instead:

1. **All three registries are gone**, not shrunk. What stayed in `tools.py` is
   tool validation, which is domain knowledge and belongs there.
2. **`last()` is the approver-stack bug, fixed once for everyone.** That bug —
   disposing an older registration wiping a newer approver — was found in review,
   fixed in `tools.py`, and its explanation ran to five lines of comment. Every
   point now has the property without anyone rediscovering it.
3. **The next registry costs one line.** `points.py` also carries `where`,
   `entries`, `on_change`, `count` and `has`, which `tools.py` does not use at
   all. It is a shelf, and shelves are judged by the second book.

The honest read: this is an investment, not a saving, and it will be wrong if no
second consumer arrives. Spec 04 is the first test of that.

## Behaviour change

`ToolsService` now declares `inject = ["points"]`, so a composition using tools
must mount `PointsService` first:

```python
await root.plugin(PointsService)
await root.plugin(ToolsService)
```

Without it, `ToolsService` stays `PENDING` and `ctx.tools` is unreachable — the
kernel's normal behaviour for an unmet dependency, not a special case. Six test
setups were updated. No tool-facing API changed.

## Verification

```console
$ uv run pytest src/plugkit/tests -q
335 passed, 3 skipped, 2 xfailed
```
