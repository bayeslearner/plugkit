"""Port of packages/core/tests/logger.spec.ts."""

from plugkit.cordis import Context, Service

from .conftest import sleep


def setup():
    ctx = Context()
    captured = []
    ctx.logger.exporter({"colors": 0, "levels": {"default": 3}, "export": lambda msg: captured.append(msg)})
    return ctx, captured


async def test_keeps_the_bounded_buffer_in_place_and_chronological():
    ctx = Context()
    buffer = ctx.logger.buffer
    ctx.logger.bufferSize = 2
    ctx.logger.info("one")
    ctx.logger.info("two")
    ctx.logger.info("three")
    assert ctx.logger.buffer is buffer
    assert [message.args[0] for message in buffer] == ["two", "three"]

    ctx.logger.bufferSize = 1
    ctx.logger.info("four")
    assert [message.args[0] for message in buffer] == ["four"]

    ctx.logger.bufferSize = 0
    ctx.logger.info("five")
    assert buffer == []


async def test_disposes_the_exporter_that_registered_the_disposer():
    ctx = Context()
    ctx.logger.exporters.clear()
    first = []
    second = []
    dispose_first = ctx.logger.exporter({"export": lambda message: first.append(message)})
    dispose_second = ctx.logger.exporter({"export": lambda message: second.append(message)})

    dispose_first()
    ctx.logger.info("test")
    assert first == []
    assert len(second) == 1

    dispose_second()
    ctx.logger.info("test")
    assert len(second) == 1


async def test_uses_fiber_name_when_called_from_outside_any_service():
    ctx, captured = setup()
    ctx.logger.debug("hello")
    assert [m.name for m in captured] == ["root"]


async def test_honours_explicit_name_argument():
    ctx, captured = setup()
    ctx.logger("custom").debug("hello")
    assert [m.name for m in captured] == ["custom"]


async def test_honours_intercept_name():
    ctx, captured = setup()
    ctx.intercept("logger", {"name": "intercepted"}).logger.debug("hello")
    assert [m.name for m in captured] == ["intercepted"]


async def test_uses_service_name_when_called_from_inside_a_service_method():
    ctx, captured = setup()

    class FooService(Service):
        name = "foo:driver"

        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        def action(self):
            self.ctx.logger.debug("from action")

    await ctx.plugin(FooService)
    ctx.foo.action()
    await sleep()
    assert "foo:driver" in [m.name for m in captured]
    assert "root" not in [m.name for m in captured]


async def test_still_lets_outer_caller_intercept_override_the_service_derived_name():
    ctx, captured = setup()

    class FooService(Service):
        name = "foo:driver"

        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        def action(self):
            self.ctx.logger.debug("from action")

    await ctx.plugin(FooService)
    ctx.intercept("logger", {"name": "caller-override"}).foo.action()
    await sleep()
    assert "caller-override" in [m.name for m in captured]
    assert "foo:driver" not in [m.name for m in captured]


async def test_uses_the_innermost_service_name_and_restores_the_outer_service():
    ctx, captured = setup()

    class BarService(Service):
        name = "bar:driver"

        def __init__(self, ctx, config):
            super().__init__(ctx, "bar")

        def action(self):
            self.ctx.logger.debug("from bar")

    class FooService(Service):
        name = "foo:driver"
        inject = ["bar"]

        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        def action(self):
            self.ctx.bar.action()
            self.ctx.logger.debug("from foo")

    await ctx.plugin(BarService)
    await ctx.plugin(FooService)

    async def apply(ctx, config):
        ctx.foo.action()

    await ctx.inject(["foo"], apply)

    assert [(m.name, m.args[0]) for m in captured] == [
        ("bar:driver", "from bar"),
        ("foo:driver", "from foo"),
    ]


async def test_uses_service_name_when_called_from_inside_init():
    ctx, captured = setup()

    class FooService(Service):
        name = "foo:driver"

        def __init__(self, ctx, config):
            super().__init__(ctx, "foo")

        async def __cordis_init__(self):
            self.ctx.logger.debug("from init")

    await ctx.plugin(FooService)
    await sleep()
    assert "foo:driver" in [m.name for m in captured]
