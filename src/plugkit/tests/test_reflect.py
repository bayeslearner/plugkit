"""Port of packages/core/tests/reflect.spec.ts."""

import pytest

from plugkit.cordis import Context

from .conftest import Mock


async def test_context_is():
    class SubContext(Context):
        pass

    assert Context.is_(SubContext())


async def test_access_check():
    root = Context()

    async def plugin(ctx, config):
        ctx.prototype  # noqa: B018 — reserved property access must not throw
        ctx.constructor  # noqa: B018
        with pytest.raises(AttributeError, match='cannot get property "bar" without inject'):
            ctx.bar
        with pytest.raises(AttributeError, match='cannot set property "bar" without provide'):
            ctx.bar = 0

    await root.plugin(plugin)

    async def plugin2(ctx, config):
        with pytest.raises(AttributeError, match='cannot set property "foo" without provide'):
            ctx.foo = 0
        ctx.provide("foo")
        with pytest.raises(AttributeError, match='service "foo" has been registered at <plugin2>'):
            ctx.provide("foo")
        ctx.foo = 0

    await root.plugin(plugin2)


async def test_service_injection():
    root = Context()
    warn = Mock()
    root.logger.warn = warn
    root.mixin("foo", ["bar"])
    root.provide("foo")
    root.set("foo", {"bar": 1})

    # foo is a service
    assert root.get("foo") is not None
    # bar is a mixin
    assert root.get("bar") is None
    # root is a property
    assert root.get("root") is None

    async def apply(ctx, config):
        warn.reset_calls()
        assert len(warn.calls) == 0

        async def inner(ctx, config):
            warn.reset_calls()
            assert ctx.baz == 2
            assert len(warn.calls) == 0

        ctx.extend({"baz": 2}).plugin(inner)

    root.inject(["foo"], apply)


async def test_service_inject_leak():
    root = Context()
    root.provide("foo")
    root.set("foo", {"bar": 1})

    async def apply(ctx, config):
        pass

    fiber = await root.inject(["foo"], apply)
    assert fiber.ctx.foo is not None
    await fiber.dispose()
    with pytest.raises(AttributeError, match='cannot get required service "foo" in inactive context'):
        fiber.ctx.foo
