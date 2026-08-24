"""Port of packages/core/tests/plugin.spec.ts."""

import pytest

from plugkit.cordis import Context, FiberState, Service

from .conftest import Mock, event, get_hook_snapshot, sleep


async def test_apply_functional_plugin():
    root = Context()
    callback = Mock()
    options = {"foo": "bar"}
    await root.plugin(callback, options)

    assert len(callback.calls) == 1
    assert callback.calls[0]["args"][1] == options


async def test_apply_object_plugin():
    root = Context()
    callback = Mock()
    options = {"bar": "foo"}
    plugin = {"apply": callback}
    await root.plugin(plugin, options)

    assert len(callback.calls) == 1
    assert callback.calls[0]["args"][1] == options


async def test_apply_invalid_plugin():
    root = Context()
    with pytest.raises(TypeError):
        root.plugin(None)
    with pytest.raises(TypeError):
        root.plugin({})
    with pytest.raises(TypeError):
        root.plugin({"apply": {}})


async def test_inactive_context():
    root = Context()
    callback = Mock()

    def plugin(ctx, config):
        def dispose():
            with pytest.raises(Exception, match="inactive context"):
                ctx.plugin(callback)
            with pytest.raises(Exception, match="inactive context"):
                ctx.effect(lambda: lambda: None)
            with pytest.raises(Exception, match="inactive context"):
                ctx.on("custom-event", lambda: None)

        return dispose

    fiber = root.plugin(plugin)
    await fiber.dispose()
    assert len(callback.calls) == 0


async def test_context_inspect():
    root = Context()

    assert repr(root) == "Context <root>"

    async def plugin(ctx, config):
        assert repr(ctx) == "Context <plugin>"

    # anonymous (lambda) plugins inherit the parent fiber name
    await root.plugin(lambda ctx, config: None)
    assert repr(root) == "Context <root>"

    await root.plugin(plugin)

    def foo(ctx, config):
        assert repr(ctx) == "Context <foo>"

    await root.plugin(foo)

    async def apply(ctx, config):
        assert repr(ctx) == "Context <bar>"

    await root.plugin({"name": "bar", "apply": apply})

    class Qux:
        def __init__(self, ctx, config):
            assert repr(ctx) == "Context <Qux>"

    await root.plugin(Qux)


def test_ctx_registry():
    root = Context()
    list(root.registry.keys())
    list(root.registry.values())
    list(root.registry.entries())
    root.registry.for_each(lambda value, key: None)


async def test_nested_plugins():
    async def plugin(ctx, config):
        ctx.on(event, callback)

        async def inner1(ctx, config):
            ctx.on(event, callback)

            async def inner2(ctx, config):
                ctx.on(event, callback)

            await ctx.plugin(inner2)

        await ctx.plugin(inner1)

    root = Context()
    callback = Mock()
    root.on(event, callback)
    fiber = await root.plugin(plugin)

    assert len(callback.calls) == 0
    assert root.registry.size == 3
    root.emit(event)
    assert len(callback.calls) == 4

    callback.reset_calls()
    await fiber.dispose()
    assert root.registry.size == 0
    root.emit(event)
    assert len(callback.calls) == 1

    callback.reset_calls()
    await fiber.dispose()
    assert root.registry.size == 0
    root.emit(event)
    assert len(callback.calls) == 1


async def test_compare_snapshot():
    async def plugin(ctx, config):
        ctx.on(event, lambda: None)

        async def inner1(ctx, config):
            ctx.on(event, lambda: None)

            async def inner2(ctx, config):
                ctx.on(event, lambda: None)

            await ctx.plugin(inner2)

        await ctx.plugin(inner1)

    root = Context()
    before = get_hook_snapshot(root)
    await root.plugin(plugin)
    after = get_hook_snapshot(root)
    root.registry.delete(plugin)
    await sleep()
    assert before == get_hook_snapshot(root)
    await root.plugin(plugin)
    assert after == get_hook_snapshot(root)


async def test_root_dispose():
    root = Context()
    dispose = Mock()
    fiber = root.plugin(lambda ctx, config: dispose)
    assert root.fiber.uid == 0
    assert fiber.uid == 1
    assert len(dispose.calls) == 0
    assert root.fiber._disposables.length == 1
    await root.fiber.dispose()
    assert root.fiber.uid == 0
    assert fiber.uid is None
    assert len(dispose.calls) == 1
    assert root.fiber._disposables.length == 0
    await root.fiber.dispose()
    assert root.fiber.uid == 0
    assert fiber.uid is None
    assert len(dispose.calls) == 1
    assert root.fiber._disposables.length == 0


async def test_service_init():
    start = Mock()
    stop = Mock()

    class Foo:
        def __init__(self, ctx, config):
            pass

        def __cordis_init__(self):
            start()
            return stop

    root = Context()
    fiber = await root.plugin(Foo)
    assert len(start.calls) == 1
    assert len(stop.calls) == 0
    await fiber.dispose()
    assert len(start.calls) == 1
    assert len(stop.calls) == 1


async def test_context_provide_and_set():
    root = Context()
    assert Context.is_(root)
    assert root.get("foo") is None
    dispose = root.provide("foo")
    assert root.get("foo") is None
    root.set("foo", 1)
    assert root.get("foo") == 1
    await dispose()
    assert root.get("foo") is None
