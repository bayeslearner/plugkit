"""Port of packages/logger-console/tests/index.spec.ts."""

import pytest

from plugkit.cordis import Context
from plugkit.cordis.logger_console import ConsoleExporter
from plugkit.cordis.timer import set_clock

from .fake_clock import FakeClock


@pytest.fixture()
def exporter():
    clock = FakeClock()
    set_clock(clock)
    try:
        ctx = Context()
        exp = ConsoleExporter(ctx, {"colors": 0, "showDiff": True, "showTime": ""})
        data = [""]
        exp.export = lambda msg: data.__setitem__(0, data[0] + exp.render(msg) + "\n")
        yield ctx, exp, data, clock
    finally:
        set_clock(None)


async def test_format_error(exporter):
    ctx, exp, data, clock = exporter
    inner = Exception("message")
    inner.__traceback__ = None

    class OuterError(Exception):
        def __init__(self, message, errors):
            super().__init__(message)
            self.errors = errors

    outer = OuterError("outer", [inner])
    ctx.logger("test").error(outer)
    assert data[0] == "[E] test message +0ms\n"


async def test_format_object(exporter):
    ctx, exp, data, clock = exporter
    await clock.advance(2)
    ctx.logger("test").info({"foo": "bar"})
    assert data[0] == "[I] test { 'foo': 'bar' } +2ms\n"


async def test_custom_formatter(exporter):
    ctx, exp, data, clock = exporter
    await clock.advance(1)
    exp.formatters["x"] = lambda value, exporter, message: "custom"
    ctx.logger("test").info("%x%%x")
    assert data[0] == "[I] test custom%x +1ms\n"


async def test_log_levels(exporter):
    ctx, exp, data, clock = exporter
    logger = ctx.logger("test")
    logger.debug("%C", "foo bar")
    assert data[0] == ""

    logger.level = 3
    logger.debug("%C", "foo bar")
    assert data[0]


async def test_label_style(exporter):
    ctx, exp, data, clock = exporter
    exp.label = {"align": "right", "width": 10, "margin": 2}
    ctx.logger("test").info("message\nmessage")
    assert data[0] == "      test  [I]  message\n                 message +0ms\n"
