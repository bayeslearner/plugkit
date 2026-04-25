"""Coverage gap tests — targeting uncovered lines across kernel modules.

Organized by module. Each section targets specific missing lines identified
by pytest --cov-report=term-missing.
"""
import asyncio
import pytest
from typing import Protocol, runtime_checkable, Any
from pydantic import BaseModel

from reactpy.kernel import (
    Kernel, component, provides, requires, requires_aggregate, requires_best,
    runnable, lifecycle, api, subscribe, bind, unbind, prop, kind, skill,
    exportable,
)
from reactpy.kernel.bus import Bus, BusTransport
from reactpy.kernel.registry import ServiceRegistry
from reactpy.kernel.runtime import Runtime
from reactpy.kernel.lifecycle_manager import LifecycleManager, State
from reactpy.kernel.contracts import (
    IConfig, ICredentials, IStorage, IAuth, ILogger, ITracer, IWorkspace, IConfigAdmin,
)


# ══════════════════════════════════════════════════════════════════════
# kernel/contracts.py — verify Protocols are runtime_checkable
# ══════════════════════════════════════════════════════════════════════

class TestContracts:
    def test_all_protocols_are_runtime_checkable(self):
        """Every contract Protocol should be @runtime_checkable."""
        for proto in [IConfig, ICredentials, IStorage, IAuth, ILogger, ITracer, IWorkspace, IConfigAdmin]:
            assert isinstance(proto, type)
            assert getattr(proto, "_is_runtime_protocol", False), f"{proto.__name__} not runtime_checkable"

    def test_iconfig_satisfied_by_dict_like(self):
        class FakeConfig:
            def get(self, key, default=None): return default
        assert isinstance(FakeConfig(), IConfig)

    def test_ilogger_satisfied_by_impl(self):
        class FakeLogger:
            def info(self, msg, **kw): pass
            def warning(self, msg, **kw): pass
            def error(self, msg, **kw): pass
            def debug(self, msg, **kw): pass
        assert isinstance(FakeLogger(), ILogger)

    def test_iauth_satisfied_by_impl(self):
        class FakeAuth:
            def authenticate(self, token): return {}
            def authorize(self, identity, action): return True
        assert isinstance(FakeAuth(), IAuth)

    def test_icredentials_satisfied_by_impl(self):
        class FakeCreds:
            def get(self, key, *, target=None): return ""
            def for_target(self, target): return {}
            def list_targets(self): return []
        assert isinstance(FakeCreds(), ICredentials)

    def test_iconfigadmin_satisfied_by_impl(self):
        class FakeCA:
            def get_configuration(self, pid): return {}
            def update(self, pid, properties): pass
            def delete(self, pid): pass
        assert isinstance(FakeCA(), IConfigAdmin)


# ══════════════════════════════════════════════════════════════════════
# kernel/bus.py — remote transport, unsubscribe, event_types
# ══════════════════════════════════════════════════════════════════════

class TestBusCoverage:
    @pytest.mark.asyncio
    async def test_bus_transport_invoke_not_implemented(self):
        t = BusTransport(name="test")
        with pytest.raises(NotImplementedError):
            await t.invoke("x", {})

    @pytest.mark.asyncio
    async def test_bus_transport_publish_not_implemented(self):
        t = BusTransport(name="test")
        with pytest.raises(NotImplementedError):
            await t.publish("x", {})

    @pytest.mark.asyncio
    async def test_remote_transport_fallback(self):
        """When no local handler, bus tries remote transports."""
        bus = Bus()
        results = []

        class MockTransport(BusTransport):
            async def invoke(self, target, params):
                results.append(target)
                return {"remote": True}

        bus.add_transport(MockTransport(name="mock"))
        result = await bus.invoke("remote.target", {"a": 1})
        assert result == {"remote": True}
        assert results == ["remote.target"]

    @pytest.mark.asyncio
    async def test_remote_transport_publish(self):
        """Bus.publish forwards to remote transports."""
        bus = Bus()
        published = []

        class MockTransport(BusTransport):
            async def publish(self, event_type, data):
                published.append((event_type, data))

        bus.add_transport(MockTransport(name="mock"))
        await bus.publish("test.event", {"v": 1})
        assert published == [("test.event", {"v": 1})]

    @pytest.mark.asyncio
    async def test_remote_transport_not_implemented_skipped(self):
        """If a transport raises NotImplementedError, bus tries next."""
        bus = Bus()

        class FailTransport(BusTransport):
            async def invoke(self, target, params):
                raise NotImplementedError

        class SuccessTransport(BusTransport):
            async def invoke(self, target, params):
                return {"ok": True}

        bus.add_transport(FailTransport(name="fail"))
        bus.add_transport(SuccessTransport(name="success"))
        result = await bus.invoke("x", {})
        assert result == {"ok": True}

    def test_unsubscribe(self):
        bus = Bus()
        handler = lambda et, d: None
        bus.subscribe("ev", handler)
        assert "ev" in bus.event_types
        bus.unsubscribe("ev", handler)

    def test_event_types_property(self):
        bus = Bus()
        bus.subscribe("a", lambda et, d: None)
        bus.subscribe("b", lambda et, d: None)
        assert set(bus.event_types) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_target_routing_via_bus(self):
        """L3 target routing: factory.runnable + target param → instance.runnable."""
        bus = Bus()

        async def prod_handler(p):
            return {"env": "prod"}

        async def dev_handler(p):
            return {"env": "dev"}

        bus.register_handler("splunk-prod.query", prod_handler)
        bus.register_handler("splunk-dev.query", dev_handler)
        bus.register_target_route("splunk.query", "prod", "splunk-prod.query")
        bus.register_target_route("splunk.query", "dev", "splunk-dev.query")

        result = await bus.invoke("splunk.query", {"target": "prod"})
        assert result == {"env": "prod"}

        result = await bus.invoke("splunk.query", {"target": "dev"})
        assert result == {"env": "dev"}


# ══════════════════════════════════════════════════════════════════════
# kernel/runtime.py — publish, on, spawn error, bus property, audit
# ══════════════════════════════════════════════════════════════════════

class TestRuntimeCoverage:
    @pytest.mark.asyncio
    async def test_publish_allowed(self):
        bus = Bus()
        published = []
        bus.subscribe("test", lambda et, d: published.append(d))
        rt = Runtime(
            component_name="test", factory_name="test",
            properties={}, _bus=bus,
        )
        await rt.publish("test", {"v": 1})
        assert published == [{"v": 1}]

    @pytest.mark.asyncio
    async def test_publish_denied(self):
        bus = Bus()
        rt = Runtime(
            component_name="test", factory_name="test",
            properties={}, _bus=bus,
            _publish_allow=["safe.*"],
        )
        with pytest.raises(PermissionError, match="cannot publish"):
            await rt.publish("dangerous.event")

    @pytest.mark.asyncio
    async def test_publish_with_audit(self):
        bus = Bus()
        rt = Runtime(
            component_name="audited", factory_name="audited",
            properties={}, _bus=bus,
            _audit=True,
        )
        # Should not raise, just log
        await rt.publish("test.event", {})

    def test_on_subscribes(self):
        bus = Bus()
        rt = Runtime(
            component_name="test", factory_name="test",
            properties={}, _bus=bus,
        )
        received = []
        rt.on("my.event", lambda et, d: received.append(d))
        assert "my.event" in bus.event_types

    def test_bus_property(self):
        bus = Bus()
        rt = Runtime(
            component_name="test", factory_name="test",
            properties={}, _bus=bus,
        )
        assert rt.bus is bus

    def test_bus_handlers_property(self):
        bus = Bus()
        async def h(p): pass
        bus.register_handler("x.y", h)
        rt = Runtime(
            component_name="test", factory_name="test",
            properties={}, _bus=bus,
        )
        assert "x.y" in rt.bus_handlers

    def test_services_property(self):
        bus = Bus()
        rt = Runtime(
            component_name="test", factory_name="test",
            properties={}, _bus=bus,
        )
        rt.inject("config", {"k": "v"})
        assert rt.services == {"config": {"k": "v"}}

    @pytest.mark.asyncio
    async def test_spawn_without_callback_raises(self):
        bus = Bus()
        rt = Runtime(
            component_name="test", factory_name="test",
            properties={}, _bus=bus,
            _spawn=None,
        )
        with pytest.raises(RuntimeError, match="spawn not available"):
            await rt.spawn("factory")

    @pytest.mark.asyncio
    async def test_invoke_with_audit(self):
        bus = Bus()
        async def h(p): return "ok"
        bus.register_handler("x", h)
        rt = Runtime(
            component_name="audited", factory_name="audited",
            properties={}, _bus=bus,
            _audit=True,
        )
        result = await rt.invoke("x")
        assert result == "ok"


# ══════════════════════════════════════════════════════════════════════
# kernel/__init__.py — drain, scoped wrappers, sync runnables, etc.
# ══════════════════════════════════════════════════════════════════════

class TestKernelCoverage:
    @pytest.mark.asyncio
    async def test_drain(self):
        """kernel.drain() transitions to DRAINING and completes."""
        kernel = Kernel()

        @component("drainable")
        class Drainable:
            pass

        kernel.discover([Drainable])
        await kernel.boot()
        assert kernel.healthy

        await kernel.drain(timeout_s=1.0)
        assert kernel.state.name == "DRAINING"

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_sync_runnable_via_bus(self):
        """Sync runnables (def, not async def) work through the bus."""
        @component("sync-rbl")
        class SyncRunnable:
            @runnable("hello", params=BaseModel, description="sync")
            def hello(self, params):
                return {"sync": True}

        kernel = Kernel()
        kernel.discover([SyncRunnable])
        await kernel.boot()

        result = await kernel.bus.invoke("sync-rbl.hello", {})
        assert result == {"sync": True}

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_sync_lifecycle(self):
        """Sync activate/deactivate (no async) work."""
        order = []

        @component("sync-lc")
        class SyncLC:
            @lifecycle.activate
            def activate(self):
                order.append("activated")

            @lifecycle.deactivate
            def deactivate(self):
                order.append("deactivated")

        kernel = Kernel()
        kernel.discover([SyncLC])
        await kernel.boot()
        await kernel.shutdown()
        assert order == ["activated", "deactivated"]

    @pytest.mark.asyncio
    async def test_async_lifecycle(self):
        """Async activate/deactivate work."""
        order = []

        @component("async-lc")
        class AsyncLC:
            @lifecycle.activate
            async def activate(self):
                order.append("activated")

            @lifecycle.deactivate
            async def deactivate(self):
                order.append("deactivated")

        kernel = Kernel()
        kernel.discover([AsyncLC])
        await kernel.boot()
        await kernel.shutdown()
        assert order == ["activated", "deactivated"]

    @pytest.mark.asyncio
    async def test_sync_subscribe_handler(self):
        """Sync subscribe handlers work."""
        received = []

        @component("sync-sub")
        class SyncSub:
            @subscribe("test.ev")
            def on_event(self, event_type, data):
                received.append(data)

        kernel = Kernel()
        kernel.discover([SyncSub])
        await kernel.boot()

        await kernel.bus.publish("test.ev", {"v": 1})
        assert received == [{"v": 1}]

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_async_subscribe_handler(self):
        """Async subscribe handlers work and are properly awaited."""
        received = []

        @component("async-sub")
        class AsyncSub:
            @subscribe("test.ev")
            async def on_event(self, event_type, data):
                received.append(data)

        kernel = Kernel()
        kernel.discover([AsyncSub])
        await kernel.boot()

        await kernel.bus.publish("test.ev", {"v": 2})
        assert received == [{"v": 2}]

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_exportable_decorator(self):
        """@exportable sets metadata on component."""
        @component("exported-app")
        @provides("IExported")
        @exportable(transport="jsonrpc", discovery="mdns")
        class ExportedApp:
            pass

        from reactpy.kernel.component import get_meta, _finalize_meta
        _finalize_meta(ExportedApp)
        meta = get_meta(ExportedApp)
        assert meta.exportable == {"transport": "jsonrpc", "discovery": "mdns"}

    @pytest.mark.asyncio
    async def test_kind_and_skill_registered(self):
        """@kind and @skill register in kernel.kinds and kernel.skills."""
        @component("kinded-app")
        @kind("alert", model=BaseModel, description="Alert schema")
        @skill("helper", content="# Help", triggers=["help"])
        class KindedApp:
            pass

        kernel = Kernel()
        kernel.discover([KindedApp])
        await kernel.boot()

        assert "alert" in kernel.kinds
        assert "helper" in kernel.skills

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_hot_remove_cleans_up(self):
        """hot_remove unregisters services and bus handlers."""
        @component("removable")
        @provides("IRemovable")
        class Removable:
            @runnable("op", params=BaseModel, description="x")
            async def op(self, params):
                return {"ok": True}

        kernel = Kernel()
        kernel.discover([Removable])
        await kernel.boot()

        assert kernel.bus.has_handler("removable.op")
        assert kernel.registry.has("IRemovable")

        await kernel.hot_remove("removable")
        assert not kernel.bus.has_handler("removable.op")
        assert not kernel.registry.has("IRemovable")

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════════
# kernel/lifecycle_manager.py — edge cases
# ══════════════════════════════════════════════════════════════════════

class TestLifecycleManagerCoverage:
    def test_instantiate_unknown_factory_raises(self):
        lm = LifecycleManager()
        with pytest.raises(TypeError, match="Unknown factory"):
            lm.instantiate("nonexistent")

    def test_instantiate_duplicate_name_raises(self):
        @component("dup-test")
        class DupTest:
            pass

        lm = LifecycleManager()
        lm.register_factory(DupTest)
        lm.instantiate("dup-test", "instance-1")
        with pytest.raises(ValueError, match="already exists"):
            lm.instantiate("dup-test", "instance-1")

    @pytest.mark.asyncio
    async def test_activate_wrong_state_raises(self):
        @component("bad-state")
        class BadState:
            pass

        lm = LifecycleManager()
        lm.register_factory(BadState)
        ci = lm.instantiate("bad-state")
        # State is DISCOVERED, not RESOLVED
        with pytest.raises(RuntimeError, match="Cannot activate"):
            await lm.activate("bad-state", lambda ci: None)

    @pytest.mark.asyncio
    async def test_retry_non_errored_raises(self):
        @component("not-errored")
        class NotErrored:
            pass

        lm = LifecycleManager()
        lm.register_factory(NotErrored)
        ci = lm.instantiate("not-errored")
        ci.state = State.RESOLVED

        def mock_rt(ci):
            from reactpy.kernel.runtime import Runtime
            from reactpy.kernel.bus import Bus
            return Runtime(component_name=ci.name, factory_name=ci.meta.factory_name,
                          properties={}, _bus=Bus())

        await lm.activate("not-errored", mock_rt)
        assert ci.state == State.ACTIVE

        with pytest.raises(RuntimeError, match="not ERRORED"):
            await lm.retry_erroneous("not-errored", mock_rt)

    def test_get_instance_none(self):
        lm = LifecycleManager()
        assert lm.get_instance("nonexistent") is None

    def test_active_instances(self):
        lm = LifecycleManager()
        assert lm.active_instances() == []


# ══════════════════════════════════════════════════════════════════════
# kernel/registry.py — edge cases
# ══════════════════════════════════════════════════════════════════════

class TestScopedWrappers:
    """Tests for _ScopedCredentials and _ScopedStorage."""

    def test_scoped_credentials(self):
        from reactpy.kernel import _ScopedCredentials

        class FakeInner:
            def get(self, key, *, target=None, app=None):
                return f"{app}/{key}/{target}"
            def for_target(self, target, *, app=None):
                return {"app": app, "target": target}
            def list_targets(self, *, app=None):
                return [f"{app}-targets"]

        scoped = _ScopedCredentials(FakeInner(), "my-app")
        assert scoped.get("token", target="prod") == "my-app/token/prod"
        assert scoped.for_target("prod") == {"app": "my-app", "target": "prod"}
        assert scoped.list_targets() == ["my-app-targets"]

    @pytest.mark.asyncio
    async def test_scoped_storage(self):
        from reactpy.kernel import _ScopedStorage

        calls = []

        class FakeInner:
            async def put(self, key, data, prefix=None):
                calls.append(("put", key, prefix))
            async def get(self, key, prefix=None):
                calls.append(("get", key, prefix))
                return b"data"
            async def list(self, prefix=""):
                calls.append(("list", prefix))
                return ["a", "b"]
            async def delete(self, key, prefix=None):
                calls.append(("delete", key, prefix))

        scoped = _ScopedStorage(FakeInner(), "my-app")

        await scoped.put("file.txt", b"hello")
        assert calls[-1] == ("put", "file.txt", "my-app")

        result = await scoped.get("file.txt")
        assert result == b"data"
        assert calls[-1] == ("get", "file.txt", "my-app")

        await scoped.list("subdir")
        assert calls[-1] == ("list", "my-app/subdir")

        await scoped.list()
        assert calls[-1] == ("list", "my-app")

        await scoped.delete("file.txt")
        assert calls[-1] == ("delete", "file.txt", "my-app")


class TestParamsCoverage:
    def test_params_getattr(self):
        from reactpy.kernel import Params
        p = Params({"name": "Alice"})
        assert p.name == "Alice"

    def test_params_getattr_missing(self):
        from reactpy.kernel import Params
        p = Params({})
        with pytest.raises(AttributeError, match="has no field"):
            _ = p.nonexistent

    def test_params_setattr(self):
        from reactpy.kernel import Params
        p = Params({})
        p.name = "Bob"
        assert p["name"] == "Bob"


class TestKernelInitCoverage:
    @pytest.mark.asyncio
    async def test_discover_non_component_warns(self):
        """Discovering a non-@component class logs a warning and skips."""
        class NotAComponent:
            pass

        kernel = Kernel()
        kernel.discover([NotAComponent])  # should not raise
        assert len(kernel.lifecycle.all_instances()) == 0

    @pytest.mark.asyncio
    async def test_hot_add_non_component_raises(self):
        class NotAComponent:
            pass
        kernel = Kernel()
        with pytest.raises(TypeError, match="not a @component"):
            await kernel.hot_add(NotAComponent)

    @pytest.mark.asyncio
    async def test_hot_remove_nonexistent_raises(self):
        kernel = Kernel()
        with pytest.raises(KeyError, match="No instance"):
            await kernel.hot_remove("nonexistent")

    @pytest.mark.asyncio
    async def test_status_structure(self):
        @component("status-test")
        @provides("IST")
        class StatusTest:
            @runnable("op", params=BaseModel, description="x")
            async def op(self, params):
                return {}

        kernel = Kernel()
        kernel.discover([StatusTest])
        await kernel.boot()

        status = kernel.status()
        assert "components" in status
        assert "services" in status
        assert "bus_handlers" in status
        assert "kinds" in status
        assert "skills" in status
        comp = status["components"][0]
        assert "name" in comp
        assert "state" in comp
        assert "traits" in comp
        assert "runnables" in comp
        assert "provides" in comp

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_set_policy(self):
        kernel = Kernel()
        kernel.set_policy("test-app", {
            "invoke_allow": ["safe.*"],
            "invoke_deny": ["admin.*"],
            "audit": True,
        })
        assert "test-app" in kernel._policies


class TestStructuralScoping:
    """Tests for structural scoping — ILogger, ICredentials, IStorage get scoped."""

    @pytest.mark.asyncio
    async def test_logger_scoped_per_component(self):
        """ILogger is structurally scoped — each component gets its own logger."""
        from reactpy.providers.config import ConfigProvider
        from reactpy.providers.logging_provider import LoggingProvider

        @component("scoped-log-test", depends=["config", "logging"])
        @requires(logger="ILogger")
        class ScopedLogTest:
            @lifecycle.activate
            def activate(self):
                # logger should be a ComponentLogger, not the raw LoggingProvider
                self.logger_type = type(self.rt.logger).__name__

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, ScopedLogTest])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("scoped-log-test")
        assert ci.instance.logger_type == "ComponentLogger"

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_credentials_scoped_per_component(self):
        """ICredentials is structurally scoped — each component sees only its own."""
        from reactpy.providers.config import ConfigProvider
        from reactpy.providers.credentials import CredentialProvider

        @component("scoped-cred-test", depends=["config", "credentials"])
        @requires(creds="ICredentials")
        class ScopedCredTest:
            @lifecycle.activate
            def activate(self):
                self.cred_type = type(self.rt.creds).__name__

        kernel = Kernel()
        kernel.discover([ConfigProvider, CredentialProvider, ScopedCredTest])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("scoped-cred-test")
        assert ci.instance.cred_type == "_ScopedCredentials"

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_storage_scoped_per_component(self):
        """IStorage is structurally scoped — each component gets its own prefix."""
        from reactpy.providers.config import ConfigProvider
        from reactpy.providers.storage import StorageProvider

        @component("scoped-stor-test", depends=["config", "storage"])
        @requires(storage="IStorage")
        class ScopedStorTest:
            @lifecycle.activate
            def activate(self):
                self.stor_type = type(self.rt.storage).__name__

        kernel = Kernel()
        kernel.discover([ConfigProvider, StorageProvider, ScopedStorTest])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("scoped-stor-test")
        assert ci.instance.stor_type == "_ScopedStorage"

        await kernel.shutdown()


class TestOptionalAndMissingDeps:
    @pytest.mark.asyncio
    async def test_optional_single_requires_injects_none(self):
        """Optional single requires injects None when no provider."""
        @component("opt-single")
        @requires_best("missing", "INonexistent", optional=True)
        class OptSingle:
            @lifecycle.activate
            def activate(self):
                self.val = self.rt.missing

        kernel = Kernel()
        kernel.discover([OptSingle])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("opt-single")
        assert ci.instance.val is None

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_missing_required_dep_logs_warning(self):
        """Missing non-optional dependency logs warning but doesn't crash boot."""
        @component("needs-missing")
        @requires(thing="INonexistentService")
        class NeedsMissing:
            @lifecycle.activate
            def activate(self):
                pass

        kernel = Kernel()
        kernel.discover([NeedsMissing])
        await kernel.boot()
        # Should still boot (warning logged)
        ci = kernel.lifecycle.get_instance("needs-missing")
        assert ci.state == State.ACTIVE

        await kernel.shutdown()


class TestUpdateInjection:
    @pytest.mark.asyncio
    async def test_aggregate_updates_on_hot_add(self):
        """_update_injection re-injects aggregate list when services change."""
        @component("agg-provider-1")
        @provides("IAgg")
        @prop("_n", "n", 1)
        class AggP1:
            pass

        @component("agg-consumer-x", depends=["agg-provider-1"])
        @requires_aggregate("items", "IAgg")
        class AggConsumer:
            @bind("items")
            def on_add(self, svc, props):
                pass  # trigger _update_injection

            @lifecycle.activate
            def activate(self):
                pass

        kernel = Kernel()
        kernel.discover([AggP1, AggConsumer])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("agg-consumer-x")
        assert len(ci.instance.rt.items) == 1

        @component("agg-provider-2")
        @provides("IAgg")
        @prop("_n", "n", 2)
        class AggP2:
            pass

        await kernel.hot_add(AggP2)
        # After hot-add, the bind callback + _update_injection should refresh
        assert len(ci.instance.rt.items) == 2

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_map_updates_on_hot_add(self):
        """_update_injection re-injects map dict when services change."""
        from reactpy.kernel import requires_map

        @component("map-prov-a")
        @provides("IMapSvc")
        @prop("_k", "key", "A")
        class MapA:
            pass

        @component("map-consumer-x")
        @requires_map("items", "IMapSvc", key="key")
        class MapConsumer:
            @bind("items")
            def on_add(self, svc, props):
                pass  # just trigger _update_injection

            @lifecycle.activate
            def activate(self):
                pass

        kernel = Kernel()
        kernel.discover([MapA, MapConsumer])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("map-consumer-x")
        assert "A" in ci.instance.rt.items

        @component("map-prov-b")
        @provides("IMapSvc")
        @prop("_k", "key", "B")
        class MapB:
            pass

        await kernel.hot_add(MapB)
        assert "B" in ci.instance.rt.items

        await kernel.shutdown()


class TestAsyncBind:
    @pytest.mark.asyncio
    async def test_async_bind_callback(self):
        """Async @bind callbacks work."""
        bound = []

        @component("async-bind-provider")
        @provides("IAsyncBind")
        @prop("_v", "val", "x")
        class Provider:
            pass

        @component("async-bind-consumer", depends=["async-bind-provider"])
        @requires_aggregate("items", "IAsyncBind")
        class Consumer:
            @lifecycle.activate
            def activate(self):
                pass

            @bind("items")
            async def on_bind(self, svc, props):
                bound.append(props.get("val"))

        kernel = Kernel()
        kernel.discover([Provider, Consumer])
        await kernel.boot()

        # Give the async task a moment to complete
        await asyncio.sleep(0.05)
        assert "x" in bound

        await kernel.shutdown()


class TestRunnableEdgeCases:
    @pytest.mark.asyncio
    async def test_runnable_with_no_params_model(self):
        """Runnable with params=None still works (no validation)."""
        @component("no-model")
        class NoModel:
            @runnable("op", params=None, description="no model")
            async def op(self, params):
                return {"got": dict(params) if params else {}}

        kernel = Kernel()
        kernel.discover([NoModel])
        await kernel.boot()

        result = await kernel.bus.invoke("no-model.op", {"x": 1})
        assert result == {"got": {"x": 1}}

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_sync_runnable_with_rt_param(self):
        """Sync runnable with (self, rt, params) legacy signature works."""
        @component("sync-rt")
        class SyncRt:
            @runnable("op", params=BaseModel, description="sync with rt")
            def op(self, rt, params):
                return {"name": rt.component_name}

        kernel = Kernel()
        kernel.discover([SyncRt])
        await kernel.boot()

        result = await kernel.bus.invoke("sync-rt.op", {})
        assert result == {"name": "sync-rt"}

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_runnable_params_model_without_model_validate(self):
        """Params model without model_validate (not Pydantic) still works."""
        class PlainParams:
            pass  # no model_validate method

        @component("plain-params")
        class PlainParamsComp:
            @runnable("op", params=PlainParams, description="plain")
            async def op(self, params):
                return {"type": type(params).__name__}

        kernel = Kernel()
        kernel.discover([PlainParamsComp])
        await kernel.boot()

        result = await kernel.bus.invoke("plain-params.op", {"x": 1})
        assert result["type"] == "Params"  # falls through to Params dict

        await kernel.shutdown()


class TestTraitsCoverage:
    def test_trait_registry_all(self):
        """TraitRegistry.all() returns all traits sorted by level then name."""
        from reactpy.kernel.traits import TraitRegistry, Level
        tr = TraitRegistry()
        tr.define("b", Level.APP)
        tr.define("a", Level.KERNEL)
        tr.define("c", Level.APP)
        result = tr.all()
        assert result[0].name == "a"  # KERNEL < APP
        assert result[1].name == "b"
        assert result[2].name == "c"

    def test_compute_exportable_trait(self):
        """Trait computation detects exportable."""
        from reactpy.kernel.component import ComponentMeta
        from reactpy.kernel.traits import TraitRegistry, Level

        tr = TraitRegistry()
        # Register the needed traits
        for name in ["identifiable", "lifecycle", "dependable", "registrable",
                     "factoryable", "inspectable", "exportable"]:
            tr.define(name, Level.KERNEL)

        meta = ComponentMeta(factory_name="test")
        meta.exportable = {"transport": "jsonrpc", "discovery": "mdns"}
        traits = tr.compute(meta)
        assert "exportable" in traits

    def test_compute_secured_via_auth(self):
        """SECURED trait computed from @requires(auth=IAuth)."""
        from reactpy.kernel.component import ComponentMeta
        from reactpy.kernel.traits import TraitRegistry, Level

        tr = TraitRegistry()
        for name in ["identifiable", "lifecycle", "dependable", "registrable",
                     "factoryable", "secured"]:
            tr.define(name, Level.KERNEL)

        meta = ComponentMeta(factory_name="test")
        meta.requires = {"auth": "IAuth"}
        traits = tr.compute(meta)
        assert "secured" in traits

    def test_compute_inspectable_trait(self):
        """Trait computation detects inspectable when health_fn exists."""
        from reactpy.kernel.component import ComponentMeta
        from reactpy.kernel.traits import TraitRegistry, Level

        tr = TraitRegistry()
        for name in ["identifiable", "lifecycle", "dependable", "registrable",
                     "factoryable", "inspectable"]:
            tr.define(name, Level.KERNEL)

        meta = ComponentMeta(factory_name="test")
        meta.health_fn = lambda self: {"ok": True}
        traits = tr.compute(meta)
        assert "inspectable" in traits


class TestRegistryCoverage:
    def test_require_map_skips_missing_key(self):
        reg = ServiceRegistry()
        reg.provide("IFoo", "a", "p1", {"lang": "EN"})
        reg.provide("IFoo", "b", "p2")  # no "lang" property
        result = reg.require_map("IFoo", "lang")
        assert result == {"EN": "a"}

    def test_require_for_missing_raises(self):
        reg = ServiceRegistry()
        with pytest.raises(KeyError):
            reg.require_for("INonexistent", "consumer")

    def test_unget_service_no_factory(self):
        """unget_service on non-factory does nothing."""
        reg = ServiceRegistry()
        reg.provide("IFoo", "svc", "p1")
        # Should not raise
        reg.unget_service("IFoo", "consumer")

    def test_unprovide_nonexistent(self):
        """Unproviding something that doesn't exist is a no-op."""
        reg = ServiceRegistry()
        from reactpy.kernel.registry import ServiceEntry
        entry = ServiceEntry(contract="IFoo", implementation="x", provider_name="p")
        reg.unprovide(entry)  # should not raise

    def test_require_with_filter_props(self):
        reg = ServiceRegistry()
        reg.provide("IFoo", "wrong", "p1", {"lang": "FR"})
        reg.provide("IFoo", "right", "p2", {"lang": "EN"})
        assert reg.require("IFoo", lang="EN") == "right"

    def test_require_filter_no_match_raises(self):
        reg = ServiceRegistry()
        reg.provide("IFoo", "only-fr", "p1", {"lang": "FR"})
        with pytest.raises(KeyError, match="No service provides"):
            reg.require("IFoo", lang="EN")
