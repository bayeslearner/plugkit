---
title: "Tools and permission"
subtitle: "The five-stage pipeline, and why a guard has no allow"
---

`ctx.tools` is a registry of callable tools plus a pipeline every call passes
through. It exists because of one requirement: **add a rule about everyone's
tools without touching any of them.**

That is the difference between a plugin system and a folder of modules. If a
security rule means editing twenty tool implementations, you do not have
composability, you have a convention.

## A tool is a plain object

Duck-typed. Anything with `name`, `description`, and `execute`:

```python
class SearchTool:                     # imports nothing from plugkit
    name = "search"
    description = "Search the index."
    parameters = {"type": "object", "properties": {"q": {"type": "string"}}}

    def __init__(self, index):
        self.index = index

    def execute(self, arguments, execution):
        return self.index.query(arguments["q"])
```

Register it from a plugin:

```python
def search_plugin(ctx, config=None):
    ctx.tools.register(SearchTool(index))
search_plugin.inject = ["tools", "index"]
```

Mount the registry before anything registers into it. `ToolsService` holds its
tools, guards and approvers in extension points, so it injects `points`:

```python
from plugkit import PointsService, ToolsService

await root.plugin(PointsService)
await root.plugin(ToolsService)
await root.plugin(search_plugin)
```

`register` is an effect, so the tool disappears when `search_plugin` unloads.
You do not unregister it.

Call it:

```python
result = await root.tools.execute("search", {"q": "kernel"})
result.ok        # True
result.value     # whatever execute returned
```

`execute` never raises. Failures come back as a `ToolResult` with a code —
`UNKNOWN_TOOL`, `DENIED`, `BLOCKED`, `TOOL_ERROR`, `TOOL_TIMEOUT`, `ABORTED`.

## The five stages

Each stage is an extension point. The list names the event, its dispatch mode,
and what a listener may return.

```
1. tools/pre-execute    waterfall    Allow | Deny | Ask
2. ctx.tools.guard()    monotonic    a reason string, or None
3. tools/execute        waterfall    wraps the call
4. tools/post-execute   waterfall    Accept | Block
5. tools/result         emit         observe only
```

Picking one:

| You want to | Stage | Why not the others |
|---|---|---|
| ask a human first | 1 | the only stage with an `Ask` |
| a rule nobody can override | 2 | guards cannot allow, only deny |
| a timeout, a retry, a duration metric | 3 | the only stage holding the whole call |
| change or reject a result | 4 | stage 5 is immutable |
| log, audit, count | 5 | doing it in 4 races other listeners |

### Stage 1 — allow, deny, or ask

A **waterfall**: listeners wrap each other, like middleware, ending in the
registry's own default.

```python
from plugkit import Deny


def policy(ctx, config=None):
    async def gate(execution, next_):
        if execution.arguments.get("path", "").startswith("/etc"):
            return Deny("no reads under /etc")
        return await next_()          # don't forget this line
    ctx.on("tools/pre-execute", gate)
policy.inject = ["tools"]
```

`next_()` runs the rest of the chain, including the registry's default (allow).
Not calling it vetoes everything after you.

There is deliberately **no "rewrite the arguments" option**. By the time this
stage runs, the arguments are already logged and on screen; letting a listener
change them would make history, audit, display, and execution disagree.

`Ask` **fails closed**. No approver registered, an approver that raises, or an
approver returning anything but `True` all become a denial:

```python
from plugkit import Ask


def approval(ctx, config=None):
    ctx.on("tools/pre-execute", lambda e, next_: Ask("run this?") if e.tool.destructive else next_())
    ctx.tools.set_approver(lambda execution, reason: ask_the_human(reason))
approval.inject = ["tools"]
```

### Stage 2 — guards, and why they have no allow

A guard inspects the call and either denies it or stays silent:

```python
def safety(ctx, config=None):
    ctx.tools.guard(
        lambda e: "refusing to delete the root filesystem"
        if e.arguments.get("path") == "/" else None
    )
safety.inject = ["tools"]
```

Return a reason to deny, `None` to stay out of the way. **There is no allow
return value.**

If there were: two guards, one denies, one allows — whichever ran last wins. Mount
order here depends on when dependencies became available, so a security rule
would hold or not hold depending on how fast a database started.

With no allow, a denial from any guard is final, regardless of order and
regardless of what stage 1 decided:

```python
# stage 1 says allow. The guard still wins.
result = await root.tools.execute("delete", {"path": "/"})
result.ok                       # False
result.error["message"]         # "refusing to delete the root filesystem"
```

This property has a name — **monotonic**: the check can only ever reduce
permission. Any gate you add to this kernel should have it, and if you find
yourself wanting an allow on a guard, you are building a stage-1 veto and should
say so.

### Stage 3 — wrapping the call

The only stage where the call's beginning and end are in the same function, so
timeouts, retries, and metrics live here.

```python
def metrics(ctx, config=None):
    async def wrap(execution, next_):
        start = time.monotonic()
        result = await next_()
        record(execution.name, time.monotonic() - start)
        return result
    ctx.on("tools/execute", wrap)
metrics.inject = ["tools"]
```

`timeout_policy` ships as the reference implementation — it reads `timeout_s` off
the tool itself:

```python
from plugkit import timeout_policy
await root.plugin(timeout_policy)
```

::: {.callout-warning}
## A timeout notifies; it does not kill

Declaring `timeout_s` is a promise that your tool forwards cancellation to
something that can stop. A body that ignores it will not stop when the
deadline fires — you will get a `TOOL_TIMEOUT` result while the work continues.

Registration order also picks the semantics. With a timeout and a retry wrapper
both on `tools/execute`: timeout registered *outer* means the whole retry
operation shares one clock; *inner* means each attempt gets its own.
:::

### Stage 4 — changing the result

A post-execute listener receives the result and may replace or reject it:

```python
from plugkit import Accept


def redact(ctx, config=None):
    async def scrub(execution, result, next_):
        if execution.name == "read_secrets":
            return Accept.replacing("[redacted]")
        return await next_()
    ctx.on("tools/post-execute", scrub)
redact.inject = ["tools"]
```

`Accept.replacing(value)` swaps the value; `Block(feedback)` rejects the result
and hands the caller your feedback instead.

### Stage 5 — observing

Everything is frozen. A listener that raises here is logged and contained — it
**cannot** change whether the call succeeded:

```python
def audit(ctx, config=None):
    ctx.on("tools/result", lambda execution, result: write_audit_row(execution, result.ok))
audit.inject = ["tools"]
```

Stage 5 fires on **every** outcome, including denials. An audit that only sees
successes is not an audit.

## Three plugins that never heard of each other

Three plugins that have never heard of each other:

```python
await root.plugin(PointsService)   # tools holds its registries in points
await root.plugin(ToolsService)
await root.plugin(search_plugin)      # provides a tool
await root.plugin(delete_plugin)      # provides another
await root.plugin(safety)             # a rule over both
await root.plugin(audit)              # a record of both
await root.plugin(timeout_policy)     # a budget over both
```

`safety` was written without knowing `delete` exists. `delete` was written
without knowing `safety` exists. Neither imports the other. Adding a fourth tool
inherits the rule, the audit trail, and the budget for free — and unloading
`safety` removes its rule cleanly, because that registration was an effect like
any other.

## Where the shape comes from

This is DeepSeek Harness's tool pipeline, ported stage for stage. Its
documentation calls it the widest and most useful extension surface it has, and
because the event names and semantics match, dsh's writing about it applies here
directly.

## Next

[Extension points](04-your-own-service.qmd) — how `ctx.tools` holds its tools,
and how to build a collection others contribute to.

- [`docs/design/kernel-architecture.qmd`](https://github.com/bayeslearner/plugkit/blob/main/docs/design/kernel-architecture.qmd) — the architecture anchor
- `src/plugkit/tests/test_tools.py` — one test per stage, plus the properties above
