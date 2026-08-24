"""Port of packages/core/tests/dispose.spec.ts."""

import asyncio

import pytest

from plugkit.cordis import Context

from .conftest import Mock, event, sleep


async def test_dispose_by_plugin():
    root = Context()
    dispose = Mock()

    def plugin(ctx, config):
        ctx.effect(lambda: dispose, "test")

    fiber = await root.plugin(plugin)
    assert fiber.get_effects() == [{"label": "test", "children": []}]
    assert len(dispose.calls) == 0
    await fiber.dispose()
    assert len(dispose.calls) == 1
    await fiber.dispose()
    assert len(dispose.calls) == 1


async def test_dispose_manually():
    root = Context()
    dispose1 = Mock()
    dispose2 = root.effect(lambda: dispose1)
    assert root.fiber.get_effects() == [{"label": "anonymous", "children": []}]
    assert len(dispose1.calls) == 0
    await dispose2()
    assert len(dispose1.calls) == 1
    await dispose2()
    assert len(dispose1.calls) == 1


async def test_yield_dispose():
    root = Context()
    seq = []

    def gen():
        yield lambda: seq.append(1)
        yield root.on("custom-event", lambda: None)
        yield lambda: seq.append(2)

        def inner_gen():
            yield root.on("custom-event", lambda: None)
            yield lambda: seq.append(3)

        yield root.effect(inner_gen)

    dispose = root.effect(gen)
    root.on("custom-event", lambda: None)
    assert root.fiber.get_effects() == [
        {
            "label": "anonymous",
            "children": [
                {"label": 'ctx.on("custom-event")', "children": []},
                {"label": "anonymous", "children": [{"label": 'ctx.on("custom-event")', "children": []}]},
            ],
        },
        {"label": 'ctx.on("custom-event")', "children": []},
    ]
    assert seq == []
    await dispose()
    assert seq == [3, 2, 1]
    await dispose()
    assert seq == [3, 2, 1]


async def test_async_return_1():
    root = Context()
    seq = []
    gate = asyncio.Event()

    async def effect():
        await gate.wait()
        seq.append(1)
        return lambda: seq.append(2)

    dispose = root.effect(effect)
    assert seq == []
    gate.set()
    await sleep()
    assert seq == [1]
    await dispose()
    assert seq == [1, 2]


async def test_async_return_2():
    root = Context()
    seq = []
    gate = asyncio.Event()

    async def effect():
        await gate.wait()
        seq.append(1)
        return lambda: seq.append(2)

    dispose = root.effect(effect)
    dispose()  # fire-and-forget, like JS
    assert seq == []
    gate.set()
    await sleep()
    assert seq == [1, 2]


async def test_async_yield_1():
    root = Context()
    seq = []
    gates = [asyncio.Event() for _ in range(3)]

    async def gen():
        await gates[0].wait()
        seq.append(1)
        yield lambda: seq.append(2)
        await gates[1].wait()
        seq.append(3)
        yield lambda: seq.append(4)
        await gates[2].wait()
        seq.append(5)
        yield lambda: seq.append(6)

    dispose = root.effect(gen)
    assert seq == []
    for gate in gates:
        gate.set()
    await sleep()
    assert seq == [1, 3, 5]
    await dispose()
    assert seq == [1, 3, 5, 6, 4, 2]


async def test_async_yield_2_aborted():
    root = Context()
    seq = []
    gate = asyncio.Event()

    async def gen():
        await gate.wait()
        seq.append(1)
        yield lambda: seq.append(2)
        seq.append(3)
        yield lambda: seq.append(4)
        seq.append(5)
        yield lambda: seq.append(6)

    dispose = root.effect(gen)
    await sleep()
    dispose()  # fire-and-forget, like JS
    assert seq == []
    gate.set()
    await sleep()
    assert seq == [1, 2]


async def test_async_yield_3_aborted():
    root = Context()
    seq = []
    gate1 = asyncio.Event()
    gate2 = asyncio.Event()

    async def gen():
        await gate1.wait()
        seq.append(1)
        yield lambda: seq.append(2)
        await gate2.wait()
        seq.append(3)
        yield lambda: seq.append(4)
        seq.append(5)
        yield lambda: seq.append(6)

    dispose = root.effect(gen)
    assert seq == []
    gate1.set()
    await sleep()
    assert seq == [1]
    dispose()  # fire-and-forget, like JS
    assert seq == [1]
    gate2.set()
    await sleep()
    assert seq == [1, 3, 4, 2]


async def test_async_yield_4_await_dispose():
    root = Context()
    seq = []
    gates = [asyncio.Event() for _ in range(3)]

    async def gen():
        await gates[0].wait()
        seq.append(1)
        yield lambda: seq.append(2)
        await gates[1].wait()
        seq.append(3)
        yield lambda: seq.append(4)
        await gates[2].wait()
        seq.append(5)
        yield lambda: seq.append(6)

    dispose = root.effect(gen)
    assert seq == []
    for gate in gates:
        gate.set()
    dispose2 = await dispose
    assert seq == [1, 3, 5]
    await dispose2()
    assert seq == [1, 3, 5, 6, 4, 2]


async def test_return_with_error():
    root = Context()
    seq = []

    def effect():
        raise Exception("test")
        return lambda: seq.append(1)  # noqa: B012

    with pytest.raises(Exception, match="test"):
        root.effect(effect)
    assert seq == []


async def test_yield_with_error():
    root = Context()
    seq = []

    def gen():
        yield lambda: seq.append(1)
        raise Exception("test")
        yield lambda: seq.append(2)  # noqa: B012

    with pytest.raises(Exception, match="test"):
        root.effect(gen)
    assert seq == [1]


async def test_async_return_with_error():
    root = Context()
    seq = []

    async def effect():
        raise Exception("test")
        return lambda: seq.append(1)  # noqa: B012

    dispose = root.effect(effect)
    assert seq == []
    with pytest.raises(Exception, match="test"):
        await dispose
    assert seq == []


async def test_async_yield_with_error():
    root = Context()
    seq = []

    async def gen():
        yield lambda: seq.append(1)
        raise Exception("test")
        yield lambda: seq.append(2)  # noqa: B012

    dispose = root.effect(gen)
    assert seq == []
    caught = None
    try:
        await dispose
    except BaseException as e:
        caught = e
    assert isinstance(caught, Exception)
    assert seq == [1]
