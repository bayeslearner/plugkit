"""Supervision, grafted onto Cordis's unused FAILED state."""

import asyncio

from plugkit import Context, FiberState
from plugkit.services.supervision import SupervisorService, compute_delay


async def settle(n=30):
    for _ in range(n):
        await asyncio.sleep(0)


async def mount_failing(root, plugin):
    """Mount a plugin that will fail.

    `await root.plugin(p)` rethrows startup errors (fiber.py `__await__`,
    matching Cordis), so a failing mount is started without awaiting it and
    observed through its state instead.
    """
    fiber = root.plugin(plugin)
    await settle()
    return fiber


def flaky(fail_times: int, log: list):
    """A plugin that raises on its first `fail_times` applications."""
    state = {"n": 0}

    def plugin(ctx, config=None):
        state["n"] += 1
        if state["n"] <= fail_times:
            log.append(f"boom{state['n']}")
            raise RuntimeError("not ready")
        log.append(f"ok{state['n']}")

    return plugin


def test_backoff_shapes():
    assert [compute_delay(1, n, "constant") for n in (1, 2, 3)] == [1, 1, 1]
    assert [compute_delay(1, n, "linear") for n in (1, 2, 3)] == [1, 2, 3]
    assert [compute_delay(1, n, "exponential") for n in (1, 2, 3)] == [1, 2, 4]


async def test_unsupervised_failure_stays_failed():
    """The behaviour without the graft — this is what Cordis does today."""
    log = []
    root = Context()
    fiber = await mount_failing(root, flaky(1, log))
    assert fiber.state is FiberState.FAILED
    assert log == ["boom1"]


async def test_transient_failure_is_restarted():
    log = []
    root = Context()
    await root.plugin(SupervisorService)
    fiber = await mount_failing(root, flaky(1, log))
    assert fiber.state is FiberState.FAILED

    root.supervisor.supervise(fiber, base_delay=0, backoff="constant")
    fiber._error = None
    fiber.restart()  # kick it once so the next failure is observed under policy
    await settle()

    assert "ok2" in log, log
    assert fiber.state is FiberState.ACTIVE


async def test_exceeding_max_restarts_escalates():
    log = []
    escalated = []
    root = Context()
    await root.plugin(SupervisorService)
    root.on("supervisor/escalate", lambda fiber, error: escalated.append(fiber.name))

    fiber = await mount_failing(root, flaky(99, log))
    root.supervisor.supervise(fiber, max_restarts=2, base_delay=0, backoff="constant")

    for _ in range(6):
        if escalated:
            break
        fiber._error = None
        fiber.restart()
        await settle()

    assert escalated, "never escalated"
    assert fiber.state is FiberState.FAILED
    policy = root.supervisor.policy_for(fiber)
    assert len(policy.history) == 3, policy.history


async def test_supervision_ends_with_the_plugin_that_asked_for_it():
    log = []
    root = Context()
    await root.plugin(SupervisorService)
    target = await mount_failing(root, flaky(99, log))

    def owner(ctx, config=None):
        ctx.supervisor.supervise(target, base_delay=0, backoff="constant")

    owner.inject = ["supervisor"]
    owner_fiber = await root.plugin(owner)
    await settle()
    assert root.supervisor.policy_for(target) is not None

    await owner_fiber.dispose()
    await settle()
    assert root.supervisor.policy_for(target) is None, "policy outlived its owner"
