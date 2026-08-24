"""Port of packages/core/tests/associate.spec.ts and shadow.spec.ts."""

import pytest

from plugkit.cordis import Context, Service, symbols

from .conftest import event


async def test_service_injection():
    root = Context()

    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")
            self.qux = 1

    class FooBar(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo.bar")

    await root.plugin(Foo)
    fiber = await root.plugin(FooBar)
    assert isinstance(root.foo, Foo)
    assert isinstance(root.foo.bar, FooBar)
    assert root.foo.qux == 1
    await fiber.dispose()
    assert root.foo.bar is None


async def test_property_injection():
    root = Context()

    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

    root.provide("foo.bar")
    root.provide("foo.baz")
    await root.plugin(Foo)
    assert isinstance(root.foo, Foo)
    root.foo.qux = 2
    root.foo.bar = 3

    def baz(self):
        return self

    # instance attribute functions return `this` — the Foo view
    root.foo.baz = baz

    async def apply(ctx, config):
        assert ctx.foo.qux == 2
        assert ctx.foo.bar == 3
        with pytest.raises(AttributeError, match='cannot get property "foo.qux" without inject'):
            getattr(ctx, 'foo.qux')
        assert getattr(ctx, "foo.bar") == 3
        assert isinstance(ctx.foo.baz(), Foo)

    await root.inject(["foo"], apply)


async def test_associated_type_service_injection():
    class Session:
        def __init__(self, ctx):
            self.__cordis_tracker__ = {"property": "ctx", "associate": "session"}
            self.ctx = ctx

    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        def create_session(self):
            return Session(self.ctx)

    class Bar(Service):
        inject = ["foo"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "bar")
            ctx.mixin("bar", {"answer": "session.answer"})

        def answer(self):
            return 42

    root = Context()
    await root.plugin(Foo)

    async def apply(ctx, config):
        session = ctx.foo.create_session()
        assert isinstance(session, Session)
        assert session.bar is None

        async def inner(ctx, config):
            session = ctx.foo.create_session()
            assert isinstance(session, Session)
            assert session.answer() == 42

        await ctx.plugin(Bar)
        await ctx.inject(["bar"], inner)

    await root.inject(["foo"], apply)


async def test_associated_type_accessor_injection():
    class Session:
        def __init__(self, ctx):
            self.__cordis_tracker__ = {"property": "ctx", "associate": "session"}
            self.ctx = ctx

    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        def session(self):
            return Session(self.ctx)

    class Bar:
        def __init__(self, ctx, config):
            self.secret = None
            ctx.mixin(self, {"bar": "session.bar"})

        @property
        def bar(self):
            return self.secret

        @bar.setter
        def bar(self, value):
            self.secret = value + 1

    root = Context()
    await root.plugin(Foo)
    await root.plugin(Bar)

    async def apply(ctx, config):
        session = ctx.foo.session()
        assert isinstance(session, Session)
        # JS: `ctx[barInstance]` coerces to a missing key → reflect error
        with pytest.raises(AttributeError):
            session.bar

        # the inner inject never activates — `bar` is never provided
        async def inner(ctx, config):
            session = ctx.foo.session()
            assert session.bar is None
            session.bar = 100
            assert session.bar == 101

        await ctx.inject(["bar"], inner)

    await root.inject(["foo"], apply)


async def test_keeps_caller_metadata_separate_from_the_service_shadow():
    inner_origin = []
    outer_origin = []

    class Inner(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "inner")
            inner_origin.append(ctx)

        def inspect(self):
            return {
                "caller": getattr(self, symbols.caller),
                "shadow": self.ctx._shadow,
            }

    class Outer(Service):
        inject = ["inner"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "outer")
            outer_origin.append(ctx)

        def inspect(self):
            result = dict(self.ctx.inner.inspect())
            result["outer_shadow"] = self.ctx._shadow
            return result

    root = Context()
    await root.plugin(Inner)
    await root.plugin(Outer)

    result = []

    async def apply(ctx, config):
        result.append(ctx.outer.inspect())

    await root.inject(["outer"], apply)

    assert result[0]["caller"] is outer_origin[0]
    assert result[0]["shadow"] is inner_origin[0]
    assert result[0]["outer_shadow"] is outer_origin[0]


async def test_exposes_the_caller_without_preserving_shadow_for_noshadow_services():
    outer_origin = []

    class Probe:
        def __init__(self, ctx):
            self.__cordis_tracker__ = {"property": "ctx", "noShadow": True}
            self.ctx = ctx

        def inspect(self):
            return {
                "caller": getattr(self, symbols.caller),
                "shadow": getattr(self.ctx, "_shadow", None),
            }

    class Outer(Service):
        inject = ["probe"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "outer")
            outer_origin.append(ctx)

        def inspect(self):
            return self.ctx.probe.inspect()

    root = Context()
    root.provide("probe", Probe(root))
    await root.plugin(Outer)

    result = []

    async def apply(ctx, config):
        result.append(ctx.outer.inspect())

    await root.inject(["outer"], apply)

    assert result[0]["caller"] is outer_origin[0]
    assert result[0]["shadow"] is None


async def test_exposes_the_caller_to_callable_services():
    outer_origin = []

    class Callable(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "callable")

        def __cordis_invoke__(self):
            return getattr(self, symbols.caller)

    class Outer(Service):
        inject = ["callable"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "outer")
            outer_origin.append(ctx)

        def call(self):
            return self.ctx.callable()

    root = Context()
    await root.plugin(Callable)
    await root.plugin(Outer)

    caller = []

    async def apply(ctx, config):
        caller.append(ctx.outer.call())

    await root.inject(["outer"], apply)

    assert caller[0] is outer_origin[0]


async def test_strips_service_shadow_before_creating_plugins():
    class Loader(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "loader")

        def load(self, plugin):
            return self.ctx.plugin(plugin)

    class Server(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "server")

    injected = []

    class Consumer:
        # JS: `function Consumer(ctx)` is a *constructor* — the body runs and
        # its return value is ignored.  A Python class mirrors that.
        def __init__(self, ctx, config):
            def check(ctx, config):
                injected.append(isinstance(ctx.server, Server))

            ctx.inject(["server"], check)

    root = Context()
    await root.plugin(Loader)

    async def apply(ctx, config):
        loader = ctx.loader
        await loader.load(Server)
        await loader.load(Consumer)

    await root.inject(["loader"], apply)

    assert injected == [True]
    assert len([m for m in root.logger.buffer if m.type == "error"]) == 0


async def test_inspect():
    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        def bar(self, arg):
            assert "X" in repr(arg)
            self.baz(arg)

        def baz(self, arg):
            assert "X" in repr(arg)

    root = Context()
    await root.plugin(Foo)

    class X:
        pass

    root.foo.bar(X)


async def test_isolated_event():
    class Foo(Service):
        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")
            self.ctx.emit(self, event)

    root = Context()
    ctx = root.isolate("foo")
    outer = []
    inner = []
    root.on(event, lambda *args: outer.append(1))
    ctx.on(event, lambda *args: inner.append(1))
    await ctx.plugin(Foo)

    assert outer == []
    assert inner == [1]
