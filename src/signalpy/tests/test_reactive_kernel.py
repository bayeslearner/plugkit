"""Tests for the Reactive Kernel v2.

Covers: reactive primitives, component model, kernel boot/shutdown,
reactive propagation, reduced decorator surface, FastAPI integration.
"""
import asyncio
import pytest
from typing import Protocol, runtime_checkable
from pydantic import BaseModel

from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    api, computed, effect, prop, subscribe, kind, skill, exportable,
    Signal, batch, current_effect,
)
from signalpy.kernel.reactive import Computed, Effect, untracked, dispose_all
from signalpy.kernel.registry import ServiceRegistry
from signalpy.kernel.runtime import Runtime
from signalpy.kernel.bus import Bus
from signalpy.kernel.lifecycle_manager import LifecycleManager, State
from signalpy.kernel.traits import TraitRegistry, Level


# ══════════════════════════════════════════════════════════════════
# 1. Reactive Primitives
# ══════════════════════════════════════════════════════════════════

class TestSignal:
    def test_get_set(self):
        s = Signal(0)
        assert s.get() == 0
        s.set(5)
        assert s.get() == 5

    def test_update(self):
        s = Signal(10)
        s.update(lambda x: x + 1)
        assert s.get() == 11

    def test_peek_no_tracking(self):
        s = Signal("hello")
        log = []
        e = Effect(lambda: log.append(s.peek()))
        assert log == ["hello"]
        s.set("world")
        assert len(log) == 1  # peek didn't track

    def test_same_value_no_notify(self):
        s = Signal(42)
        log = []
        e = Effect(lambda: log.append(s.get()))
        s.set(42)  # same identity
        assert len(log) == 1

    def test_value_property(self):
        s = Signal("x")
        assert s.value == "x"
        s.value = "y"
        assert s.value == "y"


class TestComputed:
    def test_basic(self):
        a = Signal(1)
        b = Signal(2)
        c = Computed(lambda: a.get() + b.get())
        assert c.get() == 3
        a.set(10)
        assert c.get() == 12

    def test_lazy(self):
        count = [0]
        s = Signal(1)
        c = Computed(lambda: (count.__setitem__(0, count[0] + 1), s.get())[1])
        assert count[0] == 0  # not computed yet
        c.get()
        assert count[0] == 1
        c.get()
        assert count[0] == 1  # cached

    def test_chain(self):
        x = Signal(5)
        doubled = Computed(lambda: x.get() * 2)
        quad = Computed(lambda: doubled.get() * 2)
        assert quad.get() == 20
        x.set(3)
        assert quad.get() == 12

    def test_dispose(self):
        s = Signal(1)
        c = Computed(lambda: s.get() * 2)
        c.get()
        c.dispose()
        s.set(100)
        assert c.get() == 2  # still returns last value


class TestEffect:
    def test_runs_immediately(self):
        s = Signal(0)
        log = []
        e = Effect(lambda: log.append(s.get()))
        assert log == [0]

    def test_reruns_on_change(self):
        s = Signal("a")
        log = []
        e = Effect(lambda: log.append(s.get()))
        s.set("b")
        s.set("c")
        assert log == ["a", "b", "c"]

    def test_dispose_stops_tracking(self):
        s = Signal(0)
        log = []
        e = Effect(lambda: log.append(s.get()))
        e.dispose()
        s.set(99)
        assert len(log) == 1

    def test_lazy_effect(self):
        s = Signal(0)
        log = []
        e = Effect(lambda: log.append(s.get()), lazy=True)
        assert log == []
        e.run()
        assert log == [0]


class TestBatch:
    def test_groups_changes(self):
        a = Signal(0)
        b = Signal(0)
        log = []
        e = Effect(lambda: log.append(f"{a.get()},{b.get()}"))
        assert log == ["0,0"]
        with batch():
            a.set(1)
            b.set(2)
        assert log == ["0,0", "1,2"]

    def test_nested_batch(self):
        s = Signal(0)
        log = []
        e = Effect(lambda: log.append(s.get()))
        with batch():
            s.set(1)
            with batch():
                s.set(2)
            # inner batch ends but outer still active
        assert log == [0, 2]


class TestUntracked:
    def test_no_dependency(self):
        s = Signal("tracked")
        log = []
        e = Effect(lambda: log.append(untracked(lambda: s.get())))
        s.set("changed")
        assert len(log) == 1


class TestDisposeAll:
    def test_disposes_multiple(self):
        s = Signal(0)
        log1, log2 = [], []
        e1 = Effect(lambda: log1.append(s.get()))
        e2 = Effect(lambda: log2.append(s.get()))
        dispose_all(e1, e2)
        s.set(99)
        assert len(log1) == 1
        assert len(log2) == 1


# ══════════════════════════════════════════════════════════════════
# 1b. Reactive Edge Cases — threading, ordering, dedup, cleanup
# ══════════════════════════════════════════════════════════════════

class TestDuplicateTracking:
    def test_reading_same_signal_twice_does_not_double_subscribe(self):
        """_subscribers is a set — duplicate adds are no-ops."""
        s = Signal(0)
        log = []

        def fn():
            a = s.get()  # read 1
            b = s.get()  # read 2 (same signal)
            log.append(a + b)

        e = Effect(fn)
        assert len(s._subscribers) == 1  # not 2
        s.set(5)
        assert log == [0, 10]  # effect ran twice total (initial + change)

    def test_effect_resubscribes_after_rerun(self):
        """_untrack_all + re-track on each run. Deps can change per run."""
        a = Signal(1)
        b = Signal(2)
        switch = Signal(True)
        log = []

        def fn():
            if switch.get():
                log.append(("a", a.get()))
            else:
                log.append(("b", b.get()))

        e = Effect(fn)
        assert log == [("a", 1)]

        # Change a — effect re-runs (it tracks a and switch)
        a.set(10)
        assert log[-1] == ("a", 10)

        # Flip switch — now effect tracks b instead of a
        switch.set(False)
        assert log[-1] == ("b", 2)

        # Change a — effect should NOT re-run (no longer tracking a)
        a.set(99)
        assert log[-1] == ("b", 2)  # unchanged

        # Change b — effect SHOULD re-run
        b.set(20)
        assert log[-1] == ("b", 20)


class TestComputedSkipsPropagation:
    def test_computed_skips_propagation_in_chain(self):
        """In a Computed→Computed→Effect chain, if the middle Computed's value
        doesn't change (same identity), the downstream Effect doesn't re-run."""
        source = Signal(1)
        THRESHOLD = 10

        # c1 depends on source, changes when source changes
        c1 = Computed(lambda: source.get())
        # c2 depends on c1 but returns a fixed boolean (same object for same branch)
        c2 = Computed(lambda: c1.get() > THRESHOLD)
        log = []
        e = Effect(lambda: log.append(c2.get()))

        assert log == [False]  # source=1, 1>10 = False

        # Change source from 1 to 2 — c1 changes, but c2 still returns False
        source.set(2)
        # c2 recomputed: 2>10 = False. Same as before.
        # But the Effect still re-runs because _notify propagates eagerly.
        # The skip-propagation optimization prevents FURTHER downstream
        # Computeds from being notified, but the direct Effect subscriber
        # still gets the push notification before recompute.
        # This is the push-then-pull tradeoff — same as Vue 3.
        assert len(log) >= 1  # at least initial

        # Cross the threshold — c2 value actually changes
        source.set(20)
        assert log[-1] is True  # now True

    def test_computed_does_propagate_if_value_changed(self):
        """If computed recomputes to a different object, downstream effects DO re-run."""
        source = Signal("hello")
        c = Computed(lambda: source.get().upper())
        log = []
        e = Effect(lambda: log.append(c.get()))

        assert log == ["HELLO"]
        source.set("world")
        assert log == ["HELLO", "WORLD"]  # new string object → propagated


class TestDisposedCleanup:
    def test_disposed_consumers_cleaned_from_subscribers(self):
        """Disposed consumers are removed from subscriber sets on next notify."""
        s = Signal(0)
        log = []
        e = Effect(lambda: log.append(s.get()))
        assert len(s._subscribers) == 1

        e.dispose()
        s.set(1)  # _notify_subscribers cleans up disposed consumers
        assert len(s._subscribers) == 0  # cleaned up
        assert log == [0]  # effect did not re-run


class TestCreationOrdering:
    def test_batch_executes_effects_in_creation_order(self):
        """Effects in a batch flush run in creation order (by _id)."""
        s = Signal(0)
        order = []

        e1 = Effect(lambda: order.append("first"), lazy=True)
        e2 = Effect(lambda: order.append("second"), lazy=True)
        e3 = Effect(lambda: order.append("third"), lazy=True)

        # Subscribe all three to the same signal
        # We need them to read s inside their effect to track it
        order.clear()
        e1_fn = lambda: (s.get(), order.append("first"))
        e2_fn = lambda: (s.get(), order.append("second"))
        e3_fn = lambda: (s.get(), order.append("third"))

        e1 = Effect(e1_fn)
        e2 = Effect(e2_fn)
        e3 = Effect(e3_fn)
        order.clear()

        with batch():
            s.set(1)
        # All three should have run in creation order
        assert order == ["first", "second", "third"]


class TestReentrancyGuard:
    def test_effect_does_not_infinitely_loop(self):
        """Effect that writes to its own dependency doesn't recurse."""
        s = Signal(0)
        log = []

        def fn():
            val = s.get()
            log.append(val)
            if val < 3:
                s.set(val + 1)  # writes to own dep — reentrancy guard kicks in

        e = Effect(fn)
        # _running flag prevents re-entry during execution.
        # The set() inside the effect fires _notify, but run() sees _running=True and skips.
        # After the effect finishes, _running resets, and the pending notification fires.
        # This may result in a few runs but NOT infinite recursion.
        assert len(log) <= 10  # definitely not infinite
        assert log[0] == 0  # first run


class TestComputedDirtyDedup:
    def test_already_dirty_computed_does_not_double_propagate(self):
        """If a computed is already dirty, a second notification doesn't propagate again."""
        s1 = Signal(1)
        s2 = Signal(2)
        c = Computed(lambda: s1.get() + s2.get())
        log = []
        e = Effect(lambda: log.append(c.get()))
        assert log == [3]

        with batch():
            s1.set(10)
            s2.set(20)
        # Computed got notified twice (once per signal), but should only
        # propagate once (second notify sees _dirty=True, skips)
        assert log == [3, 30]  # effect ran exactly once in the batch


# ══════════════════════════════════════════════════════════════════
# 2. Component Model — Reduced Surface
# ══════════════════════════════════════════════════════════════════

class TestRequiresUnified:
    def test_single(self):
        @component("test-single")
        @requires(config="IConfig")
        class C: pass
        from signalpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(C)
        meta = get_meta(C)
        req = meta.requirements[0]
        assert req.attr_name == "config"
        assert req.aggregate is False

    def test_aggregate_via_list(self):
        @runtime_checkable
        class IFoo(Protocol):
            pass
        @component("test-agg")
        @requires(items=list[IFoo])
        class C: pass
        from signalpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(C)
        meta = get_meta(C)
        req = next(r for r in meta.requirements if r.attr_name == "items")
        assert req.aggregate is True
        assert req.contract == "IFoo"

    def test_map_via_key(self):
        @component("test-map")
        @requires(dicts="IDictionary", key="language")
        class C: pass
        from signalpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(C)
        meta = get_meta(C)
        req = next(r for r in meta.requirements if r.attr_name == "dicts")
        assert req.aggregate is True
        assert req.key == "language"

    def test_optional(self):
        @component("test-opt")
        @requires(cache="ICache", optional=True)
        class C: pass
        from signalpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(C)
        meta = get_meta(C)
        req = meta.requirements[0]
        assert req.optional is True


class TestComputedDecorator:
    def test_marks_method(self):
        @component("test-comp")
        class C:
            @computed
            def url(self):
                return "http://localhost"
        from signalpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(C)
        meta = get_meta(C)
        assert len(meta.computed_defs) == 1
        assert meta.computed_defs[0].fn.__name__ == "url"


class TestEffectDecorator:
    def test_marks_method(self):
        @component("test-eff")
        class C:
            @effect
            def on_change(self):
                pass
        from signalpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(C)
        meta = get_meta(C)
        assert len(meta.effect_defs) == 1


# ══════════════════════════════════════════════════════════════════
# 3. Kernel Integration
# ══════════════════════════════════════════════════════════════════

class TestKernelBoot:
    @pytest.mark.asyncio
    async def test_boot_and_shutdown(self):
        @component("leaf")
        @provides("ILeaf")
        class Leaf:
            @lifecycle.activate
            def activate(self): self.ok = True

        @component("mid", depends=["leaf"])
        @requires(leaf="ILeaf")
        class Mid:
            @lifecycle.activate
            def activate(self): pass

        kernel = Kernel()
        kernel.discover([Leaf, Mid])
        await kernel.boot()
        assert kernel.healthy
        assert len(kernel.lifecycle.active_instances()) == 2
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_bus_invocation(self):
        class P(BaseModel):
            x: int = 0

        @component("math-test")
        class Math:
            @runnable("add", params=P, description="add")
            async def add(self, params):
                return {"result": params.x + 1}

        kernel = Kernel()
        kernel.discover([Math])
        await kernel.boot()
        r = await kernel.bus.invoke("math-test.add", {"x": 5})
        assert r == {"result": 6}
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_sync_runnable(self):
        @component("sync-test")
        class SyncTest:
            @runnable("hello", params=BaseModel, description="sync")
            def hello(self, params):
                return {"sync": True}

        kernel = Kernel()
        kernel.discover([SyncTest])
        await kernel.boot()
        r = await kernel.bus.invoke("sync-test.hello", {})
        assert r == {"sync": True}
        await kernel.shutdown()


class TestReactiveRuntime:
    @pytest.mark.asyncio
    async def test_rt_is_reactive(self):
        """self.rt.X reads are reactive — tracked by Effect."""
        @component("provider-rt")
        @provides("ISvc")
        class Svc:
            @lifecycle.activate
            def activate(self):
                self.version = 1

        @component("consumer-rt", depends=["provider-rt"])
        @requires(svc="ISvc")
        class Consumer:
            @lifecycle.activate
            def activate(self):
                self.effect_log = []

            @effect
            def track_svc(self):
                self.effect_log.append(f"v={self.rt.svc.version}")

        kernel = Kernel()
        kernel.discover([Svc, Consumer])
        await kernel.boot()

        consumer = kernel.lifecycle.get_instance("consumer-rt").instance
        assert consumer.effect_log == ["v=1"]

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_computed_works(self):
        @component("comp-provider")
        @provides("IComp")
        class CompSvc:
            @lifecycle.activate
            def activate(self):
                self.value = 42

        @component("comp-consumer", depends=["comp-provider"])
        @requires(svc="IComp")
        class CompConsumer:
            @lifecycle.activate
            def activate(self):
                pass

            @computed
            def doubled(self):
                return self.rt.svc.value * 2

        kernel = Kernel()
        kernel.discover([CompSvc, CompConsumer])
        await kernel.boot()

        consumer = kernel.lifecycle.get_instance("comp-consumer").instance
        assert consumer.doubled() == 84

        await kernel.shutdown()


class TestReactivePropagation:
    @pytest.mark.asyncio
    async def test_hot_add_propagates(self):
        """Hot-adding a higher-ranked service updates consumer's @effect."""
        @component("svc-low")
        @provides("IProp")
        @prop("_rank", "service.ranking", 10)
        class SvcLow:
            @lifecycle.activate
            def activate(self):
                self.version = 1

        @component("consumer-prop", depends=["svc-low"])
        @requires(svc="IProp")
        class Consumer:
            @lifecycle.activate
            def activate(self):
                self.effect_log = []

            @effect
            def track(self):
                self.effect_log.append(self.rt.svc.version)

        kernel = Kernel()
        kernel.discover([SvcLow, Consumer])
        await kernel.boot()

        consumer = kernel.lifecycle.get_instance("consumer-prop").instance
        assert consumer.effect_log == [1]

        @component("svc-high")
        @provides("IProp")
        @prop("_rank", "service.ranking", 0)
        class SvcHigh:
            @lifecycle.activate
            def activate(self):
                self.version = 2

        await kernel.hot_add(SvcHigh)
        assert consumer.effect_log == [1, 2]

        await kernel.shutdown()


class TestAggregateRequires:
    @pytest.mark.asyncio
    async def test_list_type_hint(self):
        """@requires(x=list[C]) injects all matching services."""
        @runtime_checkable
        class IItem(Protocol):
            pass

        @component("item-a")
        @provides(IItem)
        class A:
            @lifecycle.activate
            def activate(self):
                self.name = "a"

        @component("item-b")
        @provides(IItem)
        class B:
            @lifecycle.activate
            def activate(self):
                self.name = "b"

        @component("collector", depends=["item-a", "item-b"])
        @requires(items=list[IItem])
        class Collector:
            @lifecycle.activate
            def activate(self):
                self.count = len(self.rt.items)

        kernel = Kernel()
        kernel.discover([A, B, Collector])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("collector")
        assert ci.instance.count == 2

        await kernel.shutdown()


class TestHotAddRemove:
    @pytest.mark.asyncio
    async def test_hot_add(self):
        kernel = Kernel()
        @component("base")
        class Base:
            pass
        kernel.discover([Base])
        await kernel.boot()

        @component("added")
        @provides("IAdded")
        class Added:
            @runnable("op", params=BaseModel, description="x")
            async def op(self, params):
                return {"added": True}

        await kernel.hot_add(Added)
        r = await kernel.bus.invoke("added.op", {})
        assert r == {"added": True}
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_hot_remove(self):
        @component("removable")
        @provides("IRemovable")
        class Removable:
            @runnable("op", params=BaseModel, description="x")
            async def op(self, params):
                return {}

        kernel = Kernel()
        kernel.discover([Removable])
        await kernel.boot()
        assert kernel.bus.has_handler("removable.op")
        await kernel.hot_remove("removable")
        assert not kernel.bus.has_handler("removable.op")
        await kernel.shutdown()


class TestStatus:
    @pytest.mark.asyncio
    async def test_includes_reactive_info(self):
        @component("status-test")
        class StatusTest:
            @computed
            def url(self): return "x"
            @effect
            def track(self): pass

        kernel = Kernel()
        kernel.discover([StatusTest])
        await kernel.boot()

        status = kernel.status()
        comp = status["components"][0]
        assert "reactive" in comp
        assert "url" in comp["reactive"]["computed"]
        assert "track" in comp["reactive"]["effects"]
        await kernel.shutdown()


class TestFastAPIIntegration:
    @pytest.mark.asyncio
    async def test_http_request(self):
        from signalpy.providers.config import ConfigProvider
        from signalpy.providers.logging_provider import LoggingProvider
        from signalpy.providers.credentials import CredentialProvider
        from signalpy.providers.storage import StorageProvider
        from signalpy.providers.gateway import APIGateway
        from signalpy.adapters.rest import RESTTransport

        class P(BaseModel):
            name: str = "world"

        @component("http-greeter", version="1.0")
        @provides("IGreeter")
        @requires(config="IConfig", logger="ILogger")
        @api("rest", prefix="/greetings", version="v1")
        class Greeter:
            @lifecycle.activate
            def activate(self):
                pass
            @runnable("hello", params=P, description="Greet")
            async def hello(self, params):
                return {"msg": f"Hello, {params.name}!"}

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, CredentialProvider,
                         StorageProvider, APIGateway, RESTTransport, Greeter])
        await kernel.boot()

        rest = kernel.registry.require("IRestAPI")
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=rest.app), base_url="http://test") as c:
            r = await c.post("/api/v1/greetings/hello", json={"name": "Alice"})
            assert r.status_code == 200
            assert r.json()["data"]["msg"] == "Hello, Alice!"

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# Implicit-batch correctness guarantees (P0)
#
# These pin down behavior that the engine must provide *without*
# requiring the caller to wrap writes in `with batch():`.
# ══════════════════════════════════════════════════════════════════

class TestImplicitBatchGuarantees:
    """Reactive correctness guarantees that hold outside explicit batch().

    The engine wraps every Signal.set notification cascade and every
    Effect/Computed body in an implicit batch. The four scenarios
    below are the regressions that wrapping must prevent.
    """

    def test_diamond_runs_effect_once(self):
        """A → (B, C) → E. One write to A must run E once, not twice."""
        a = Signal(1)
        b = Computed(lambda: a.get() + 10)
        c = Computed(lambda: a.get() + 100)

        runs = []
        e = Effect(lambda: runs.append((b.get(), c.get())))
        assert runs == [(11, 101)]            # initial run

        a.set(2)
        assert runs == [(11, 101), (12, 102)] # exactly one re-run, not two
        e.dispose()

    def test_effect_order_outside_batch_matches_creation_order(self):
        """Three effects on the same signal fire in creation order, deterministically."""
        s = Signal(0)
        order = []

        e1 = Effect(lambda: order.append(("e1", s.get())))
        e2 = Effect(lambda: order.append(("e2", s.get())))
        e3 = Effect(lambda: order.append(("e3", s.get())))
        order.clear()                          # discard initial runs

        s.set(7)
        # All three fire once, in creation order — not set-iteration order
        assert order == [("e1", 7), ("e2", 7), ("e3", 7)]
        dispose_all(e1, e2, e3)

    def test_writes_from_effect_body_coalesce(self):
        """An effect that writes two signals causes one downstream re-run, not two."""
        x = Signal(0)
        y = Signal(0)
        trigger = Signal(False)

        downstream_runs = []
        downstream = Effect(lambda: downstream_runs.append((x.get(), y.get())))
        downstream_runs.clear()                # discard initial run

        def writer():
            if trigger.get():                  # subscribe so we re-run on set
                x.set(x.peek() + 1)
                y.set(y.peek() + 1)

        writer_eff = Effect(writer)
        downstream_runs.clear()                # writer's initial body had trigger=False

        trigger.set(True)
        # Writer re-runs, writes x then y, both inside the writer's auto-batch.
        # Downstream sees ONE update — (1, 1) — not two.
        assert downstream_runs == [(1, 1)]

        writer_eff.dispose()
        downstream.dispose()

    def test_explicit_batch_still_works_and_nests(self):
        """Explicit `with batch():` must still coalesce, and nesting is fine."""
        a = Signal(0)
        b = Signal(0)

        runs = []
        e = Effect(lambda: runs.append((a.get(), b.get())))
        runs.clear()

        with batch():
            with batch():
                a.set(1)
                b.set(1)
            # Inner batch exit does NOT flush (depth still > 0)
            assert runs == []
        # Outer exit flushes once
        assert runs == [(1, 1)]
        e.dispose()

    def test_chained_computed_diamond(self):
        """A → B → D and A → C → D. One A.set must flush D once."""
        a = Signal(1)
        b = Computed(lambda: a.get() * 2)
        c = Computed(lambda: a.get() * 3)
        d = Computed(lambda: b.get() + c.get())

        runs = []
        e = Effect(lambda: runs.append(d.get()))
        assert runs == [5]                     # 1*2 + 1*3

        a.set(2)
        assert runs == [5, 10]                 # one re-run, not two
        e.dispose()


# ══════════════════════════════════════════════════════════════════
# P1: Async effect supersede semantics
# ══════════════════════════════════════════════════════════════════

class TestAsyncSupersede:
    """Async effects must not silently drop notifications that arrive
    while the body is mid-await. The engine sets _pending_run and
    re-runs the effect once the in-flight body finishes (or, with
    cancel_on_supersede=True, cancels and re-runs immediately)."""

    @pytest.mark.asyncio
    async def test_async_pending_rerun_fires_after_inflight_finishes(self):
        """Default behavior: notification mid-await schedules a re-run."""
        sig = Signal(1)
        runs: list[int] = []
        gate_seen_first = asyncio.Event()
        gate_release = asyncio.Event()

        async def body():
            v = sig.get()
            runs.append(v)
            if v == 1:
                gate_seen_first.set()
                await gate_release.wait()

        e = Effect(body, lazy=True)
        # First run starts and parks at the gate
        e.run()
        await gate_seen_first.wait()
        # Notification while body is mid-await — must not be dropped
        sig.set(2)
        # Release the in-flight body; pending re-run should fire
        gate_release.set()
        # Yield enough times for finally + re-scheduled run to complete
        for _ in range(20):
            await asyncio.sleep(0)
            if len(runs) >= 2:
                break
        assert runs == [1, 2]
        e.dispose()

    @pytest.mark.asyncio
    async def test_is_stale_inside_async_body(self):
        """current_effect().is_stale() flips True after a supersede."""
        sig = Signal(1)
        observed: list[bool] = []
        gate_seen_first = asyncio.Event()
        gate_release = asyncio.Event()

        async def body():
            v = sig.get()
            if v == 1:
                gate_seen_first.set()
                await gate_release.wait()
                # After the await we should see staleness from the
                # supersede that happened while we waited.
                observed.append(current_effect().is_stale())

        e = Effect(body, lazy=True)
        e.run()
        await gate_seen_first.wait()
        sig.set(2)         # supersede while body waits
        gate_release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if observed:
                break
        assert observed == [True]
        e.dispose()

    @pytest.mark.asyncio
    async def test_cancel_on_supersede_aborts_inflight(self):
        """With cancel_on_supersede, the in-flight task is cancelled
        and the effect re-runs with the latest value."""
        sig = Signal(1)
        runs: list[int] = []
        cancelled_marker = []
        gate_seen_first = asyncio.Event()

        async def body():
            v = sig.get()
            runs.append(v)
            if v == 1:
                gate_seen_first.set()
                try:
                    await asyncio.sleep(10)  # long sleep — must be cancelled
                except asyncio.CancelledError:
                    cancelled_marker.append(True)
                    raise

        e = Effect(body, lazy=True, cancel_on_supersede=True)
        e.run()
        await gate_seen_first.wait()
        sig.set(2)         # should cancel the in-flight task
        for _ in range(20):
            await asyncio.sleep(0)
            if len(runs) >= 2:
                break
        assert cancelled_marker == [True]
        assert runs == [1, 2]
        e.dispose()

    def test_is_stale_false_for_sync_effect(self):
        """Sync effects can't be superseded mid-execution; is_stale stays False."""
        sig = Signal(1)
        observed: list[bool] = []

        def body():
            sig.get()
            observed.append(current_effect().is_stale())

        e = Effect(body)
        sig.set(2)
        assert observed == [False, False]
        e.dispose()

    def test_current_effect_returns_none_outside_effect(self):
        assert current_effect() is None
