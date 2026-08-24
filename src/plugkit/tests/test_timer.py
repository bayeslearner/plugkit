"""Port of packages/timer/tests/index.spec.ts."""

import asyncio

import pytest

from plugkit.cordis import Context
from plugkit.cordis.timer import TimerService, set_clock

from .conftest import Mock
from .fake_clock import FakeClock


@pytest.fixture()
def with_clock():
    ctx = Context()
    clock = FakeClock()
    set_clock(clock)
    try:
        yield ctx, clock
    finally:
        set_clock(None)


async def install_timer(ctx, clock):
    await ctx.plugin(TimerService)
    return ctx


def make_iterate(callback, resolve, reject):
    async def iterate(iterator):
        try:
            async for _ in iterator:
                callback()
            resolve()
        except BaseException as error:
            reject(error)

    return iterate


async def test_timeout_basic_support(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()

    async def apply(ctx, config):
        ctx.timeout(callback, 1000)

    await ctx.plugin({"inject": ["timer"], "apply": apply})

    assert len(callback.calls) == 0
    await clock.advance(1000)
    assert len(callback.calls) == 1
    await clock.advance(1000)
    assert len(callback.calls) == 1


async def test_timeout_dispose(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()

    async def apply(ctx, config):
        dispose = ctx.timeout(callback, 1000)
        assert len(callback.calls) == 0
        dispose()
        await clock.advance(2000)
        assert len(callback.calls) == 0

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_timeout_promise(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    resolve = Mock()
    reject = Mock()

    async def apply(ctx, config):
        future = ctx.timeout(1000)

        async def waiter():
            try:
                await future
                resolve()
            except BaseException as error:
                reject(error)

        asyncio.ensure_future(waiter())
        await clock.advance(500)
        assert len(resolve.calls) == 0
        assert len(reject.calls) == 0
        await clock.advance(500)
        assert len(resolve.calls) == 1
        assert len(reject.calls) == 0
        ctx.fiber.dispose()
        await clock.advance(2000)
        assert len(resolve.calls) == 1
        assert len(reject.calls) == 0

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_interval_basic_support(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()

    async def apply(ctx, config):
        dispose = ctx.interval(callback, 1000)
        assert len(callback.calls) == 0
        await clock.advance(1000)
        assert len(callback.calls) == 1
        await clock.advance(1000)
        assert len(callback.calls) == 2
        dispose()
        await clock.advance(2000)
        assert len(callback.calls) == 2

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_interval_async_iterator_manual_return(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()
    resolve = Mock()
    reject = Mock()

    async def apply(ctx, config):
        iterator = ctx.interval(1000)
        asyncio.ensure_future(make_iterate(callback, resolve, reject)(iterator))
        assert len(callback.calls) == 0
        await clock.advance(1000)
        assert len(callback.calls) == 1
        await clock.advance(1000)
        assert len(callback.calls) == 2
        await iterator.aclose()
        await clock.advance(1000)
        assert len(callback.calls) == 2
        assert len(resolve.calls) == 1
        assert len(reject.calls) == 0

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_interval_async_iterator_manual_throw(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()
    resolve = Mock()
    reject = Mock()

    async def apply(ctx, config):
        iterator = ctx.interval(1000)
        asyncio.ensure_future(make_iterate(callback, resolve, reject)(iterator))
        assert len(callback.calls) == 0
        await clock.advance(1000)
        assert len(callback.calls) == 1
        await clock.advance(1000)
        assert len(callback.calls) == 2
        await iterator.athrow(Exception("test"))
        await clock.advance(1000)
        assert len(callback.calls) == 2
        assert len(resolve.calls) == 0
        assert len(reject.calls) == 1

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_interval_async_iterator_break_return(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()
    resolve = Mock()
    reject = Mock()

    async def iterate(iterator):
        try:
            i = 0
            async for _ in iterator:
                i += 1
                if i > 2:
                    break
                callback()
            resolve()
        except BaseException as error:
            reject(error)

    async def apply(ctx, config):
        iterator = ctx.interval(1000)
        asyncio.ensure_future(iterate(iterator))
        assert len(callback.calls) == 0
        await clock.advance(1000)
        assert len(callback.calls) == 1
        await clock.advance(1000)
        assert len(callback.calls) == 2
        await clock.advance(1000)
        assert len(callback.calls) == 2
        assert len(resolve.calls) == 1
        assert len(reject.calls) == 0

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_interval_async_iterator_break_throw(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()
    resolve = Mock()
    reject = Mock()

    async def iterate(iterator):
        try:
            i = 0
            async for _ in iterator:
                i += 1
                if i > 2:
                    raise Exception("test")
                callback()
            resolve()
        except BaseException as error:
            reject(error)

    async def apply(ctx, config):
        iterator = ctx.interval(1000)
        asyncio.ensure_future(iterate(iterator))
        assert len(callback.calls) == 0
        await clock.advance(1000)
        assert len(callback.calls) == 1
        await clock.advance(1000)
        assert len(callback.calls) == 2
        await clock.advance(1000)
        assert len(callback.calls) == 2
        assert len(resolve.calls) == 0
        assert len(reject.calls) == 1

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_interval_async_iterator_context_dispose(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()
    resolve = Mock()
    reject = Mock()

    async def gen(ctx, config):
        iterator = ctx.interval(1000)
        asyncio.ensure_future(make_iterate(callback, resolve, reject)(iterator))
        assert len(callback.calls) == 0
        await clock.advance(1000)
        assert len(callback.calls) == 1
        await clock.advance(1000)
        assert len(callback.calls) == 2
        ctx.fiber.dispose()

        async def disposer():
            await clock.advance(1000)
            assert len(callback.calls) == 2
            assert len(resolve.calls) == 0
            assert len(reject.calls) == 1

        yield disposer

    await ctx.plugin({"inject": ["timer"], "apply": gen})
    await clock.advance(1000)


async def test_throttle_basic_support(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()

    async def apply(ctx, config):
        throttled = ctx.throttle(callback, 1000)
        throttled()
        assert len(callback.calls) == 1
        await clock.advance(600)
        throttled()
        assert len(callback.calls) == 1
        await clock.advance(600)
        throttled()
        assert len(callback.calls) == 2
        await clock.advance(2000)
        assert len(callback.calls) == 3

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_throttle_trailing_mode(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()

    async def apply(ctx, config):
        throttled = ctx.throttle(callback, 1000)
        throttled()
        assert len(callback.calls) == 1
        await clock.advance(500)
        throttled()
        assert len(callback.calls) == 1
        await clock.advance(500)
        assert len(callback.calls) == 2
        await clock.advance(2000)
        assert len(callback.calls) == 2

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_throttle_disposed(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()

    async def apply(ctx, config):
        throttled = ctx.throttle(callback, 1000)
        throttled.dispose()
        throttled()
        assert len(callback.calls) == 1
        await clock.advance(500)
        throttled()
        await clock.advance(2000)
        assert len(callback.calls) == 1

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_debounce_basic_support(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()

    async def apply(ctx, config):
        debounced = ctx.debounce(callback, 1000)
        debounced()
        assert len(callback.calls) == 0
        await clock.advance(400)
        debounced()
        assert len(callback.calls) == 0
        await clock.advance(400)
        debounced()
        assert len(callback.calls) == 0
        await clock.advance(1000)
        assert len(callback.calls) == 1

    await ctx.plugin({"inject": ["timer"], "apply": apply})


async def test_debounce_disposed(with_clock):
    ctx, clock = with_clock
    await install_timer(ctx, clock)
    callback = Mock()

    async def apply(ctx, config):
        debounced = ctx.debounce(callback, 1000)
        debounced.dispose()
        debounced()
        assert len(callback.calls) == 0
        await clock.advance(2000)
        assert len(callback.calls) == 0

    await ctx.plugin({"inject": ["timer"], "apply": apply})
