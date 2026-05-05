"""Tests for spec 011 — eliminate bus.invoke, direct dispatch via @requires.

Tests the additive changes:
- transports=[] on @runnable
- handler reference on HandlerSchema
- transport config on @component
- kernel.runnables() and kernel.runnables_by_component()
"""
import pytest
from pydantic import BaseModel

from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
)
from signalpy.kernel.component import _finalize_meta, RunnableDef
from signalpy.kernel.bus import HandlerSchema
from signalpy.kernel.contracts import IConfig


# ── RunnableDef.visible_on ──────────────────────────────────────────


def _make_rd(**kwargs) -> RunnableDef:
    defaults = dict(
        name="test", params_model=BaseModel, return_type=None,
        fn=lambda: None, description="test",
    )
    defaults.update(kwargs)
    return RunnableDef(**defaults)


def test_visible_on_default_all():
    rd = _make_rd()
    assert rd.visible_on("rest")
    assert rd.visible_on("mcp")
    assert rd.visible_on("native")


def test_visible_on_native_only():
    rd = _make_rd(transports=["native"])
    assert rd.visible_on("native")
    assert not rd.visible_on("rest")
    assert not rd.visible_on("mcp")


def test_visible_on_specific_transports():
    rd = _make_rd(transports=["mcp", "rest"])
    assert rd.visible_on("mcp")
    assert rd.visible_on("rest")
    assert not rd.visible_on("cli")
    assert not rd.visible_on("native")


def test_visible_on_legacy_internal():
    """internal=True without transports behaves as native-only."""
    rd = _make_rd(internal=True)
    assert rd.visible_on("native")
    assert not rd.visible_on("rest")


def test_visible_on_transports_overrides_internal():
    """If transports is set explicitly, it takes precedence."""
    rd = _make_rd(internal=True, transports=["mcp"])
    assert rd.visible_on("mcp")
    assert not rd.visible_on("rest")


# ── HandlerSchema.visible_on ────────────────────────────────────────


def test_schema_visible_on():
    schema = HandlerSchema(name="test", transports=["mcp", "native"])
    assert schema.visible_on("mcp")
    assert schema.visible_on("native")
    assert not schema.visible_on("rest")


def test_schema_from_runnable_def_carries_transports():
    rd = _make_rd(transports=["native"])
    schema = HandlerSchema.from_runnable_def(rd, provider_name="app")
    assert schema.transports == ["native"]
    assert schema.visible_on("native")
    assert not schema.visible_on("rest")


def test_schema_from_runnable_def_carries_handler():
    handler = lambda p: p
    rd = _make_rd()
    schema = HandlerSchema.from_runnable_def(rd, provider_name="app", handler=handler)
    assert schema.handler is handler


# ── Transport config on @component ──────────────────────────────────


def test_component_transport_config():
    @component("my-app", version="1.0",
               rest={"prefix": "/my-app", "version": "v1"},
               mcp={"name": "my-tools"})
    class MyApp:
        pass

    meta = _finalize_meta(MyApp)
    assert meta.transport_config["rest"] == {"prefix": "/my-app", "version": "v1"}
    assert meta.transport_config["mcp"] == {"name": "my-tools"}


def test_component_no_transport_config():
    @component("plain", version="1.0")
    class Plain:
        pass

    meta = _finalize_meta(Plain)
    assert meta.transport_config == {}


def test_has_api_with_transport_config():
    @component("app", version="1.0", rest={"prefix": "/app"})
    class App:
        pass

    meta = _finalize_meta(App)
    assert meta.has_api("rest")
    assert not meta.has_api("mcp")
    assert meta.has_api()


# ── api_runnables with transport_config ─────────────────────────────


def test_api_runnables_with_transport_config():
    @component("app", version="1.0", rest={"prefix": "/app"}, mcp={"name": "tools"})
    class App:
        @runnable("public-op", params=BaseModel, description="Public")
        async def public_op(self, params):
            return {}

        @runnable("internal-op", params=BaseModel, description="Internal",
                  transports=["native"])
        async def internal_op(self, params):
            return {}

        @runnable("mcp-only", params=BaseModel, description="MCP only",
                  transports=["mcp"])
        async def mcp_only(self, params):
            return {}

    meta = _finalize_meta(App)

    rest_runnables = meta.api_runnables("rest")
    rest_names = [r.name for r in rest_runnables]
    assert "public-op" in rest_names
    assert "internal-op" not in rest_names
    assert "mcp-only" not in rest_names

    mcp_runnables = meta.api_runnables("mcp")
    mcp_names = [r.name for r in mcp_runnables]
    assert "public-op" in mcp_names
    assert "mcp-only" in mcp_names
    assert "internal-op" not in mcp_names


# ── kernel.runnables() ──────────────────────────────────────────────


@component("svc-a", version="1.0",
           rest={"prefix": "/a"}, mcp={"name": "a-tools"})
@provides("ISvcA")
@requires(config="IConfig")
class SvcA:
    @lifecycle.activate
    def activate(self):
        pass

    @runnable("public", params=BaseModel, description="Public op")
    async def public_op(self, params):
        return {}

    @runnable("native-only", params=BaseModel, description="Native only",
              transports=["native"])
    async def native_only(self, params):
        return {}

    @runnable("mcp-only", params=BaseModel, description="MCP only",
              transports=["mcp"])
    async def mcp_only(self, params):
        return {}


@pytest.fixture
async def kernel_with_svc():
    from signalpy.providers.config import ConfigProvider
    from signalpy.providers.logging_provider import LoggingProvider

    kernel = Kernel()
    kernel.discover([ConfigProvider, LoggingProvider, SvcA])
    await kernel.boot()
    yield kernel
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_kernel_runnables_all(kernel_with_svc):
    """kernel.runnables() returns all non-internal schemas."""
    schemas = kernel_with_svc.runnables()
    names = [s.name for s in schemas if s.provider == "svc-a"]
    assert "public" in names
    assert "mcp-only" in names
    # native-only has transports=["native"], which is not internal
    assert "native-only" in names


@pytest.mark.asyncio
async def test_kernel_runnables_by_transport(kernel_with_svc):
    """kernel.runnables(transport=) filters by transport visibility."""
    rest = kernel_with_svc.runnables(transport="rest")
    rest_names = [s.name for s in rest if s.provider == "svc-a"]
    assert "public" in rest_names
    assert "native-only" not in rest_names
    assert "mcp-only" not in rest_names

    mcp = kernel_with_svc.runnables(transport="mcp")
    mcp_names = [s.name for s in mcp if s.provider == "svc-a"]
    assert "public" in mcp_names
    assert "mcp-only" in mcp_names
    assert "native-only" not in mcp_names

    native = kernel_with_svc.runnables(transport="native")
    native_names = [s.name for s in native if s.provider == "svc-a"]
    assert "public" in native_names
    assert "native-only" in native_names


@pytest.mark.asyncio
async def test_kernel_runnables_have_handler(kernel_with_svc):
    """Every schema has a callable handler reference."""
    schemas = kernel_with_svc.runnables()
    for schema in schemas:
        assert schema.handler is not None
        assert callable(schema.handler)


@pytest.mark.asyncio
async def test_kernel_runnables_handler_works(kernel_with_svc):
    """schema.handler can be called directly (no bus.invoke needed)."""
    schemas = kernel_with_svc.runnables()
    public = next(s for s in schemas
                  if s.provider == "svc-a" and s.name == "public")
    result = await public.handler({})
    assert result == {}


@pytest.mark.asyncio
async def test_kernel_runnables_by_component(kernel_with_svc):
    """kernel.runnables_by_component groups by provider with transport config."""
    grouped = kernel_with_svc.runnables_by_component(transport="rest")
    assert "svc-a" in grouped
    entry = grouped["svc-a"]
    assert entry["transport_config"] == {"prefix": "/a"}
    names = [s.name for s in entry["schemas"]]
    assert "public" in names
    assert "native-only" not in names


@pytest.mark.asyncio
async def test_runnable_signal_updated(kernel_with_svc):
    """kernel.runnable_signal tracks available runnables."""
    signal_val = kernel_with_svc.runnable_signal.get()
    assert "svc-a.public" in signal_val
    assert "svc-a.native-only" in signal_val


@pytest.mark.asyncio
async def test_hot_remove_cleans_schemas(kernel_with_svc):
    """hot_remove cleans up kernel-level schemas."""
    kernel = kernel_with_svc
    assert any(s.provider == "svc-a" for s in kernel.runnables())

    await kernel.hot_remove("svc-a")
    assert not any(s.provider == "svc-a" for s in kernel.runnables())
    assert "svc-a.public" not in kernel.runnable_signal.get()
