"""Port of packages/core/tests/service.spec.ts."""

import asyncio

from plugkit.cordis import Context, Service

from .conftest import Counter, Mock, get_hook_snapshot, sleep


async def test_pending_inject():
    # JS test waits for a 'custom-event' to release the init promise.
    release = asyncio.Event()

    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        async def __cordis_init__(self):
            self.ctx.on("custom-event", lambda: release.set())
            await release.wait()

    root = Context()

    callback = Mock()
    root.inject(["foo"], callback)
    assert len(callback.calls) == 0

    # inject should be blocked by `Service.init`
    root.plugin(Foo)
    await sleep()
    assert len(callback.calls) == 0

    root.emit("custom-event")
    await sleep()
    assert len(callback.calls) == 1


async def test_traceable_effect_with_inject():
    class Foo(Service):
        inject = ["counter"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        @property
        def value(self):
            return self.ctx.counter.value

        def increase(self):
            return self.ctx.counter.increase()

    root = Context()
    warning = Mock()
    root.logger.warn = warning
    root.provide("counter")
    root.set("counter", Counter(root))

    await root.plugin(Foo)
    root.foo.increase()
    assert root.foo.value == 1
    assert len(warning.calls) == 0

    async def apply(ctx, config):
        root.foo.increase()
        assert ctx.foo.value == 2
        assert len(warning.calls) == 0

    fiber = await root.inject(["foo"], apply)

    await fiber.dispose()
    root.foo.increase()
    assert root.foo.value == 3
    assert len(warning.calls) == 0


async def test_traceable_effect_without_inject():
    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        @property
        def value(self):
            return self.ctx.counter.value

        def increase(self):
            return self.ctx.counter.increase()

    root = Context()
    root.provide("counter")
    root.set("counter", Counter(root))

    await root.plugin(Foo)
    root.foo.increase()
    assert root.foo.value == 1

    async def apply(ctx, config):
        root.foo.increase()
        assert root.foo.value == 2

    fiber = await root.inject(["foo"], apply)

    await fiber.dispose()
    root.foo.increase()
    assert root.foo.value == 3


async def test_compare_snapshot():
    class Test(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "test")
            ctx.inject(["test"], lambda ctx, config: None)

    root = Context()
    before = get_hook_snapshot(root)
    await root.plugin(Test)
    after = get_hook_snapshot(root)
    root.registry.delete(Test)
    await sleep()
    assert before == get_hook_snapshot(root)
    root.plugin(Test)
    assert after == get_hook_snapshot(root)


async def test_multiple_injects():
    foo = Mock()
    bar = Mock()
    qux = Mock()

    class Foo(Service):
        inject = ["qux"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        def __cordis_init__(self):
            foo()
            return None

    class Bar(Service):
        inject = ["foo", "qux"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "bar")

        def __cordis_init__(self):
            bar()
            return None

    class Qux(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "qux")

        def __cordis_init__(self):
            qux()
            return None

    root = Context()
    await root.plugin(Foo)
    await root.plugin(Bar)
    await root.plugin(Qux)
    await sleep()
    assert len(foo.calls) == 1
    assert len(bar.calls) == 1
    assert len(qux.calls) == 1
