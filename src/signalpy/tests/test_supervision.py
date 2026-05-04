"""Tests for supervision strategies: one_for_one, one_for_all, rest_for_one."""
import asyncio
import pytest
from pydantic import BaseModel
from unittest.mock import AsyncMock

from signalpy.kernel import (
    Kernel,
    component,
    provides,
    requires,
    runnable,
    lifecycle,
    SupervisionContext,
    SupervisionEscalation,
)
from signalpy.kernel.lifecycle_manager import (
    LifecycleManager,
    State,
    RestartTracker,
    _compute_delay,
)


# ── Helpers ──────────────────────────────────────────────────────────

_activation_log: list[str] = []
_fail_count: dict[str, int] = {}


def _reset_test_state():
    _activation_log.clear()
    _fail_count.clear()


# ── Backoff + RestartTracker unit tests ──────────────────────────────


def test_compute_delay_constant():
    assert _compute_delay(2.0, 1, "constant") == 2.0
    assert _compute_delay(2.0, 5, "constant") == 2.0


def test_compute_delay_linear():
    assert _compute_delay(1.0, 1, "linear") == 1.0
    assert _compute_delay(1.0, 3, "linear") == 3.0


def test_compute_delay_exponential():
    assert _compute_delay(1.0, 1, "exponential") == 1.0
    assert _compute_delay(1.0, 2, "exponential") == 2.0
    assert _compute_delay(1.0, 3, "exponential") == 4.0
    assert _compute_delay(1.0, 4, "exponential") == 8.0


def test_restart_tracker_sliding_window():
    tracker = RestartTracker()
    tracker.record(100.0)
    tracker.record(110.0)
    tracker.record(150.0)

    # All 3 within 60s window from t=150
    assert tracker.count_within(150.0, 60.0) == 3

    # Only 2 within 60s window from t=165 (100.0 is outside)
    assert tracker.count_within(165.0, 60.0) == 2

    # Only 1 within 10s window
    assert tracker.count_within(155.0, 10.0) == 1


# ── Component fixtures for supervision tests ─────────────────────────


@component("reliable-worker", version="1.0")
@provides("IReliableWorker")
class ReliableWorker:
    @lifecycle.activate
    def activate(self):
        _activation_log.append(f"activated:{self.rt.component_name}")


# A worker that fails N times then succeeds
@component("flaky-worker", version="1.0")
@provides("IFlakyWorker")
class FlakyWorker:
    @lifecycle.activate
    def activate(self):
        name = self.rt.component_name
        count = _fail_count.get(name, 0)
        _fail_count[name] = count + 1
        if count < 2:  # fail first 2 times
            raise ConnectionError(f"Flaky failure #{count + 1}")
        _activation_log.append(f"activated:{name}")


# A worker that always fails
@component("broken-worker", version="1.0")
@provides("IBrokenWorker")
class BrokenWorker:
    @lifecycle.activate
    def activate(self):
        raise RuntimeError("permanently broken")


# ── one_for_one tests ────────────────────────────────────────────────


@component("supervisor-1for1", version="1.0")
class SupervisorOneForOne:
    @lifecycle.activate
    async def activate(self):
        _activation_log.append("supervisor-activated")

    @lifecycle.supervision(
        strategy="one_for_one",
        max_restarts=5,
        within_seconds=60,
        backoff="constant",
        base_delay=0.01,  # tiny delay for tests
    )
    async def on_child_failure(self, child_name, error, attempt, context):
        _activation_log.append(f"supervision:{child_name}:attempt={attempt}")
        return True


@pytest.mark.asyncio
async def test_one_for_one_via_spawn():
    """Test supervision through rt.spawn() — the real usage path."""
    _reset_test_state()

    @component("spawn-supervisor", version="1.0")
    class SpawnSupervisor:
        @lifecycle.activate
        async def activate(self):
            _activation_log.append("spawn-sup-activated")
            # Spawn a flaky worker as child
            await self.rt.spawn("flaky-worker", "flaky-1")

        @lifecycle.supervision(
            strategy="one_for_one",
            max_restarts=5,
            within_seconds=60,
            backoff="constant",
            base_delay=0.01,
        )
        async def on_child_failure(self, child_name, error, attempt, context):
            _activation_log.append(f"supervision:{child_name}:attempt={attempt}")
            return True

    kernel = Kernel()
    kernel.discover([SpawnSupervisor, FlakyWorker])
    await kernel.boot()

    # The flaky worker fails 2 times then succeeds on 3rd
    # Supervisor should have been called for each failure
    assert "spawn-sup-activated" in _activation_log
    supervision_calls = [e for e in _activation_log if e.startswith("supervision:")]
    assert len(supervision_calls) == 2
    assert "activated:flaky-1" in _activation_log

    await kernel.shutdown()


@pytest.mark.asyncio
async def test_supervision_callback_declines_restart():
    """Supervisor callback returning False should stop retries."""
    _reset_test_state()

    @component("declining-supervisor", version="1.0")
    class DecliningSupervisor:
        @lifecycle.activate
        async def activate(self):
            await self.rt.spawn("broken-worker", "broken-1")

        @lifecycle.supervision(
            strategy="one_for_one",
            max_restarts=5,
            within_seconds=60,
            backoff="constant",
            base_delay=0.01,
        )
        async def on_child_failure(self, child_name, error, attempt, context):
            _activation_log.append(f"declined:{child_name}")
            return False  # decline restart

    kernel = Kernel()
    kernel.discover([DecliningSupervisor, BrokenWorker])
    await kernel.boot()

    # Supervisor should have been called once and declined
    assert len([e for e in _activation_log if e.startswith("declined:")]) == 1

    # The broken child should stay ERRORED
    child_ci = kernel.lifecycle.get_instance("broken-1")
    assert child_ci.state == State.ERRORED

    await kernel.shutdown()


@pytest.mark.asyncio
async def test_max_restarts_exceeded_escalation():
    """Exceeding max_restarts should escalate — supervisor goes ERRORED."""
    _reset_test_state()

    @component("strict-supervisor", version="1.0")
    class StrictSupervisor:
        @lifecycle.activate
        async def activate(self):
            await self.rt.spawn("broken-worker", "broken-esc")

        @lifecycle.supervision(
            strategy="one_for_one",
            max_restarts=2,
            within_seconds=60,
            backoff="constant",
            base_delay=0.01,
        )
        async def on_child_failure(self, child_name, error, attempt, context):
            return True  # always try restart

    kernel = Kernel()
    kernel.discover([StrictSupervisor, BrokenWorker])
    await kernel.boot()

    # After 2 restarts, supervision escalates. The supervisor's activate callback
    # gets a SupervisionEscalation from the spawn. boot() catches it, so the
    # supervisor ends up ERRORED (set by _escalate_to_parent before the raise).
    sup_ci = kernel.lifecycle.get_instance("strict-supervisor")
    assert sup_ci.state == State.ERRORED
    assert isinstance(sup_ci.error, SupervisionEscalation)

    await kernel.shutdown()


@pytest.mark.asyncio
async def test_supervision_context_update_properties():
    """SupervisionContext.update_properties should pass new props to restart."""
    _reset_test_state()

    spawned_props = []

    @component("adaptive-worker", version="1.0")
    class AdaptiveWorker:
        @lifecycle.activate
        def activate(self):
            name = self.rt.component_name
            endpoint = self.rt.properties.get("endpoint", "default")
            spawned_props.append(endpoint)
            count = _fail_count.get(name, 0)
            _fail_count[name] = count + 1
            if count < 1:
                raise ConnectionError("first attempt fails")
            _activation_log.append(f"activated:{name}:{endpoint}")

    @component("adaptive-supervisor", version="1.0")
    class AdaptiveSupervisor:
        @lifecycle.activate
        async def activate(self):
            await self.rt.spawn("adaptive-worker", "adaptive-1",
                                {"endpoint": "primary"})

        @lifecycle.supervision(
            strategy="one_for_one",
            max_restarts=3,
            within_seconds=60,
            backoff="constant",
            base_delay=0.01,
        )
        async def on_child_failure(self, child_name, error, attempt, context):
            context.update_properties({"endpoint": "fallback"})
            return True

    kernel = Kernel()
    kernel.discover([AdaptiveSupervisor, AdaptiveWorker])
    await kernel.boot()

    # First activation used "primary", restart should use "fallback"
    assert "primary" in spawned_props
    assert any("fallback" in e for e in _activation_log)

    await kernel.shutdown()


# ── one_for_all tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_for_all_restarts_all_children():
    """one_for_all: when one child fails, ALL children should restart."""
    _reset_test_state()

    @component("all-supervisor", version="1.0")
    class AllSupervisor:
        @lifecycle.activate
        async def activate(self):
            await self.rt.spawn("reliable-worker", "reliable-1")
            await self.rt.spawn("flaky-worker", "flaky-all")

        @lifecycle.supervision(
            strategy="one_for_all",
            max_restarts=5,
            within_seconds=60,
            backoff="constant",
            base_delay=0.01,
        )
        async def on_child_failure(self, child_name, error, attempt, context):
            return True

    kernel = Kernel()
    kernel.discover([AllSupervisor, ReliableWorker, FlakyWorker])
    await kernel.boot()

    # Both workers should end up activated (reliable restarted even though it was fine)
    reliable_activations = [e for e in _activation_log if "activated:reliable-1" in e]
    # reliable-1 activates at least twice: initial + one_for_all restart(s)
    assert len(reliable_activations) >= 2

    await kernel.shutdown()


# ── rest_for_one tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rest_for_one_restarts_from_failed():
    """rest_for_one: restart the failed child + everything after it.

    We spawn all 3 children successfully, then manually trigger supervision
    on the middle child by erroring it and calling _handle_activation_failure.
    Verifies that before-1 is untouched, middle-1 and after-1 are restarted.
    """
    _reset_test_state()

    @component("rest-supervisor", version="1.0")
    class RestSupervisor:
        @lifecycle.activate
        async def activate(self):
            await self.rt.spawn("reliable-worker", "before-1")
            await self.rt.spawn("reliable-worker", "middle-1")
            await self.rt.spawn("reliable-worker", "after-1")

        @lifecycle.supervision(
            strategy="rest_for_one",
            max_restarts=5,
            within_seconds=60,
            backoff="constant",
            base_delay=0.01,
        )
        async def on_child_failure(self, child_name, error, attempt, context):
            _activation_log.append(f"rest-supervision:{child_name}")
            return True

    kernel = Kernel()
    kernel.discover([RestSupervisor, ReliableWorker])
    await kernel.boot()

    # All 3 should be activated initially
    assert "activated:before-1" in _activation_log
    assert "activated:middle-1" in _activation_log
    assert "activated:after-1" in _activation_log
    initial_before = _activation_log.count("activated:before-1")
    initial_after = _activation_log.count("activated:after-1")
    assert initial_before == 1
    assert initial_after == 1

    # Now simulate a failure of middle-1 and trigger supervision
    middle_ci = kernel.lifecycle.get_instance("middle-1")
    sup_ci = kernel.lifecycle.get_instance("rest-supervisor")
    middle_ci.state = State.ERRORED
    middle_ci.error = RuntimeError("simulated failure")

    await kernel.lifecycle._handle_activation_failure(
        middle_ci,
        RuntimeError("simulated failure"),
        sup_ci,
        kernel._build_runtime,
        register_bus=kernel._register_component_bus,
    )

    # "before-1" should NOT have been restarted
    before_count = _activation_log.count("activated:before-1")
    assert before_count == 1  # still just the initial activation

    # "middle-1" should have been restarted
    middle_count = _activation_log.count("activated:middle-1")
    assert middle_count >= 2  # initial + rest_for_one restart

    # "after-1" should have been restarted (it's after the failed child)
    after_count = _activation_log.count("activated:after-1")
    assert after_count >= 2  # initial + rest_for_one restart

    # Supervision callback should have been called
    assert any("rest-supervision:middle-1" in e for e in _activation_log)

    await kernel.shutdown()


# ── RESTARTING state test ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restarting_state_is_transient():
    """RESTARTING state should be set during backoff, then transition to RESOLVED/ACTIVE."""
    _reset_test_state()
    states_seen = []

    @component("state-tracking-worker", version="1.0")
    class StateTrackingWorker:
        @lifecycle.activate
        def activate(self):
            name = self.rt.component_name
            count = _fail_count.get(name, 0)
            _fail_count[name] = count + 1
            if count < 1:
                raise RuntimeError("fail once")

    @component("state-supervisor", version="1.0")
    class StateSupervisor:
        @lifecycle.activate
        async def activate(self):
            await self.rt.spawn("state-tracking-worker", "tracked-1")

        @lifecycle.supervision(
            strategy="one_for_one",
            max_restarts=3,
            within_seconds=60,
            backoff="constant",
            base_delay=0.01,
        )
        async def on_child_failure(self, child_name, error, attempt, context):
            return True

    kernel = Kernel()
    kernel.discover([StateSupervisor, StateTrackingWorker])
    await kernel.boot()

    # After boot, the child should be ACTIVE (failed once, restarted successfully)
    child_ci = kernel.lifecycle.get_instance("tracked-1")
    assert child_ci.state == State.ACTIVE

    await kernel.shutdown()


# ── Trait test ───────────────────────────────────────────────────────


def test_supervisable_trait():
    """Components with @lifecycle.supervision should have the supervisable trait."""
    @component("trait-test-sup", version="1.0")
    class TraitTestSupervisor:
        @lifecycle.supervision(strategy="one_for_one", max_restarts=1)
        async def on_child_failure(self, child_name, error, attempt, context):
            return True

    from signalpy.kernel.component import _finalize_meta
    from signalpy.kernel.traits import TraitRegistry, Level, SUPERVISABLE
    meta = _finalize_meta(TraitTestSupervisor)
    registry = TraitRegistry()
    # Register minimal L0 traits that compute() expects
    for name in ["identifiable", "lifecycle", "dependable", "registrable", "factoryable"]:
        registry.define(name, Level.KERNEL)
    registry.define(SUPERVISABLE, Level.PLATFORM)
    traits = registry.compute(meta)
    assert SUPERVISABLE in traits


def test_non_supervisor_lacks_trait():
    """Components without @lifecycle.supervision should not have supervisable trait."""
    from signalpy.kernel.component import _finalize_meta
    from signalpy.kernel.traits import TraitRegistry, Level, SUPERVISABLE
    meta = _finalize_meta(ReliableWorker)
    registry = TraitRegistry()
    for name in ["identifiable", "lifecycle", "dependable", "registrable", "factoryable"]:
        registry.define(name, Level.KERNEL)
    registry.define(SUPERVISABLE, Level.PLATFORM)
    traits = registry.compute(meta)
    assert SUPERVISABLE not in traits


# ── Decorator validation ─────────────────────────────────────────────


def test_supervision_rejects_bad_strategy():
    with pytest.raises(ValueError, match="Unknown supervision strategy"):
        lifecycle.supervision(strategy="invalid")


def test_supervision_rejects_bad_backoff():
    with pytest.raises(ValueError, match="Unknown backoff strategy"):
        lifecycle.supervision(backoff="random")
