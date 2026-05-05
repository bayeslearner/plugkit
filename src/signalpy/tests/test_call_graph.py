"""Tests for static + runtime call graph features."""
import asyncio
import pytest
from pydantic import BaseModel

from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle, subscribe,
)
from signalpy.kernel.contracts import IConfig
from signalpy.kernel.component import _finalize_meta


# ── Static call graph extraction ─────────────────────────────────────


class InvokeParams(BaseModel):
    query: str = ""


@component("caller-app", version="1.0")
@requires(config="IConfig")
class CallerApp:
    """A component that invokes other runnables and publishes events."""

    @lifecycle.activate
    def activate(self):
        pass

    @runnable("do-work", params=BaseModel, description="Do work")
    async def do_work(self, params):
        # These invoke targets should be extracted statically
        result = await self.rt.invoke("search.query", {"q": "hello"})
        await self.rt.invoke("email-service.send", {"to": "a@b.com"})
        return result

    @runnable("notify", params=BaseModel, description="Notify")
    async def notify(self, params):
        # These publish events should be extracted statically
        self.rt.publish("orders.placed", {"id": 1})
        self.rt.publish("data.updated", {"source": "caller"})
        return {"ok": True}


def test_static_invoke_targets_extracted():
    """_finalize_meta extracts invoke target strings from source."""
    meta = _finalize_meta(CallerApp)
    assert "search.query" in meta.invoke_targets
    assert "email-service.send" in meta.invoke_targets


def test_static_publish_events_extracted():
    """_finalize_meta extracts publish event strings from source."""
    meta = _finalize_meta(CallerApp)
    assert "orders.placed" in meta.publish_events
    assert "data.updated" in meta.publish_events


@component("no-calls", version="1.0")
class NoCallsApp:
    @lifecycle.activate
    def activate(self):
        self.x = 1

    @runnable("noop", params=BaseModel, description="Noop")
    async def noop(self, params):
        return {"ok": True}


def test_no_invoke_targets_when_none_in_source():
    """Components without invoke/publish have empty lists."""
    meta = _finalize_meta(NoCallsApp)
    assert meta.invoke_targets == []
    assert meta.publish_events == []


# ── Module-level function following ─────────────────────────────────


# These module-level helpers simulate the pattern where a component
# method delegates to a function in the same module that contains
# the actual bus calls.

def _mount_routes(app, bus):
    """Simulates a large route-mounting function with bus calls."""
    async def list_cases():
        return await bus.invoke("case-store.list", {})
    async def list_chats():
        return await bus.invoke("conversation-store.list", {})


def _setup_listeners(rt):
    """Module-level helper that publishes events."""
    rt.publish("system.ready", {"status": "ok"})
    rt.publish("audit.login", {"user": "admin"})


def _no_bus_calls():
    """A helper with no invoke/publish calls."""
    return 42


@component("delegator-app", version="1.0")
class DelegatorApp:
    """Component that delegates to module-level functions."""

    @lifecycle.activate
    def activate(self):
        _mount_routes(None, self.rt)
        _setup_listeners(self.rt)

    @runnable("direct", params=BaseModel, description="Direct call")
    async def direct(self, params):
        # This one is directly in the method
        return await self.rt.invoke("direct-svc.action", {})


def test_follows_module_level_invoke_targets():
    """Static analysis follows module-level functions for invoke targets."""
    meta = _finalize_meta(DelegatorApp)
    assert "case-store.list" in meta.invoke_targets
    assert "conversation-store.list" in meta.invoke_targets
    # Direct calls in class methods still work
    assert "direct-svc.action" in meta.invoke_targets


def test_follows_module_level_publish_events():
    """Static analysis follows module-level functions for publish events."""
    meta = _finalize_meta(DelegatorApp)
    assert "system.ready" in meta.publish_events
    assert "audit.login" in meta.publish_events


@component("no-delegate", version="1.0")
class NoDelegateApp:
    """Component that calls a module-level function with no bus calls."""

    @lifecycle.activate
    def activate(self):
        _no_bus_calls()

    @runnable("noop", params=BaseModel, description="Noop")
    async def noop(self, params):
        return {"ok": True}


def test_module_level_without_bus_calls_yields_nothing():
    """Following a module-level function without bus calls adds no targets."""
    meta = _finalize_meta(NoDelegateApp)
    assert meta.invoke_targets == []
    assert meta.publish_events == []


# ── Runtime call graph recording ─────────────────────────────────────


@component("target-svc", version="1.0")
@provides("ITargetService")
class TargetService:
    @runnable("action", params=BaseModel, description="Do action")
    async def action(self, params):
        return {"done": True}

    @runnable("slow", params=BaseModel, description="Slow action")
    async def slow(self, params):
        await asyncio.sleep(0.01)
        return {"done": True}

    @runnable("fail", params=BaseModel, description="Failing action")
    async def fail(self, params):
        raise ValueError("intentional failure")


@pytest.fixture
async def kernel_with_target():
    from signalpy.providers.config import ConfigProvider
    from signalpy.providers.logging_provider import LoggingProvider

    kernel = Kernel()
    kernel.discover([ConfigProvider, LoggingProvider, TargetService])
    await kernel.boot()
    yield kernel
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_runtime_call_graph_records_invocations(kernel_with_target):
    """Bus records call count and timing for invoke calls."""
    kernel = kernel_with_target
    bus = kernel.bus

    # Make some calls
    await bus.invoke("target-svc.action", {})
    await bus.invoke("target-svc.action", {})
    await bus.invoke("target-svc.slow", {})

    graph = bus.call_graph
    assert "target-svc.action" in graph
    assert graph["target-svc.action"]["count"] == 2
    assert graph["target-svc.action"]["errors"] == 0
    assert graph["target-svc.slow"]["count"] == 1
    assert graph["target-svc.slow"]["total_ms"] >= 5  # at least some time


@pytest.mark.asyncio
async def test_runtime_call_graph_records_errors(kernel_with_target):
    """Bus records errors in call stats."""
    kernel = kernel_with_target
    bus = kernel.bus

    with pytest.raises(ValueError):
        await bus.invoke("target-svc.fail", {})

    graph = bus.call_graph
    assert "target-svc.fail" in graph
    assert graph["target-svc.fail"]["count"] == 1
    assert graph["target-svc.fail"]["errors"] == 1


@pytest.mark.asyncio
async def test_runtime_call_graph_reset(kernel_with_target):
    """reset_call_graph clears all stats."""
    kernel = kernel_with_target
    bus = kernel.bus

    await bus.invoke("target-svc.action", {})
    assert bus.call_graph != {}

    bus.reset_call_graph()
    assert bus.call_graph == {}


# ── kernel.graph() integration ───────────────────────────────────────


@pytest.fixture
async def full_kernel():
    from signalpy.providers.config import ConfigProvider
    from signalpy.providers.logging_provider import LoggingProvider

    kernel = Kernel()
    kernel.discover([ConfigProvider, LoggingProvider, CallerApp, TargetService])
    await kernel.boot()
    yield kernel
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_kernel_graph_includes_weak_invocation_edges(full_kernel):
    """kernel.graph() includes static invoke targets as weak edges."""
    g = full_kernel.graph()
    inv_edges = g["edges"]["invocations"]
    # CallerApp invokes search.query and email-service.send
    targets_from_caller = [e for e in inv_edges if e["from"] == "caller-app"]
    target_names = [e["to"] for e in targets_from_caller]
    assert "search.query" in target_names
    assert "email-service.send" in target_names
    assert all(e["type"] == "weak" for e in targets_from_caller)


@pytest.mark.asyncio
async def test_kernel_graph_includes_weak_publication_edges(full_kernel):
    """kernel.graph() includes static publish events as weak edges."""
    g = full_kernel.graph()
    pub_edges = g["edges"]["publications"]
    pubs_from_caller = [e for e in pub_edges if e["from"] == "caller-app"]
    event_names = [e["event"] for e in pubs_from_caller]
    assert "orders.placed" in event_names
    assert "data.updated" in event_names


@pytest.mark.asyncio
async def test_kernel_graph_includes_call_stats(full_kernel):
    """kernel.graph() includes runtime call stats after invocations."""
    kernel = full_kernel
    await kernel.bus.invoke("target-svc.action", {})
    await kernel.bus.invoke("target-svc.action", {})

    g = kernel.graph()
    assert "call_stats" in g
    assert "target-svc.action" in g["call_stats"]
    assert g["call_stats"]["target-svc.action"]["count"] == 2
