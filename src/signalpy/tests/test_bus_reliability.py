"""Tests for bus reliability features: timeout, invoke_nowait, dead letter channel."""
import asyncio
import pytest

from signalpy.kernel.bus import Bus


# ── Helpers ──────────────────────────────────────────────────────────


async def _slow_handler(params):
    await asyncio.sleep(10)
    return {"done": True}


async def _fast_handler(params):
    return {"message": params.get("msg", "ok")}


async def _failing_handler(params):
    raise ValueError("handler exploded")


# ── invoke timeout ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_timeout_raises():
    """invoke() with timeout should raise TimeoutError when handler is slow."""
    bus = Bus()
    bus.register_handler("slow.op", _slow_handler)
    with pytest.raises(asyncio.TimeoutError):
        await bus.invoke("slow.op", {}, timeout=0.05)


@pytest.mark.asyncio
async def test_invoke_timeout_succeeds_when_fast():
    """invoke() with timeout should return normally when handler is fast."""
    bus = Bus()
    bus.register_handler("fast.op", _fast_handler)
    result = await bus.invoke("fast.op", {"msg": "hi"}, timeout=5.0)
    assert result == {"message": "hi"}


@pytest.mark.asyncio
async def test_invoke_no_timeout_backward_compat():
    """invoke() without timeout should work exactly as before."""
    bus = Bus()
    bus.register_handler("fast.op", _fast_handler)
    result = await bus.invoke("fast.op", {"msg": "hello"})
    assert result == {"message": "hello"}


@pytest.mark.asyncio
async def test_invoke_timeout_fires_dead_letter():
    """Timeout should produce a dead letter event."""
    bus = Bus()
    bus.register_handler("slow.op", _slow_handler)
    dead_letters = []
    bus.subscribe(Bus.DEAD_LETTER_CHANNEL, lambda et, data: dead_letters.append(data))

    with pytest.raises(asyncio.TimeoutError):
        await bus.invoke("slow.op", {}, timeout=0.05)

    assert len(dead_letters) == 1
    assert dead_letters[0]["target"] == "slow.op"
    assert dead_letters[0]["reason"] == "timeout"


# ── invoke_nowait ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_nowait_fires_handler():
    """invoke_nowait should schedule handler and it should complete."""
    bus = Bus()
    results = []

    async def capture_handler(params):
        results.append(params.get("val"))

    bus.register_handler("capture.op", capture_handler)
    bus.invoke_nowait("capture.op", {"val": 42})

    # Give the event loop time to run the task
    await asyncio.sleep(0.05)
    assert results == [42]


@pytest.mark.asyncio
async def test_invoke_nowait_no_handler_dead_letter():
    """invoke_nowait with no handler should fire dead letter, not raise."""
    bus = Bus()
    dead_letters = []
    bus.subscribe(Bus.DEAD_LETTER_CHANNEL, lambda et, data: dead_letters.append(data))

    bus.invoke_nowait("nonexistent.op", {"x": 1})
    await asyncio.sleep(0.01)

    assert len(dead_letters) == 1
    assert dead_letters[0]["reason"] == "no_handler"
    assert dead_letters[0]["target"] == "nonexistent.op"


@pytest.mark.asyncio
async def test_invoke_nowait_handler_error_dead_letter():
    """invoke_nowait handler errors should go to dead letter, not propagate."""
    bus = Bus()
    dead_letters = []
    bus.subscribe(Bus.DEAD_LETTER_CHANNEL, lambda et, data: dead_letters.append(data))

    bus.register_handler("fail.op", _failing_handler)
    bus.invoke_nowait("fail.op", {})

    await asyncio.sleep(0.05)

    assert len(dead_letters) == 1
    assert dead_letters[0]["reason"] == "handler_error"
    assert "exploded" in dead_letters[0]["error"]


@pytest.mark.asyncio
async def test_invoke_nowait_returns_immediately():
    """invoke_nowait should return before the handler completes."""
    bus = Bus()
    started = asyncio.Event()

    async def slow_capture(params):
        started.set()
        await asyncio.sleep(10)

    bus.register_handler("slow.capture", slow_capture)
    bus.invoke_nowait("slow.capture", {})

    # The handler should have started but we should be here immediately
    await asyncio.sleep(0.05)
    assert started.is_set()


# ── Dead letter channel ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dead_letter_on_missing_handler():
    """invoke() with no handler should fire dead letter before raising."""
    bus = Bus()
    dead_letters = []
    bus.subscribe(Bus.DEAD_LETTER_CHANNEL, lambda et, data: dead_letters.append(data))

    with pytest.raises(KeyError):
        await bus.invoke("missing.op", {"a": 1})

    assert len(dead_letters) == 1
    assert dead_letters[0]["reason"] == "no_handler"
    assert dead_letters[0]["params"] == {"a": 1}
    assert dead_letters[0]["timestamp"] > 0


@pytest.mark.asyncio
async def test_dead_letter_handler_exception_swallowed():
    """A broken dead letter subscriber should not crash the bus."""
    bus = Bus()

    def broken_subscriber(et, data):
        raise RuntimeError("subscriber crash")

    bus.subscribe(Bus.DEAD_LETTER_CHANNEL, broken_subscriber)

    # This should not raise from the broken subscriber
    with pytest.raises(KeyError):
        await bus.invoke("missing.op", {})


@pytest.mark.asyncio
async def test_dead_letter_envelope_shape():
    """Dead letter envelopes should have target, params, reason, error, timestamp."""
    bus = Bus()
    dead_letters = []
    bus.subscribe(Bus.DEAD_LETTER_CHANNEL, lambda et, data: dead_letters.append(data))

    with pytest.raises(KeyError):
        await bus.invoke("x.y", {"key": "val"})

    dl = dead_letters[0]
    assert set(dl.keys()) == {"target", "params", "reason", "error", "timestamp"}
    assert dl["target"] == "x.y"
    assert dl["params"] == {"key": "val"}
    assert dl["reason"] == "no_handler"
    assert dl["error"] is None


# ── _resolve_handler (refactored internal) ───────────────────────────


@pytest.mark.asyncio
async def test_resolve_handler_name_resolution():
    """invoke should still resolve hallucinated handler names."""
    bus = Bus()
    bus.register_handler("my-app.search", _fast_handler)

    # Underscore → hyphen resolution
    result = await bus.invoke("my_app.search", {"msg": "resolved"})
    assert result == {"message": "resolved"}


@pytest.mark.asyncio
async def test_resolve_handler_target_routing():
    """invoke should still support L3 target routing."""
    bus = Bus()

    async def prod_handler(params):
        return {"env": "prod"}

    bus.register_handler("splunk-prod.query", prod_handler)
    bus.register_target_route("splunk.query", "prod", "splunk-prod.query")

    result = await bus.invoke("splunk.query", {"target": "prod"})
    assert result == {"env": "prod"}
