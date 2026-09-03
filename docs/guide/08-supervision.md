---
title: "Supervision"
subtitle: "What to do about FAILED"
---

Chapter 1 showed you the `FAILED` state: the plugin function raised, and the
fiber sits failed until someone restarts it by hand. For a long-running backend
that is not a policy — a database that was not up yet, a token that was not
minted — a transient failure at boot permanently removes a plugin from the
system.

This chapter is the answer to `FAILED`. `ctx.supervisor` declares what should
happen when a fiber fails and carries it out: restart it, with a backoff, and
give up — escalate — once it fails too many times too quickly.

## The supervisor watches the `FAILED` state, nothing else

Mount the service like any other:

```python
from plugkit import Context
from plugkit.services.supervision import SupervisorService

root = Context()
await root.plugin(SupervisorService)
```

That is all it takes. From here on, `root.supervisor` exists, and it reacts to
the kernel's `FAILED` state — the same state chapter 1 taught. It does not add
new failure modes or vocabulary; it is the payoff of the state you already have.

## Supervising a fiber

Put a fiber under supervision with `supervise`:

```python
async def main():
    root = Context()
    await root.plugin(SupervisorService)
    db = await root.plugin(Database)
    root.supervisor.supervise(db, max_restarts=5, within=60)
```

When `db` fails, the supervisor restarts it. The keyword arguments configure a
`Policy`:

| argument | default | meaning |
|---|---|---|
| `strategy` | `"one_for_one"` | which fibers restart when one fails |
| `max_restarts` | `3` | how many failures are tolerated |
| `within` | `60.0` | the window, in seconds, over which they count |
| `backoff` | `"exponential"` | how the delay between restarts grows |
| `base_delay` | `0.5` | the first delay, in seconds |

### The three strategies

`one_for_one` restarts only the fiber that failed. `one_for_all` restarts every
fiber mounted under the same parent context. `rest_for_one` restarts the failed
fiber and everything mounted after it.

```python
# only the failed fiber restarts
root.supervisor.supervise(db, strategy="one_for_one")
```

The choice is the Erlang answer to related failures: if `db` and the worker that
talks to it were mounted together and a `db` failure leaves the worker useless,
`one_for_all` restarts both and restores the pair.

### The backoff

The delay before restart attempt *n* grows from `base_delay`:

| backoff | delays |
|---|---|
| `constant` | `base`, `base`, `base`, … |
| `linear` | `base`, `2×base`, `3×base`, … |
| `exponential` | `base`, `2×base`, `4×base`, … |

Set `base_delay=0` to restart immediately — useful in tests.

## Escalation is the seam

Exceeding `max_restarts` within `within` seconds stops the restarts and emits
`supervisor/escalate`, leaving the fiber failed. That event is the seam: a parent
supervisor, an alerting plugin, or a process-level bail-out listens for it.

```python
def bail_out(fiber, error):
    print(f"{fiber.name} gave up after too many restarts")

root.on("supervisor/escalate", bail_out)
```

Restarting forever is not a policy either. Escalation is how the supervisor
admits a failure is not transient.

## Supervision ends with whoever asked for it

`supervise` returns a disposer, registered as an effect of the *calling* plugin.
When that plugin unloads, supervision of the fiber ends. Two plugins can watch
the same fiber with different policies, each for as long as it lives.

## Try it

The flaky plugin below fails on its first application and then succeeds. Without
a supervisor it stays `FAILED`; supervised, it is restarted and comes up:

```python
import asyncio

from plugkit import Context, FiberState
from plugkit.services.supervision import SupervisorService

state = {"n": 0}

def flaky(ctx, config=None):
    state["n"] += 1
    if state["n"] <= 2:
        raise RuntimeError("not ready")

async def main():
    root = Context()
    await root.plugin(SupervisorService)
    fiber = root.plugin(flaky)          # not awaited: a failing mount rethrows
    await asyncio.sleep(0)
    assert fiber.state is FiberState.FAILED

    root.supervisor.supervise(fiber, base_delay=0, backoff="constant")
    # kick it once so the next failure is observed under the policy
    fiber.restart()
    await asyncio.sleep(0)

    assert fiber.state is FiberState.ACTIVE
```

You have now seen the whole loop: build a fiber, keep your classes plain, add
tools, extend the kernel, compose from a file, and restart what fails. The
[Testing](07-testing.md) chapter turns it into a suite.
