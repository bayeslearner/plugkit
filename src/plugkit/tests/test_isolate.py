"""Port of packages/core/tests/isolate.spec.ts and decorator.spec.ts."""

from plugkit.cordis import Context, Inject, Service

from .conftest import Mock, event, sleep


async def test_isolated_context():
    root = Context()
    callback = Mock()
    dispose = Mock()

    def apply(ctx, config):
        callback()
        return dispose

    plugin = {
        "inject": ["foo"],
        "apply": apply,
    }

    await root.plugin(plugin)
    ctx1 = root.isolate("foo")
    await ctx1.plugin(plugin)
    ctx2 = root.isolate("foo")
    await ctx2.plugin(plugin)

    dispose0 = root.provide("foo", {"bar": 100})
    assert root.foo["bar"] == 100
    assert ctx1.foo is None
    assert ctx2.foo is None
    await sleep()
    assert len(callback.calls) == 1
    assert len(dispose.calls) == 0

    dispose1 = ctx1.provide("foo", {"bar": 200})
    assert root.foo["bar"] == 100
    assert ctx1.foo["bar"] == 200
    assert ctx2.foo is None
    await sleep()
    assert len(callback.calls) == 2
    assert len(dispose.calls) == 0

    dispose0()
    assert root.foo is None
    assert ctx1.foo["bar"] == 200
    assert ctx2.foo is None
    await sleep()
    assert len(callback.calls) == 2
    assert len(dispose.calls) == 1

    dispose2 = ctx2.provide("foo", {"bar": 300})
    assert root.foo is None
    assert ctx1.foo["bar"] == 200
    assert ctx2.foo["bar"] == 300
    await sleep()
    assert len(callback.calls) == 3
    assert len(dispose.calls) == 1


async def test_shared_label():
    root = Context()
    callback = Mock()
    dispose = Mock()

    def apply(ctx, config):
        callback()
        return dispose

    plugin = {
        "inject": ["foo"],
        "apply": apply,
    }

    label = object()
    await root.plugin(plugin)
    ctx1 = root.isolate("foo", label)
    await ctx1.plugin(plugin)
    ctx2 = root.isolate("foo", label)
    await ctx2.plugin(plugin)
    await sleep()
    assert len(callback.calls) == 0

    dispose0 = root.provide("foo", {"bar": 100})
    assert root.foo["bar"] == 100
    assert ctx1.foo is None
    assert ctx2.foo is None
    await sleep()
    assert len(callback.calls) == 1
    assert len(dispose.calls) == 0

    dispose12 = ctx1.provide("foo", {"bar": 200})
    assert root.foo["bar"] == 100
    assert ctx1.foo["bar"] == 200
    assert ctx2.foo["bar"] == 200
    await sleep()
    assert len(callback.calls) == 3
    assert len(dispose.calls) == 0

    dispose12()
    assert root.foo["bar"] == 100
    assert ctx1.foo is None
    assert ctx2.foo is None
    await sleep()
    assert len(callback.calls) == 3
    assert len(dispose.calls) == 2


async def test_inject_on_class_method():
    callback = Mock()
    dispose = Mock()

    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

    class Bar(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "bar")

        @Inject("foo")
        def method(self):
            callback()
            return dispose

    root = Context()
    await root.plugin(Bar)
    assert len(callback.calls) == 0
    assert len(dispose.calls) == 0
    fiber = await root.plugin(Foo)
    await sleep()  # let the inject fiber reload settle (JS microtask drain)
    assert len(callback.calls) == 1
    assert len(dispose.calls) == 0
    await fiber.dispose()
    await sleep()
    assert len(callback.calls) == 1
    assert len(dispose.calls) == 1
