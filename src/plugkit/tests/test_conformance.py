"""Conformance against Cordis's documented semantics, not against this port.

Every assertion here traces to `vendor/cordis/src/*.ts` in deepseek-harness.
This is the gate for vendoring decisions and for taking upstream changes —
a port that passes its own suite can still fail this one, and two of the three
public Python ports do.
"""

import asyncio

import pytest

from plugkit import FiberState, provide
from plugkit.cordis import Context, Service


async def settle(n=10):
    """Let deferred fiber transitions (notify -> reload) finish."""
    for _ in range(n):
        await asyncio.sleep(0)


def make_db():
    class DB(Service):
        provide = "db"

        def __init__(self, ctx, config=None):
            super().__init__(ctx)
            self.tag = config.get("tag", "A") if isinstance(config, dict) else "A"

    return DB


def make_consumer(log):
    """A plugin that registers two effects and one listener, then a disposer."""

    def consumer(ctx, config=None):
        log.append(f"start:{ctx.db.tag}")

        def eff(n):
            def execute():
                log.append(f"acquire{n}")
                return lambda: log.append(f"release{n}")

            return execute

        ctx.effect(eff(1), "e1")
        ctx.effect(eff(2), "e2")
        ctx.on("ping", lambda: log.append("pong"))
        return lambda: log.append("stop")

    consumer.inject = ["db"]
    return consumer


async def test_inject_gates_activation():
    """registry.ts: a plugin only loads while every injected service exists."""
    log = []
    root = Context()
    await root.plugin(make_consumer(log))
    await settle()
    assert log == []


async def test_starts_when_provider_appears():
    """reflect.ts:notify -> fiber._refresh -> _reload."""
    log = []
    root = Context()
    await root.plugin(make_consumer(log))
    await root.plugin(make_db(), {"tag": "A"})
    await settle()
    assert log[:3] == ["start:A", "acquire1", "acquire2"]


async def test_listener_registered_inside_a_plugin_is_live():
    log = []
    root = Context()
    await root.plugin(make_consumer(log))
    await root.plugin(make_db(), {"tag": "A"})
    await settle()
    log.clear()
    root.emit("ping")
    assert log == ["pong"]


async def test_waterfall_listener_takes_only_event_args():
    """events.ts binds the carrier as `this`; it is never an argument.

    This is the assertion the vendored port originally failed. See VENDORED.md.
    """
    root = Context()
    root.on("gate", lambda payload, next_: next_())
    assert root.waterfall("gate", "good", lambda: "allowed") == "allowed"


async def test_waterfall_vetoes_when_next_is_not_called():
    root = Context()
    root.on("gate", lambda payload, next_: "denied" if payload == "bad" else next_())
    assert root.waterfall("gate", "bad", lambda: "allowed") == "denied"
    assert root.waterfall("gate", "good", lambda: "allowed") == "allowed"


async def test_unload_is_total():
    """fiber.ts:_unload — disposers start in reverse, and nothing survives.

    Synchronous disposers here, so the observed order is exact reverse. Async
    disposers run concurrently; see test_disposal_ordering.
    """
    log = []
    root = Context()
    await root.plugin(make_consumer(log))
    db = await root.plugin(make_db(), {"tag": "A"})
    await settle()
    log.clear()
    await db.dispose()
    await settle()
    assert log in (["stop", "release2", "release1"], ["release2", "release1", "stop"])
    root.emit("ping")
    assert "pong" not in log


async def test_provider_swap_reactivates_dependents():
    """fiber.ts:_refresh — the epoch is a digest of providing-fiber identity."""
    log = []
    root = Context()
    await root.plugin(make_consumer(log))
    db = await root.plugin(make_db(), {"tag": "A"})
    await settle()
    await db.dispose()
    await settle()
    log.clear()
    await root.plugin(make_db(), {"tag": "B"})
    await settle()
    assert "start:B" in log


async def test_isolate_scopes_a_service_name():
    """context.ts:isolate — a child scope resolves the name independently."""
    root = Context()
    await root.plugin(make_db(), {"tag": "ROOT"})
    child = root.isolate("db")
    await child.plugin(make_db(), {"tag": "CHILD"})
    await settle()
    assert root.db.tag == "ROOT"
    assert child.db.tag == "CHILD"


async def test_duplicate_provide_in_one_scope_raises():
    """reflect.ts:provide — `service "x" has been registered at <...>`."""
    root = Context()
    await root.plugin(make_db(), {"tag": "A"})
    await settle()
    with pytest.raises(Exception, match="registered"):
        await root.plugin(make_db(), {"tag": "B"})


async def test_disposal_ordering_sync_versus_async():
    """`fiber.py:_start_unload` gathers disposers rather than awaiting in turn.

    Disposers are started in reverse registration order. Synchronous ones
    therefore complete in exact reverse. An async disposer yields at its first
    await, so a later-started one can finish first — completion order follows
    duration, not registration. The docs must not promise LIFO for async.
    """
    order = []
    root = Context()

    def plugin(ctx, config=None):
        def sync_effect(n):
            def install():
                return lambda: order.append(f"sync{n}")
            return install

        def async_effect(n, delay):
            def install():
                async def dispose():
                    await asyncio.sleep(delay)
                    order.append(f"async{n}")
                return dispose
            return install

        ctx.effect(sync_effect(1))
        ctx.effect(sync_effect(2))
        ctx.effect(async_effect(3, 0.03))
        ctx.effect(async_effect(4, 0.01))

    fiber = await root.plugin(plugin)
    await fiber.dispose()
    for _ in range(10):
        await asyncio.sleep(0.01)

    assert order[:2] == ["sync2", "sync1"], "sync disposers are not exact reverse"
    assert order[2:] == ["async4", "async3"], "async completion is not by duration"
    assert order != ["async4", "async3", "sync2", "sync1"], "this would be strict LIFO"


# ── the three properties an agent runtime needs ─────────────────────────
#
# Each is something iPOPO, the closest prior art in Python, cannot do. They are
# asserted here because docs/design/why-not-ipopo.qmd rests on them.


async def test_a_plugin_body_may_be_async():
    """iPOPO calls an `async def @Validate` without awaiting it: the component
    ends INVALID with a RuntimeWarning."""
    log = []

    async def slow(ctx, config=None):
        await asyncio.sleep(0.01)
        log.append("done")

    root = Context()
    fiber = await root.plugin(slow)
    assert fiber.state is FiberState.ACTIVE
    assert log == ["done"]


async def test_isolated_scopes_give_subtrees_their_own_binding():
    """iPOPO's registry is framework-global; BundleContext has no child view."""

    class Tools:
        def __init__(self, label):
            self.label = label

    root = Context()
    agent_a = root.isolate("tools")
    agent_b = root.isolate("tools")

    await agent_a.plugin(provide(Tools, "tools", extra={"label": "a"}))
    await agent_b.plugin(provide(Tools, "tools", extra={"label": "b"}))
    await settle()

    assert agent_a.tools.label == "a"
    assert agent_b.tools.label == "b"


async def test_a_waterfall_listener_can_wrap_and_veto():
    """iPOPO listeners are notification only; none can wrap or change an outcome."""
    root = Context()
    root.on("gate", lambda payload, next_: "denied" if payload == "bad" else next_())

    assert root.waterfall("gate", "good", lambda: "allowed") == "allowed"
    assert root.waterfall("gate", "bad", lambda: "allowed") == "denied"


# ── the remaining public surface ─────────────────────────────────────────


async def test_errors_are_catchable_by_their_exported_types():
    from plugkit import CordisError, ValidationError

    assert issubclass(CordisError, Exception)
    assert issubclass(ValidationError, Exception)


async def test_parallel_collects_listener_failures():
    """`ctx.parallel` awaits every listener and aggregates their errors."""
    from plugkit import AggregateError

    root = Context()

    async def ok():
        return 1

    async def bad():
        raise RuntimeError("listener failed")

    root.on("go", ok)
    root.on("go", bad)
    with pytest.raises(AggregateError):
        await root.parallel("go")


async def test_inject_normalises_list_and_dict_forms():
    from plugkit import Inject

    assert Inject.resolve(["a", "b"]) == {"a": None, "b": None}
    assert Inject.resolve({"a": 1}) == {"a": 1}


async def test_plugin_returns_a_fiber():
    """`Fiber` is exported because it is the type `plugin()` hands back."""
    from plugkit import Fiber

    root = Context()
    fiber = root.plugin(lambda ctx, config=None: None)
    assert isinstance(fiber, Fiber)
    assert fiber.state in tuple(FiberState)
