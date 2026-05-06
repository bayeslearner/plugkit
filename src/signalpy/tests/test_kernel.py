"""Tests for the kernel core: lifecycle, registry, bus, component model, runtime."""
import asyncio
import pytest
from pydantic import BaseModel

from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle
from signalpy.kernel.bus import Bus
from signalpy.kernel.registry import ServiceRegistry
from signalpy.kernel.lifecycle_manager import LifecycleManager, State
from signalpy.kernel.runtime import Runtime
from signalpy.kernel.traits import TraitRegistry, Level


# ── Fixtures ───────────────────────────────────────────────────────


class EchoParams(BaseModel):
    message: str = "hello"


@component("leaf", version="1.0")
@provides("ILeaf")
class LeafComponent:
    @lifecycle.activate
    def activate(self, rt):
        self.activated = True

    @lifecycle.deactivate
    def deactivate(self, rt):
        self.activated = False


@component("mid", version="1.0", depends=["leaf"])
@provides("IMid")
@requires(leaf="ILeaf")
class MidComponent:
    @lifecycle.activate
    def activate(self, rt):
        self.leaf_ref = rt.leaf

    @runnable("echo", params=EchoParams, description="Echo a message")
    async def echo(self, rt, params):
        msg = params.get("message", "hello") if isinstance(params, dict) else "hello"
        return {"echo": msg}


@component("top", version="1.0", depends=["mid"])
@requires(mid="IMid")
class TopComponent:
    @lifecycle.activate
    def activate(self, rt):
        self.mid_ref = rt.mid


@component("internal-test", version="1.0")
@provides("IInternalTest")
class InternalRunnableComponent:
    @runnable("public_op", params=BaseModel, description="Public operation")
    async def public_op(self, rt, params):
        return {"public": True}

    @runnable("_private_op", params=BaseModel, internal=True, description="Internal only")
    async def private_op(self, rt, params):
        return {"internal": True}


@component("multi-api", version="1.0",
           rest={"prefix": "/multi", "version": "v1"},
           mcp={"name": "multi-tools"})
@provides("IMultiApi")
class MultiApiComponent:
    @runnable("action", params=BaseModel, description="Do something")
    async def action(self, rt, params):
        return {"done": True}


# ── TraitRegistry ──────────────────────────────────────────────────


class TestTraitRegistry:
    def test_define_and_get(self):
        tr = TraitRegistry()
        td = tr.define("test-trait", Level.APP, description="A test trait")
        assert td.name == "test-trait"
        assert td.level == Level.APP
        assert tr.get("test-trait") is td

    def test_duplicate_raises(self):
        tr = TraitRegistry()
        tr.define("dup", Level.KERNEL)
        with pytest.raises(ValueError, match="already defined"):
            tr.define("dup", Level.KERNEL)

    def test_unknown_raises(self):
        tr = TraitRegistry()
        with pytest.raises(KeyError, match="Unknown trait"):
            tr.get("nonexistent")

    def test_by_level(self):
        tr = TraitRegistry()
        tr.define("k1", Level.KERNEL)
        tr.define("a1", Level.APP)
        tr.define("k2", Level.KERNEL)
        kernel_traits = tr.by_level(Level.KERNEL)
        assert len(kernel_traits) == 2
        assert all(t.level == Level.KERNEL for t in kernel_traits)

    def test_contains_and_len(self):
        tr = TraitRegistry()
        tr.define("x", Level.PLATFORM)
        assert "x" in tr
        assert "y" not in tr
        assert len(tr) == 1


# ── ServiceRegistry ───────────────────────────────────────────────


class TestServiceRegistry:
    def test_provide_and_require(self):
        reg = ServiceRegistry()
        obj = {"impl": True}
        reg.provide("IFoo", obj, "foo-provider")
        assert reg.require("IFoo") is obj

    def test_require_missing_raises(self):
        reg = ServiceRegistry()
        with pytest.raises(KeyError, match="No service provides"):
            reg.require("INothing")

    def test_require_optional_returns_none(self):
        reg = ServiceRegistry()
        assert reg.require_optional("INothing") is None

    def test_require_all(self):
        reg = ServiceRegistry()
        reg.provide("IFoo", "a", "p1")
        reg.provide("IFoo", "b", "p2")
        result = reg.require_all("IFoo")
        assert result == ["a", "b"]

    def test_unprovide(self):
        reg = ServiceRegistry()
        entry = reg.provide("IFoo", "x", "p1")
        assert reg.has("IFoo")
        reg.unprovide(entry)
        assert not reg.has("IFoo")

    def test_listener_notification(self):
        reg = ServiceRegistry()
        events = []
        reg.on_change(lambda event, entry: events.append((event, entry.contract)))
        reg.provide("IFoo", "x", "p1")
        assert events == [("provide", "IFoo")]

    def test_query(self):
        reg = ServiceRegistry()
        reg.provide("IFoo", "a", "p1")
        reg.provide("IBar", "b", "p2")
        all_entries = reg.query()
        assert len(all_entries) == 2
        foo_entries = reg.query("IFoo")
        assert len(foo_entries) == 1


# ── Event Bus ──────────────────────────────────────────────────────


class TestEventBus:
    @pytest.mark.asyncio
    async def test_publish_subscribe(self):
        bus = Bus()
        received = []
        bus.subscribe("test.event", lambda et, data: received.append(data))
        await bus.publish("test.event", {"value": 42})
        assert received == [{"value": 42}]

    def test_event_types(self):
        bus = Bus()
        bus.subscribe("a", lambda et, d: None)
        bus.subscribe("b", lambda et, d: None)
        assert set(bus.event_types) == {"a", "b"}

    def test_unsubscribe(self):
        bus = Bus()
        handler = lambda et, d: None
        bus.subscribe("ev", handler)
        assert "ev" in bus.event_types
        bus.unsubscribe("ev", handler)


class TestKernelGraph:
    """Phase 0c: kernel.graph() introspection."""

    @pytest.mark.asyncio
    async def test_graph_has_components_and_edges(self):
        from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle
        from signalpy.providers.config import ConfigProvider
        from signalpy.providers.logging_provider import LoggingProvider

        @component("test-app", version="1.0")
        @requires(config="IConfig")
        @provides("ITestService")
        class TestApp:
            @lifecycle.activate
            def activate(self): pass

            @runnable("do_thing", params=None, description="Does a thing")
            async def do_thing(self, params):
                return {"ok": True}

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, TestApp])
        await kernel.boot()

        g = kernel.graph()
        assert "test-app" in g["components"]
        assert g["components"]["test-app"]["provides"] == ["ITestService"]
        assert g["components"]["test-app"]["requires"] == {"config": "IConfig"}

        # Dependency edge: config → test-app via IConfig
        dep_edges = g["edges"]["dependencies"]
        config_to_test = [e for e in dep_edges if e["to"] == "test-app" and e["contract"] == "IConfig"]
        assert len(config_to_test) >= 1

        # Bus handler registered with schema
        bus_handlers = g["runnables"]["entries"]
        test_handler = [h for h in bus_handlers if h["target"] == "test-app.do_thing"]
        assert len(test_handler) == 1
        assert test_handler[0]["description"] == "Does a thing"

        await kernel.shutdown()


# ── LifecycleManager ──────────────────────────────────────────────


class TestLifecycleManager:
    def test_register_factory(self):
        lm = LifecycleManager()
        meta = lm.register_factory(LeafComponent)
        assert meta.factory_name == "leaf"

    def test_duplicate_factory_raises(self):
        lm = LifecycleManager()
        lm.register_factory(LeafComponent)
        with pytest.raises(ValueError, match="already registered"):
            lm.register_factory(LeafComponent)

    def test_instantiate(self):
        lm = LifecycleManager()
        lm.register_factory(LeafComponent)
        ci = lm.instantiate("leaf")
        assert ci.name == "leaf"
        assert ci.state == State.DISCOVERED

    def test_dependency_order(self):
        lm = LifecycleManager()
        lm.register_factory(TopComponent)
        lm.register_factory(MidComponent)
        lm.register_factory(LeafComponent)
        lm.instantiate("top")
        lm.instantiate("mid")
        lm.instantiate("leaf")
        order = lm.resolve_all()
        assert order.index("leaf") < order.index("mid")
        assert order.index("mid") < order.index("top")

    def test_circular_dependency_raises(self):
        @component("circ-a", depends=["circ-b"])
        class CircA: pass

        @component("circ-b", depends=["circ-a"])
        class CircB: pass

        lm = LifecycleManager()
        lm.register_factory(CircA)
        lm.register_factory(CircB)
        lm.instantiate("circ-a")
        lm.instantiate("circ-b")
        with pytest.raises(RuntimeError, match="Circular dependency"):
            lm.resolve_all()


# ── Runtime ────────────────────────────────────────────────────────


class TestRuntime:
    def test_inject_and_access(self):
        bus = Bus()
        rt = Runtime(
            component_name="test",
            factory_name="test",
            properties={},
            _bus=bus,
        )
        rt.inject("config", {"key": "value"})
        assert rt.config == {"key": "value"}

    def test_missing_service_raises(self):
        bus = Bus()
        rt = Runtime(
            component_name="test",
            factory_name="test",
            properties={},
            _bus=bus,
        )
        with pytest.raises(AttributeError, match="has no service"):
            _ = rt.nonexistent

    # rt.invoke policy tests removed — rt.invoke removed in spec 011.
    # Component-to-component calls use @requires + direct method calls.


# ── Full Kernel Integration ───────────────────────────────────────


class TestKernelIntegration:
    @pytest.mark.asyncio
    async def test_boot_and_shutdown(self):
        kernel = Kernel()
        kernel.discover([LeafComponent, MidComponent, TopComponent])
        await kernel.boot()

        status = kernel.status()
        assert len(status["components"]) == 3
        assert all(c["state"] == "ACTIVE" for c in status["components"])

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_bus_invocation(self):
        kernel = Kernel()
        kernel.discover([LeafComponent, MidComponent])
        await kernel.boot()

        result = await kernel.invoke("mid.echo", {"message": "test"})
        assert result == {"echo": "test"}

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_service_injection(self):
        kernel = Kernel()
        kernel.discover([LeafComponent, MidComponent])
        await kernel.boot()

        mid_instance = kernel.lifecycle.get_instance("mid")
        assert mid_instance.instance.leaf_ref is not None

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_internal_runnable_not_in_api(self):
        kernel = Kernel()
        kernel.discover([InternalRunnableComponent])
        await kernel.boot()

        # Internal runnable IS on the bus
        assert kernel.get_schema("internal-test._private_op") is not None
        # Public runnable is also on the bus
        assert kernel.get_schema("internal-test.public_op") is not None

        # But internal should be excluded from api_runnables
        from signalpy.kernel.component import get_meta
        meta = get_meta(InternalRunnableComponent)
        # Without @api, api_runnables returns empty for any transport
        assert meta.api_runnables("rest") == []

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_multi_api_declarations(self):
        from signalpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(MultiApiComponent)
        meta = get_meta(MultiApiComponent)
        assert meta.has_api("rest")
        assert meta.has_api("mcp")
        assert not meta.has_api("cli")
        rest_runnables = meta.api_runnables("rest")
        assert len(rest_runnables) == 1
        assert rest_runnables[0].name == "action"

    # test_policy_enforcement removed — rt.invoke removed in spec 011.
    # Policy enforcement is now per-consumer (transport adapter).

    @pytest.mark.asyncio
    async def test_params_validation(self):
        kernel = Kernel()
        kernel.discover([LeafComponent, MidComponent])
        await kernel.boot()

        # Valid params — should work and use defaults from model
        result = await kernel.invoke("mid.echo", {})
        assert result == {"echo": "hello"}

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_reverse_order(self):
        order = []

        @component("first")
        class First:
            @lifecycle.deactivate
            def deactivate(self, rt):
                order.append("first")

        @component("second", depends=["first"])
        class Second:
            @lifecycle.deactivate
            def deactivate(self, rt):
                order.append("second")

        kernel = Kernel()
        kernel.discover([First, Second])
        await kernel.boot()
        await kernel.shutdown()

        assert order == ["second", "first"]
