"""The reactive engine, grafted onto Cordis fibers.

The claim under test is the one that makes the graft worth doing: a reactive
subscription made by a plugin is owned by that plugin's fiber, so it stops when
the plugin unloads — without the plugin writing any teardown.
"""

import asyncio

from plugkit import Context, Service
from plugkit.services.reactive import ReactiveService, Signal


async def settle(n=10):
    for _ in range(n):
        await asyncio.sleep(0)


class Config(Service):
    provide = "config"

    def __init__(self, ctx, config=None):
        super().__init__(ctx)
        self.timeout = Signal(30)


async def test_effect_runs_and_reruns_on_change():
    seen = []
    root = Context()
    await root.plugin(ReactiveService)
    await root.plugin(Config)

    def watcher(ctx, config=None):
        ctx.reactive.effect(lambda: seen.append(ctx.config.timeout.get()))

    watcher.inject = ["config", "reactive"]
    await root.plugin(watcher)
    await settle()

    assert seen == [30]
    root.config.timeout.set(60)
    assert seen == [30, 60]


async def test_subscription_dies_with_the_plugin_that_made_it():
    seen = []
    root = Context()
    await root.plugin(ReactiveService)
    await root.plugin(Config)

    def watcher(ctx, config=None):
        ctx.reactive.effect(lambda: seen.append(ctx.config.timeout.get()))

    watcher.inject = ["config", "reactive"]
    fiber = await root.plugin(watcher)
    await settle()
    assert seen == [30]

    await fiber.dispose()
    await settle()

    root.config.timeout.set(99)
    assert seen == [30], "the effect outlived the plugin that registered it"


async def test_computed_caches_and_is_owned_by_the_caller():
    computes = []
    root = Context()
    await root.plugin(ReactiveService)
    await root.plugin(Config)

    holder = {}

    def deriver(ctx, config=None):
        def double():
            computes.append(1)
            return ctx.config.timeout.get() * 2

        holder["c"] = ctx.reactive.computed(double)

    deriver.inject = ["config", "reactive"]
    fiber = await root.plugin(deriver)
    await settle()

    c = holder["c"]
    assert c.get() == 60
    assert c.get() == 60
    assert len(computes) == 1, "cached value recomputed"

    root.config.timeout.set(50)
    assert c.get() == 100
    assert len(computes) == 2

    await fiber.dispose()
    await settle()
    root.config.timeout.set(1)
    assert len(computes) == 2, "the computed outlived its plugin"


async def test_batch_coalesces_writes():
    runs = []
    root = Context()
    await root.plugin(ReactiveService)
    a, b = Signal(1), Signal(2)

    def watcher(ctx, config=None):
        ctx.reactive.effect(lambda: runs.append(a.get() + b.get()))

    watcher.inject = ["reactive"]
    await root.plugin(watcher)
    await settle()
    assert runs == [3]

    with root.reactive.batch():
        a.set(10)
        b.set(20)
    assert runs == [3, 30], "two writes in a batch produced more than one flush"


def test_the_signals_library_stands_alone():
    """signals.py has no kernel import. Usable in a plain script."""
    from plugkit import Computed, Effect, Signal, batch, is_stale

    a, b = Signal(1), Signal(2)
    total = Computed(lambda: a.get() + b.get())
    assert total.get() == 3

    seen = []
    watcher = Effect(lambda: seen.append(a.get()))
    assert seen == [1]

    with batch():
        a.set(10)
        b.set(20)
    assert total.get() == 30
    assert seen == [1, 10]

    watcher.dispose()
    a.set(99)
    assert seen == [1, 10], "a disposed Effect still ran"
    assert is_stale() is False


async def test_async_effect_supersede_semantics():
    """The reactive-engine page claims a mid-await notification is not dropped."""
    from plugkit import Signal, is_stale
    from plugkit.signals import Effect

    source = Signal(1)
    runs = []

    async def body():
        value = source.get()          # read before the first await
        await asyncio.sleep(0.01)
        runs.append((value, is_stale()))

    effect = Effect(body)
    await asyncio.sleep(0)
    source.set(2)                     # arrives while the first body is awaiting
    for _ in range(20):
        await asyncio.sleep(0.005)

    assert [v for v, _ in runs] == [1, 2], f"a notification was dropped: {runs}"
    effect.dispose()


async def test_is_stale_is_false_outside_a_superseded_run():
    from plugkit import is_stale

    assert is_stale() is False
