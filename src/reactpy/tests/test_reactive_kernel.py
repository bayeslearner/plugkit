"""Tests for the Reactive Kernel v2.

Covers: reactive primitives, component model, kernel boot/shutdown,
reactive propagation, reduced decorator surface, FastAPI integration.
"""
import asyncio
import pytest
from typing import Protocol, runtime_checkable
from pydantic import BaseModel

from reactpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    api, computed, effect, prop, subscribe, kind, skill, exportable,
    Signal, batch,
)
from reactpy.kernel.reactive import Computed, Effect, untracked, dispose_all
from reactpy.kernel.registry import ServiceRegistry
from reactpy.kernel.runtime import Runtime
from reactpy.kernel.bus import Bus
from reactpy.kernel.lifecycle_manager import LifecycleManager, State
from reactpy.kernel.traits import TraitRegistry, Level


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
# 2. Component Model — Reduced Surface
# ══════════════════════════════════════════════════════════════════

class TestRequiresUnified:
    def test_single(self):
        @component("test-single")
        @requires(config="IConfig")
        class C: pass
        from reactpy.kernel.component import get_meta, _finalize_meta
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
        from reactpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(C)
        meta = get_meta(C)
        req = next(r for r in meta.requirements if r.attr_name == "items")
        assert req.aggregate is True
        assert req.contract == "IFoo"

    def test_map_via_key(self):
        @component("test-map")
        @requires(dicts="IDictionary", key="language")
        class C: pass
        from reactpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(C)
        meta = get_meta(C)
        req = next(r for r in meta.requirements if r.attr_name == "dicts")
        assert req.aggregate is True
        assert req.key == "language"

    def test_optional(self):
        @component("test-opt")
        @requires(cache="ICache", optional=True)
        class C: pass
        from reactpy.kernel.component import get_meta, _finalize_meta
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
        from reactpy.kernel.component import get_meta, _finalize_meta
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
        from reactpy.kernel.component import get_meta, _finalize_meta
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
        from reactpy.providers.config import ConfigProvider
        from reactpy.providers.logging_provider import LoggingProvider
        from reactpy.providers.credentials import CredentialProvider
        from reactpy.providers.storage import StorageProvider
        from reactpy.providers.gateway import APIGateway
        from reactpy.adapters.rest import RESTTransport

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
