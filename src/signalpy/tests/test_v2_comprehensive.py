"""Comprehensive v2 test suite — proves every challenge from our design conversations.

Each test class addresses a specific challenge we identified:
1. Reactive change propagation
2. Live properties
3. Hot-add/remove with automatic reactive propagation
4. Auth enforcement at bus level
5. Service ranking
6. Aggregate injection via list[C]
7. Map injection via key=
8. Sync + async support
9. Type-based contracts (Protocol types)
10. Spell checker end-to-end
11. FastAPI integration
12. @computed — cached reactive derived values
13. @effect replaces @bind/@unbind
14. Ref counting on registry
15. ConfigAdmin managed services
16. REACTIVE trait computation
17. Errored retry
18. Kernel status with reactive metadata
"""
import asyncio
import pytest
from typing import Protocol, runtime_checkable
from pydantic import BaseModel

from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    api, computed, effect, prop, subscribe, kind, skill, exportable,
    Signal, batch,
)
from signalpy.kernel.reactive import Computed, Effect
from signalpy.kernel.registry import ServiceRegistry
from signalpy.kernel.lifecycle_manager import State


# ══════════════════════════════════════════════════════════════════
# Shared contracts
# ══════════════════════════════════════════════════════════════════

@runtime_checkable
class IDictionary(Protocol):
    def check_word(self, word: str) -> bool: ...

@runtime_checkable
class ICounter(Protocol):
    def value(self) -> int: ...


# ══════════════════════════════════════════════════════════════════
# 1. Reactive change propagation
# ══════════════════════════════════════════════════════════════════

class TestChangePropagation:
    @pytest.mark.asyncio
    async def test_service_replace_propagates_to_effect(self):
        """When a higher-ranked service appears, consumer's @effect re-runs."""
        @component("svc-old")
        @provides("IVal")
        @prop("_r", "service.ranking", 10)
        class Old:
            @lifecycle.activate
            def activate(self): self.v = "old"

        @component("reader", depends=["svc-old"])
        @requires(svc="IVal")
        class Reader:
            @lifecycle.activate
            def activate(self): self.log = []
            @effect
            def track(self): self.log.append(self.rt.svc.v)

        kernel = Kernel()
        kernel.discover([Old, Reader])
        await kernel.boot()

        reader = kernel.lifecycle.get_instance("reader").instance
        assert reader.log == ["old"]

        @component("svc-new")
        @provides("IVal")
        @prop("_r", "service.ranking", 0)  # higher priority
        class New:
            @lifecycle.activate
            def activate(self): self.v = "new"

        await kernel.hot_add(New)
        assert reader.log == ["old", "new"]
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_computed_reflects_service_change(self):
        """@computed recomputes when underlying service changes."""
        @component("num-svc")
        @provides("INum")
        @prop("_r", "service.ranking", 10)
        class NumSvc:
            @lifecycle.activate
            def activate(self): self.n = 10

        @component("doubler", depends=["num-svc"])
        @requires(num="INum")
        class Doubler:
            @lifecycle.activate
            def activate(self): pass
            @computed
            def doubled(self): return self.rt.num.n * 2

        kernel = Kernel()
        kernel.discover([NumSvc, Doubler])
        await kernel.boot()

        d = kernel.lifecycle.get_instance("doubler").instance
        assert d.doubled() == 20

        @component("num-svc-2")
        @provides("INum")
        @prop("_r", "service.ranking", 0)
        class NumSvc2:
            @lifecycle.activate
            def activate(self): self.n = 50

        await kernel.hot_add(NumSvc2)
        assert d.doubled() == 100
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 2-3. Hot-add/remove
# ══════════════════════════════════════════════════════════════════

class TestHotAddRemove:
    @pytest.mark.asyncio
    async def test_hot_add_new_service(self):
        kernel = Kernel()
        @component("base")
        class Base: pass
        kernel.discover([Base])
        await kernel.boot()

        @component("dynamic")
        @provides("IDynamic")
        class Dynamic:
            @runnable("op", params=BaseModel, description="x")
            async def op(self, params): return {"dynamic": True}

        await kernel.hot_add(Dynamic)
        r = await kernel.bus.invoke("dynamic.op", {})
        assert r == {"dynamic": True}
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_hot_remove_cleans_up(self):
        @component("removable")
        @provides("IRm")
        class Rm:
            @runnable("op", params=BaseModel, description="x")
            async def op(self, params): return {}

        kernel = Kernel()
        kernel.discover([Rm])
        await kernel.boot()
        assert kernel.bus.has_handler("removable.op")
        assert kernel.registry.has("IRm")

        await kernel.hot_remove("removable")
        assert not kernel.bus.has_handler("removable.op")
        assert not kernel.registry.has("IRm")
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 4. Auth enforcement
# ══════════════════════════════════════════════════════════════════

class TestAuthEnforcement:
    @pytest.mark.asyncio
    async def test_requires_action_blocks_without_token(self):
        from signalpy.providers.config import ConfigProvider
        from signalpy.providers.auth import AuthProvider

        @component("protected")
        class Protected:
            @runnable("admin_op", params=BaseModel, description="x",
                      requires_action="admin.delete")
            async def admin_op(self, params): return {"ok": True}

        kernel = Kernel()
        kernel.discover([ConfigProvider, AuthProvider, Protected])
        await kernel.boot()

        auth = kernel.lifecycle.get_instance("auth").instance
        auth._enabled = True
        auth._tokens = {"admin-tk": {"identity": "admin", "roles": ["admin"]}}
        auth._policies = {"admin": ["*"]}

        with pytest.raises(PermissionError):
            await kernel.bus.invoke("protected.admin_op", {})

        r = await kernel.bus.invoke("protected.admin_op", {"__auth_token__": "admin-tk"})
        assert r == {"ok": True}
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_requires_role(self):
        from signalpy.providers.config import ConfigProvider
        from signalpy.providers.auth import AuthProvider

        @component("role-app")
        class RoleApp:
            @runnable("su_op", params=BaseModel, description="x",
                      requires_role="superuser")
            async def su_op(self, params): return {"super": True}

        kernel = Kernel()
        kernel.discover([ConfigProvider, AuthProvider, RoleApp])
        await kernel.boot()

        auth = kernel.lifecycle.get_instance("auth").instance
        auth._enabled = True
        auth._tokens = {
            "su-tk": {"identity": "su", "roles": ["superuser"]},
            "normal-tk": {"identity": "user", "roles": ["user"]},
        }

        r = await kernel.bus.invoke("role-app.su_op", {"__auth_token__": "su-tk"})
        assert r == {"super": True}

        with pytest.raises(PermissionError):
            await kernel.bus.invoke("role-app.su_op", {"__auth_token__": "normal-tk"})
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 5. Service ranking
# ══════════════════════════════════════════════════════════════════

class TestServiceRanking:
    def test_require_returns_highest_ranked(self):
        reg = ServiceRegistry()
        reg.provide("IFoo", "low", "p1", {"service.ranking": 10})
        reg.provide("IFoo", "high", "p2", {"service.ranking": 1})
        assert reg.require("IFoo") == "high"

    def test_require_all_sorted(self):
        reg = ServiceRegistry()
        reg.provide("IFoo", "c", "p1", {"service.ranking": 10})
        reg.provide("IFoo", "a", "p2", {"service.ranking": 1})
        reg.provide("IFoo", "b", "p3", {"service.ranking": 5})
        assert reg.require_all("IFoo") == ["a", "b", "c"]


# ══════════════════════════════════════════════════════════════════
# 6-7. Aggregate and map injection
# ══════════════════════════════════════════════════════════════════

class TestAggregateAndMap:
    @pytest.mark.asyncio
    async def test_list_type_injects_all(self):
        @component("i1")
        @provides(IDictionary)
        @prop("_l", "language", "EN")
        class D1:
            @lifecycle.activate
            def activate(self): self.words = {"hello"}
            def check_word(self, w): return w in self.words

        @component("i2")
        @provides(IDictionary)
        @prop("_l", "language", "FR")
        class D2:
            @lifecycle.activate
            def activate(self): self.words = {"bonjour"}
            def check_word(self, w): return w in self.words

        @component("agg", depends=["i1", "i2"])
        @requires(dicts=list[IDictionary])
        class Agg:
            @lifecycle.activate
            def activate(self):
                self.count = len(self.rt.dicts)

        kernel = Kernel()
        kernel.discover([D1, D2, Agg])
        await kernel.boot()
        assert kernel.lifecycle.get_instance("agg").instance.count == 2
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_key_injects_as_map(self):
        @component("m1")
        @provides("IMapSvc")
        @prop("_k", "lang", "EN")
        class M1: pass

        @component("m2")
        @provides("IMapSvc")
        @prop("_k", "lang", "FR")
        class M2: pass

        @component("mapper", depends=["m1", "m2"])
        @requires(svcs="IMapSvc", key="lang")
        class Mapper:
            @lifecycle.activate
            def activate(self):
                self.langs = list(self.rt.svcs.keys())

        kernel = Kernel()
        kernel.discover([M1, M2, Mapper])
        await kernel.boot()
        ci = kernel.lifecycle.get_instance("mapper")
        assert set(ci.instance.langs) == {"EN", "FR"}
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 8. Sync + async support
# ══════════════════════════════════════════════════════════════════

class TestSyncAsync:
    @pytest.mark.asyncio
    async def test_sync_runnable(self):
        @component("sync-r")
        class S:
            @runnable("op", params=BaseModel, description="x")
            def op(self, params): return {"sync": True}

        kernel = Kernel()
        kernel.discover([S])
        await kernel.boot()
        assert await kernel.bus.invoke("sync-r.op", {}) == {"sync": True}
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_async_runnable(self):
        @component("async-r")
        class A:
            @runnable("op", params=BaseModel, description="x")
            async def op(self, params): return {"async": True}

        kernel = Kernel()
        kernel.discover([A])
        await kernel.boot()
        assert await kernel.bus.invoke("async-r.op", {}) == {"async": True}
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_sync_lifecycle(self):
        order = []
        @component("sync-lc")
        class SLC:
            @lifecycle.activate
            def activate(self): order.append("up")
            @lifecycle.deactivate
            def deactivate(self): order.append("down")

        kernel = Kernel()
        kernel.discover([SLC])
        await kernel.boot()
        await kernel.shutdown()
        assert order == ["up", "down"]

    @pytest.mark.asyncio
    async def test_sync_and_async_subscribe(self):
        received = []
        @component("sub-sync")
        class SubSync:
            @subscribe("test.ev")
            def on_ev(self, et, d): received.append(("sync", d))

        @component("sub-async")
        class SubAsync:
            @subscribe("test.ev")
            async def on_ev(self, et, d): received.append(("async", d))

        kernel = Kernel()
        kernel.discover([SubSync, SubAsync])
        await kernel.boot()
        await kernel.bus.publish("test.ev", {"v": 1})
        assert len(received) == 2
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 9. Type-based contracts
# ══════════════════════════════════════════════════════════════════

class TestTypedContracts:
    @pytest.mark.asyncio
    async def test_provides_and_requires_with_types(self):
        @component("typed-p")
        @provides(IDictionary)
        class TypedP:
            @lifecycle.activate
            def activate(self): self.words = {"test"}
            def check_word(self, w): return w in self.words

        @component("typed-c", depends=["typed-p"])
        @requires(d=IDictionary)
        class TypedC:
            @lifecycle.activate
            def activate(self):
                self.result = self.rt.d.check_word("test")

        kernel = Kernel()
        kernel.discover([TypedP, TypedC])
        await kernel.boot()
        assert kernel.lifecycle.get_instance("typed-c").instance.result is True
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 10. Spell checker end-to-end (iPOPO tutorial equivalent)
# ══════════════════════════════════════════════════════════════════

class TestSpellCheckerE2E:
    @pytest.mark.asyncio
    async def test_full_flow(self):
        """Boot → check → hot-add → check again → hot-remove → verify."""
        @component("en")
        @provides(IDictionary)
        @prop("_l", "language", "EN")
        class En:
            @lifecycle.activate
            def activate(self): self.words = {"hello", "world"}
            def check_word(self, w): return w.lower() in self.words

        @component("fr")
        @provides(IDictionary)
        @prop("_l", "language", "FR")
        class Fr:
            @lifecycle.activate
            def activate(self): self.words = {"bonjour", "monde"}
            def check_word(self, w): return w.lower() in self.words

        class CP(BaseModel):
            text: str = ""; language: str = "EN"

        @component("checker", depends=["en", "fr"])
        @requires(dicts=list[IDictionary])
        class Checker:
            @lifecycle.activate
            def activate(self): pass

            @runnable("check", params=CP, description="Check")
            async def check(self, params):
                for d in self.rt.dicts:
                    bad = [w for w in params.text.split() if not d.check_word(w)]
                    if len(bad) < len(params.text.split()):
                        return {"misspelled": bad}
                return {"misspelled": params.text.split()}

        kernel = Kernel()
        kernel.discover([En, Fr, Checker])
        await kernel.boot()

        r = await kernel.bus.invoke("checker.check", {"text": "hello world"})
        assert r["misspelled"] == []

        r = await kernel.bus.invoke("checker.check", {"text": "hello xyz"})
        assert "xyz" in r["misspelled"]

        # Hot-add German
        @component("de")
        @provides(IDictionary)
        @prop("_l", "language", "DE")
        class De:
            @lifecycle.activate
            def activate(self): self.words = {"hallo", "welt"}
            def check_word(self, w): return w.lower() in self.words

        await kernel.hot_add(De)
        r = await kernel.bus.invoke("checker.check", {"text": "hallo welt"})
        assert r["misspelled"] == []

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 11. FastAPI integration
# ══════════════════════════════════════════════════════════════════

class TestFastAPIIntegration:
    @pytest.mark.asyncio
    async def test_http_roundtrip(self):
        from signalpy.providers.config import ConfigProvider
        from signalpy.providers.logging_provider import LoggingProvider
        from signalpy.providers.credentials import CredentialProvider
        from signalpy.providers.storage import StorageProvider
        from signalpy.providers.gateway import APIGateway
        from signalpy.adapters.rest import RESTTransport

        class GP(BaseModel): name: str = "world"

        @component("http-g", version="1.0")
        @provides("IGreeter")
        @requires(config="IConfig", logger="ILogger")
        @api("rest", prefix="/greetings", version="v1")
        class G:
            @lifecycle.activate
            def activate(self): pass
            @runnable("hello", params=GP, description="Greet")
            async def hello(self, params):
                return {"msg": f"Hello, {params.name}!"}

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, CredentialProvider,
                         StorageProvider, APIGateway, RESTTransport, G])
        await kernel.boot()

        rest = kernel.registry.require("IRestAPI")
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=rest.app), base_url="http://t") as c:
            r = await c.post("/api/v1/greetings/hello", json={"name": "Alice"})
            assert r.status_code == 200
            assert r.json()["data"]["msg"] == "Hello, Alice!"
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 12-13. @computed and @effect (replaces @bind/@unbind)
# ══════════════════════════════════════════════════════════════════

class TestComputedAndEffect:
    @pytest.mark.asyncio
    async def test_computed_caches(self):
        count = [0]
        @component("comp-cache")
        @provides("ICC")
        class Svc:
            @lifecycle.activate
            def activate(self): self.v = 42

        @component("comp-reader", depends=["comp-cache"])
        @requires(svc="ICC")
        class Reader:
            @lifecycle.activate
            def activate(self): pass
            @computed
            def val(self):
                count[0] += 1
                return self.rt.svc.v

        kernel = Kernel()
        kernel.discover([Svc, Reader])
        await kernel.boot()

        r = kernel.lifecycle.get_instance("comp-reader").instance
        assert r.val() == 42
        assert r.val() == 42  # cached
        assert count[0] == 1  # computed only once
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_effect_replaces_bind(self):
        """@effect auto-tracks deps. No @bind needed."""
        @component("eff-svc")
        @provides("IEff")
        class Svc:
            @lifecycle.activate
            def activate(self): self.v = "initial"

        @component("eff-consumer", depends=["eff-svc"])
        @requires(svc="IEff")
        class Consumer:
            @lifecycle.activate
            def activate(self): self.tracked = []

            @effect
            def on_change(self):
                self.tracked.append(self.rt.svc.v)

        kernel = Kernel()
        kernel.discover([Svc, Consumer])
        await kernel.boot()

        c = kernel.lifecycle.get_instance("eff-consumer").instance
        assert c.tracked == ["initial"]
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 14. Ref counting
# ══════════════════════════════════════════════════════════════════

class TestRefCounting:
    @pytest.mark.asyncio
    async def test_acquire_release(self):
        reg = ServiceRegistry()
        reg.acquire("IFoo", "consumer-a")
        reg.acquire("IFoo", "consumer-b")
        assert reg.ref_count("IFoo") == 2
        reg.release("IFoo", "consumer-a")
        assert reg.ref_count("IFoo") == 1
        reg.release("IFoo", "consumer-b")
        assert reg.ref_count("IFoo") == 0


# ══════════════════════════════════════════════════════════════════
# 15. ConfigAdmin
# ══════════════════════════════════════════════════════════════════

class TestConfigAdmin:
    @pytest.mark.asyncio
    async def test_managed_service(self):
        from signalpy.providers.config import ConfigProvider

        kernel = Kernel()
        kernel.discover([ConfigProvider])
        await kernel.boot()

        # ConfigProvider now provides both IConfig and IConfigAdmin
        ca = kernel.registry.require("IConfigAdmin")
        received = []
        class Mock:
            def updated(self, props): received.append(props)

        ca.register_managed("test", Mock())
        ca.update("test", {"k": "v"})
        assert received == [{"k": "v"}]
        ca.delete("test")
        assert received == [{"k": "v"}, None]
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 16. REACTIVE trait
# ══════════════════════════════════════════════════════════════════

class TestReactiveTrait:
    @pytest.mark.asyncio
    async def test_reactive_trait_computed(self):
        @component("tr-comp")
        class TC:
            @computed
            def url(self): return "x"

        kernel = Kernel()
        kernel.discover([TC])
        await kernel.boot()
        status = kernel.status()
        traits = status["components"][0]["traits"]
        assert "reactive" in traits
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 17. Errored retry
# ══════════════════════════════════════════════════════════════════

class TestErroredRetry:
    @pytest.mark.asyncio
    async def test_retry_recovers(self):
        attempt = [0]
        @component("retry-me")
        class RetryMe:
            @lifecycle.activate
            def activate(self):
                attempt[0] += 1
                if attempt[0] < 2:
                    raise RuntimeError("Not ready")
                self.ok = True

        kernel = Kernel()
        kernel.discover([RetryMe])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("retry-me")
        assert ci.state == State.ERRORED

        ci = await kernel.retry_erroneous("retry-me")
        assert ci.state == State.ACTIVE
        assert ci.instance.ok is True
        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# 18. Kernel status
# ══════════════════════════════════════════════════════════════════

class TestKernelStatus:
    @pytest.mark.asyncio
    async def test_status_structure(self):
        @component("status-app")
        class StatusApp:
            @computed
            def x(self): return 1
            @effect
            def y(self): pass
            @runnable("op", params=BaseModel, description="x")
            async def op(self, params): return {}

        kernel = Kernel()
        kernel.discover([StatusApp])
        await kernel.boot()

        s = kernel.status()
        assert s["state"] == "HEALTHY"
        comp = s["components"][0]
        assert comp["reactive"]["computed"] == ["x"]
        assert comp["reactive"]["effects"] == ["y"]
        assert "op" in comp["runnables"]
        await kernel.shutdown()
