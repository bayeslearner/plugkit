"""Tests for the trait system: auto-computation, L2 decorators, L3 targeting."""
import asyncio
import pytest
from pydantic import BaseModel

from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    subscribe, kind, skill, computed, effect,
)
from signalpy.kernel.traits import TraitRegistry, Level
from signalpy.kernel.component import _finalize_meta, get_meta
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider
from signalpy.providers.credentials import CredentialProvider
from signalpy.providers.storage import StorageProvider
from signalpy.providers.gateway import APIGateway


# ── Fixtures ───────────────────────────────────────────────────────

class ItemModel(BaseModel):
    name: str = ""
    value: int = 0


@component("trait-demo", version="2.0", depends=["config", "logging"],
           rest={"prefix": "/demo"})
@provides("ITraitDemo")
@requires(config="IConfig", logger="ILogger", storage="IStorage")
@kind("item", model=ItemModel, description="Demo item")
@skill("demo-guide", content="# How to use the demo", triggers=["demo", "help"])
class TraitDemoComponent:

    @lifecycle.activate
    def activate(self, rt):
        self._events: list = []

    @runnable("action", params=BaseModel, description="Do something")
    async def action(self, rt, params):
        return {"done": True}

    @subscribe("demo.event")
    async def on_event(self, event_type, data):
        self._events.append(data)


@component("bundled-app", version="1.0", depends=["config", "logging"],
           rest={"prefix": "/bundled"},
           mcp={"name": "bundled-tools"})
@provides("IBundled")
@requires(config="IConfig", logger="ILogger")
class BundledApp:
    @lifecycle.activate
    def activate(self):
        pass

    @runnable("ping", params=BaseModel, description="Ping")
    async def ping(self, params):
        return {"pong": True}


@component("target-demo", version="1.0", depends=["config"])
@provides("ITargetDemo")
@requires(config="IConfig")
class TargetDemoComponent:
    @lifecycle.activate
    def activate(self, rt):
        self._target = rt.properties.get("target", "default")

    @runnable("whoami", params=BaseModel, description="Which target am I?")
    async def whoami(self, rt, params):
        return {"target": self._target}


# ── Helper ─────────────────────────────────────────────────────────

async def boot_with(*extra_components, config_defaults=None):
    kernel = Kernel()
    components = [ConfigProvider, LoggingProvider, CredentialProvider,
                  StorageProvider, APIGateway, *extra_components]
    kernel.discover(components)
    if config_defaults:
        ci = kernel.instantiate("config")
        ci.properties = {"defaults": config_defaults}
    await kernel.boot()
    return kernel


# ── Trait auto-computation ─────────────────────────────────────────

class TestTraitComputation:
    async def test_l0_traits_always_present(self):
        kernel = await boot_with()
        status = kernel.status()
        for c in status["components"]:
            traits = c["traits"]
            assert "identifiable" in traits
            assert "lifecycle" in traits
            assert "dependable" in traits
            assert "registrable" in traits
            assert "factoryable" in traits
        await kernel.shutdown()

    async def test_l1_traits_from_requires(self):
        kernel = await boot_with(TraitDemoComponent)
        status = kernel.status()
        demo = next(c for c in status["components"] if c["name"] == "trait-demo")
        traits = demo["traits"]
        assert "configurable" in traits   # @requires(config="IConfig")
        assert "observable" in traits     # @requires(logger="ILogger")
        assert "storable" in traits       # @requires(storage="IStorage")
        assert "communicable" in traits   # has runnables
        await kernel.shutdown()

    async def test_l2_traits_from_decorators(self):
        kernel = await boot_with(TraitDemoComponent)
        status = kernel.status()
        demo = next(c for c in status["components"] if c["name"] == "trait-demo")
        traits = demo["traits"]
        assert "runnable" in traits       # @runnable
        assert "subscribable" in traits   # @subscribe
        assert "kinded" in traits         # @kind
        assert "skillful" in traits       # @skill
        assert "routable" in traits       # @api
        await kernel.shutdown()

    async def test_versioned_trait(self):
        kernel = await boot_with(TraitDemoComponent)
        status = kernel.status()
        demo = next(c for c in status["components"] if c["name"] == "trait-demo")
        assert "versioned" in demo["traits"]  # version="2.0"
        await kernel.shutdown()


# ── L2: @subscribe ─────────────────────────────────────────────────

class TestSubscribe:
    async def test_subscription_wired_during_boot(self):
        kernel = await boot_with(TraitDemoComponent)

        # Publish an event — subscriber should receive it
        await kernel.bus.publish("demo.event", {"msg": "hello"})
        await asyncio.sleep(0.01)

        # Check the component instance received the event
        ci = kernel.lifecycle.get_instance("trait-demo")
        assert len(ci.instance._events) == 1
        assert ci.instance._events[0] == {"msg": "hello"}
        await kernel.shutdown()

    async def test_multiple_subscriptions(self):
        @component("multi-sub", depends=["config"])
        @requires(config="IConfig")
        class MultiSub:
            @lifecycle.activate
            def activate(self, rt):
                self.received = []

            @subscribe("event.a")
            async def on_a(self, event_type, data):
                self.received.append(("a", data))

            @subscribe("event.b")
            async def on_b(self, event_type, data):
                self.received.append(("b", data))

        kernel = await boot_with(MultiSub)
        await kernel.bus.publish("event.a", "data-a")
        await kernel.bus.publish("event.b", "data-b")
        await asyncio.sleep(0.01)

        ci = kernel.lifecycle.get_instance("multi-sub")
        assert ("a", "data-a") in ci.instance.received
        assert ("b", "data-b") in ci.instance.received
        await kernel.shutdown()


# ── L2: @kind ──────────────────────────────────────────────────────

class TestKind:
    async def test_kind_registered_in_kernel(self):
        kernel = await boot_with(TraitDemoComponent)
        assert "item" in kernel.kinds
        assert kernel.kinds["item"].model is ItemModel
        assert kernel.kinds["item"].description == "Demo item"
        await kernel.shutdown()

    async def test_kind_validation(self):
        kernel = await boot_with(TraitDemoComponent)
        kd = kernel.kinds["item"]
        # The model is a Pydantic model — it can validate data
        obj = kd.model(name="test", value=42)
        assert obj.name == "test"
        assert obj.value == 42
        await kernel.shutdown()


# ── L2: @skill ─────────────────────────────────────────────────────

class TestSkill:
    async def test_skill_registered_in_kernel(self):
        kernel = await boot_with(TraitDemoComponent)
        assert "demo-guide" in kernel.skills
        sk = kernel.skills["demo-guide"]
        assert "How to use the demo" in sk.content
        assert "demo" in sk.triggers
        await kernel.shutdown()

    async def test_skill_lookup_by_trigger(self):
        kernel = await boot_with(TraitDemoComponent)
        # Find skills matching a trigger
        matches = [
            sk for sk in kernel.skills.values()
            if any(t in ["demo"] for t in sk.triggers)
        ]
        assert len(matches) == 1
        assert matches[0].name == "demo-guide"
        await kernel.shutdown()


# ── @platform_app bundle ───────────────────────────────────────────

class TestPlatformAppBundle:
    async def test_bundle_applies_requires_and_api(self):
        _finalize_meta(BundledApp)
        meta = get_meta(BundledApp)
        assert meta.factory_name == "bundled-app"
        assert "config" in meta.requires
        assert "logger" in meta.requires
        assert meta.requires["config"] == "IConfig"
        assert meta.has_api("rest")
        assert meta.has_api("mcp")

    async def test_bundle_boots_and_works(self):
        kernel = await boot_with(BundledApp)
        result = await kernel.invoke("bundled-app.ping", {})
        assert result == {"pong": True}
        await kernel.shutdown()


# ── L3: Targeted multi-instance ────────────────────────────────────

class TestTargeted:
    async def test_target_routing(self):
        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, CredentialProvider,
                        StorageProvider, APIGateway, TargetDemoComponent])

        # Create two instances of the same factory with different targets
        kernel.instantiate("target-demo", "td-alpha", {"target": "alpha"})
        kernel.instantiate("target-demo", "td-beta", {"target": "beta"})

        await kernel.boot()

        # Direct instance call
        result = await kernel.invoke("td-alpha.whoami", {})
        assert result == {"target": "alpha"}

        result = await kernel.invoke("td-beta.whoami", {})
        assert result == {"target": "beta"}

        # Target-routed call (factory name + target param)
        result = await kernel.invoke("target-demo.whoami", {"target": "alpha"})
        assert result == {"target": "alpha"}

        result = await kernel.invoke("target-demo.whoami", {"target": "beta"})
        assert result == {"target": "beta"}

        await kernel.shutdown()

    async def test_target_missing_raises(self):
        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, CredentialProvider,
                        StorageProvider, APIGateway, TargetDemoComponent])
        kernel.instantiate("target-demo", "td-only", {"target": "only"})
        await kernel.boot()

        with pytest.raises(KeyError):
            await kernel.invoke("target-demo.whoami", {"target": "nonexistent"})

        await kernel.shutdown()


# ── rt.spawn() — runtime component tree ────────────────────────────

@component("child-worker", version="1.0", depends=["config"])
@requires(config="IConfig")
class ChildWorker:
    @lifecycle.activate
    def activate(self, rt):
        self._id = rt.properties.get("worker_id", "?")

    @runnable("whoami", params=BaseModel, description="Who am I?")
    async def whoami(self, rt, params):
        return {"worker_id": self._id}

    @lifecycle.deactivate
    def deactivate(self, rt):
        pass


@component("parent-manager", version="1.0", depends=["config"])
@requires(config="IConfig")
class ParentManager:
    @lifecycle.activate
    async def activate(self, rt):
        self.worker_a = await rt.spawn("child-worker", "worker-a", {"worker_id": "A"})
        self.worker_b = await rt.spawn("child-worker", "worker-b", {"worker_id": "B"})

    @runnable("status", params=BaseModel, description="Manager status")
    async def status(self, rt, params):
        return {"workers": ["A", "B"]}


class TestSpawn:
    async def test_spawn_creates_children(self):
        kernel = await boot_with(ParentManager, ChildWorker)

        # Children are on the bus
        result = await kernel.invoke("worker-a.whoami", {})
        assert result == {"worker_id": "A"}

        result = await kernel.invoke("worker-b.whoami", {})
        assert result == {"worker_id": "B"}

        # Parent tracks children
        parent_ci = kernel.lifecycle.get_instance("parent-manager")
        assert "worker-a" in parent_ci.children
        assert "worker-b" in parent_ci.children

        # Children track parent
        child_ci = kernel.lifecycle.get_instance("worker-a")
        assert child_ci.parent == "parent-manager"

        await kernel.shutdown()

    async def test_shutdown_deactivates_children_before_parent(self):
        order = []

        @component("ordered-parent", depends=["config"])
        @requires(config="IConfig")
        class OrderedParent:
            @lifecycle.activate
            async def activate(self, rt):
                await rt.spawn("ordered-child", "oc-1", {"id": "1"})
                await rt.spawn("ordered-child", "oc-2", {"id": "2"})

            @lifecycle.deactivate
            def deactivate(self, rt):
                order.append("parent")

        @component("ordered-child", depends=["config"])
        @requires(config="IConfig")
        class OrderedChild:
            @lifecycle.activate
            def activate(self, rt):
                pass

            @lifecycle.deactivate
            def deactivate(self, rt):
                order.append(f"child-{rt.properties.get('id', '?')}")

        kernel = await boot_with(OrderedParent, OrderedChild)
        await kernel.shutdown()

        # Children deactivate before parent
        assert order.index("child-1") < order.index("parent")
        assert order.index("child-2") < order.index("parent")
