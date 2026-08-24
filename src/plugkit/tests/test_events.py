"""Port of packages/core/tests/events.spec.ts."""

import pytest

from plugkit.cordis import AggregateError, Context

from .conftest import Filter, Mock, Session, event


async def test_ctx_on():
    root = Context()
    callback = Mock()
    dispose = root.on(event, callback)
    root.emit(event)
    assert len(callback.calls) == 1
    root.emit(event)
    assert len(callback.calls) == 2
    dispose()
    root.emit(event)
    assert len(callback.calls) == 2


async def test_ctx_once():
    root = Context()
    callback = Mock()
    dispose = root.once(event, callback)
    root.emit(event)
    assert len(callback.calls) == 1
    root.emit(event)
    assert len(callback.calls) == 1
    dispose()
    root.emit(event)
    assert len(callback.calls) == 1


async def test_ctx_parallel():
    root = Context()
    await root.parallel(event)
    callback = Mock()
    root.extend(Filter(True)).on(event, callback)

    await root.parallel(event)
    assert len(callback.calls) == 1
    await root.parallel(Session(False), event)
    assert len(callback.calls) == 1
    await root.parallel(Session(True), event)
    assert len(callback.calls) == 2

    # a rejecting listener must not short-circuit the others
    settled = False

    async def reject(*args):
        nonlocal settled
        await __import__("asyncio").sleep(0)
        settled = True
        raise Exception("async")

    dispose = root.on(event, reject)
    callback.mock_implementation(lambda *args: (_ for _ in ()).throw(Exception("test")))
    error = None
    try:
        await root.parallel(event)
    except BaseException as e:
        error = e
    assert isinstance(error, AggregateError)
    assert sorted(str(e) for e in error.exceptions) == ["async", "test"]
    assert settled
    dispose()


async def test_ctx_emit():
    root = Context()
    root.emit(event)
    callback = Mock()
    root.extend(Filter(True)).on(event, callback)

    root.emit(event)
    assert len(callback.calls) == 1
    root.emit(Session(False), event)
    assert len(callback.calls) == 1
    root.emit(Session(True), event)
    assert len(callback.calls) == 2

    callback.mock_implementation(lambda *args: (_ for _ in ()).throw(Exception("test")))
    with pytest.raises(Exception, match="test"):
        root.emit(event)


async def test_ctx_serial():
    root = Context()
    root.serial(event)
    callback = Mock()
    root.extend(Filter(True)).on(event, callback)

    await root.serial(event)
    assert len(callback.calls) == 1
    await root.serial(Session(False), event)
    assert len(callback.calls) == 1
    await root.serial(Session(True), event)
    assert len(callback.calls) == 2

    callback.mock_implementation(lambda *args: (_ for _ in ()).throw(Exception("message")))
    with pytest.raises(Exception, match="message"):
        await root.serial(event)


async def test_ctx_bail():
    root = Context()
    root.bail(event)
    callback = Mock()
    root.extend(Filter(True)).on(event, callback)

    root.bail(event)
    assert len(callback.calls) == 1
    root.bail(Session(False), event)
    assert len(callback.calls) == 1
    root.bail(Session(True), event)
    assert len(callback.calls) == 2

    callback.mock_implementation(lambda *args: (_ for _ in ()).throw(Exception("message")))
    with pytest.raises(Exception, match="message"):
        root.bail(event)


async def test_ctx_waterfall():
    root = Context()

    def cb1(value, next_):
        return value + next_()

    def cb2(value, next_):
        return value + next_()

    mock1 = Mock(cb1)
    mock2 = Mock(cb2)
    root.on("test/waterfall", mock1)
    root.on("test/waterfall", mock2)

    assert root.waterfall("test/waterfall", 1, lambda *args: 2) == 4
    assert len(mock1.calls) == 1
    assert len(mock2.calls) == 1
    mock1.reset_calls()
    mock2.reset_calls()

    def cb3(value, next_):
        return value

    def cb4(value, next_):
        return value + next_()

    mock3 = Mock(cb3)
    mock4 = Mock(cb4)
    root.on("test/waterfall", mock3)
    root.on("test/waterfall", mock4)
    assert root.waterfall("test/waterfall", 1, lambda *args: 2) == 3
    assert len(mock1.calls) == 1
    assert len(mock2.calls) == 1
    assert len(mock3.calls) == 1
    assert len(mock4.calls) == 0
