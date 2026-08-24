"""Port of packages/core/tests/fiber.spec.ts."""

import asyncio

from plugkit.cordis import Context, FiberState, Service

from .conftest import Mock, event, sleep


async def test_inertia_lock_1():
    root = Context()
    dispose_provide = root.provide("foo", 1)
    runs = 0
    gates = [(asyncio.Event(), asyncio.Event()) for _ in range(2)]  # (apply, dispose) per run

    async def apply(ctx, config):
        nonlocal runs
        index = runs
        runs += 1
        await gates[index][0].wait()

        async def dispose():
            await gates[index][1].wait()

        return dispose

    fiber = root.inject(["foo"], apply)
    await sleep()
    assert runs == 1
    assert fiber.state == FiberState.LOADING  # ~400

    dispose_provide()  # fire-and-forget, like JS
    await sleep()
    assert fiber.state == FiberState.LOADING  # ~800

    gates[0][0].set()  # ~1000: first apply completes
    await sleep()
    assert fiber.state == FiberState.UNLOADING  # ~1200

    root.provide("foo", 1)
    await sleep()
    assert fiber.state == FiberState.UNLOADING  # inertia still locked

    gates[0][1].set()  # ~2000: first disposer completes
    await sleep()
    assert fiber.state == FiberState.LOADING  # ~2200 (reload started, run 2 blocked)

    gates[1][0].set()  # ~3200: second apply completes
    gates[1][1].set()
    await sleep()
    assert fiber.state == FiberState.ACTIVE


async def test_inertia_lock_2():
    root = Context()
    dispose_provide = root.provide("foo", 1)
    gate = asyncio.Event()

    async def apply(ctx, config):
        await gate.wait()
        return lambda: None

    fiber = root.inject(["foo"], apply)
    await sleep()
    assert fiber.state == FiberState.LOADING  # ~400

    dispose_provide()  # fire-and-forget, like JS
    await sleep()
    assert fiber.state == FiberState.LOADING  # ~800

    root.provide("foo", 2)
    gate.set()
    await sleep()
    assert fiber.state == FiberState.ACTIVE  # ~1200


async def test_inertia_lock_3():
    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

    root = Context()
    provider = await root.plugin(Foo)
    gate = asyncio.Event()

    async def apply(ctx, config):
        await gate.wait()
        return lambda: None

    fiber = root.inject(["foo"], apply)
    await sleep()
    assert fiber.state == FiberState.LOADING  # ~400

    gate.set()
    await sleep()
    assert fiber.state == FiberState.ACTIVE  # ~1000

    dispose_task = asyncio.ensure_future(provider.dispose())
    await sleep()
    assert fiber.state == FiberState.PENDING  # ~2000
    await dispose_task


async def test_plugin_error():
    root = Context()
    callback = Mock()
    error = Mock()
    root.logger.error = error

    def apply(ctx, config):
        ctx.on(event, callback)
        if not config or not config.get("foo"):
            raise Exception("plugin error")

    fiber1 = root.plugin(apply)
    fiber2 = root.plugin(apply, {"foo": True})
    await sleep()
    assert fiber1.state == FiberState.FAILED
    assert fiber2.state == FiberState.ACTIVE
    assert len(error.calls) == 1

    root.emit(event)
    assert len(callback.calls) == 1


async def test_dispose_error():
    root = Context()
    error = Mock()
    root.logger.error = error

    def dispose():
        raise Exception("test")

    def plugin(ctx, config):
        return dispose

    fiber = await root.plugin(plugin)
    await fiber.dispose()
    await sleep()
    assert len(error.calls) == 1


async def test_update_config_on_wrapped_fiber():
    root = Context()
    callback = Mock()

    fiber = root.plugin(callback, {"msg": "hello"})
    await fiber
    assert len(callback.calls) == 1
    assert callback.calls[0]["args"][1] == {"msg": "hello"}

    fiber.update({"msg": "world"})
    await fiber
    assert len(callback.calls) == 2
    assert callback.calls[1]["args"][1] == {"msg": "world"}

    fiber.update({"msg": "!!!"})
    await fiber
    assert len(callback.calls) == 3
    assert callback.calls[2]["args"][1] == {"msg": "!!!"}


async def test_restart_wrapped_fiber():
    root = Context()
    callback = Mock()
    fiber = root.plugin(callback)

    await fiber
    await fiber.restart()

    assert len(callback.calls) == 2
    assert fiber.state == FiberState.ACTIVE


async def test_update_config_while_injected_service_reloads():
    applied = []

    class Provider(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "provider")
            self.value = config["value"]

    def consumer_apply(ctx, config):
        applied.append((ctx.provider.value, config["mode"]))

    Consumer = {"inject": ["provider"], "apply": consumer_apply}

    root = Context()
    provider = root.plugin(Provider, {"value": 1})
    consumer = root.plugin(Consumer, {"mode": "old"})

    await provider
    await consumer

    provider.update({"value": 2})
    consumer.update({"mode": "new"})

    await asyncio.gather(provider.await_(), consumer.await_())

    assert applied == [(1, "old"), (2, "new")]
    assert consumer.config == {"mode": "new"}
    assert consumer.state == FiberState.ACTIVE
