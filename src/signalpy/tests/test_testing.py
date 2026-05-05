"""Tests for signalpy.testing — the test helpers themselves."""
import pytest
from pydantic import BaseModel

from signalpy.kernel import component, provides, requires, runnable, lifecycle
from signalpy.kernel.contracts import IConfig, ILogger
from signalpy.kernel.lifecycle_manager import State
from signalpy.testing import KernelFixture, mock_runtime, FakeConfig, FakeLogger


# ── Test components ──────────────────────────────────────────────────

class GreetParams(BaseModel):
    name: str = "world"


@component("greeter", version="1.0")
@requires(config=IConfig)
class GreeterApp:
    @lifecycle.activate
    def activate(self):
        self.greeting_prefix = self.rt.config.get("greeting", "Hello")

    @runnable("greet", params=GreetParams, description="Greet someone")
    async def greet(self, params):
        return {"message": f"{self.greeting_prefix}, {params.name}!"}


@component("counter", version="1.0")
@provides("ICounter")
class CounterApp:
    @lifecycle.activate
    def activate(self):
        self._count = 0

    @runnable("increment", params=BaseModel, description="Increment counter")
    async def increment(self, params):
        self._count += 1
        return {"count": self._count}


# ── KernelFixture ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kernel_boots_and_shuts_down():
    async with KernelFixture(CounterApp) as kernel:
        assert kernel.healthy
        schema = next(s for s in kernel.runnables() if s.name == "increment")
        result = await schema.handler({})
        assert result["count"] == 1
    # After exit, kernel is stopped
    assert not kernel.healthy


@pytest.mark.asyncio
async def test_kernel_with_config():
    async with KernelFixture(GreeterApp, config={"greeting": "Hey"}) as kernel:
        schema = next(s for s in kernel.runnables() if s.name == "greet")
        result = await schema.handler({"name": "Alice"})
        assert result["message"] == "Hey, Alice!"


@pytest.mark.asyncio
async def test_kernel_default_config():
    async with KernelFixture(GreeterApp) as kernel:
        schema = next(s for s in kernel.runnables() if s.name == "greet")
        result = await schema.handler({"name": "Bob"})
        assert result["message"] == "Hello, Bob!"


@pytest.mark.asyncio
async def test_kernel_multiple_components():
    async with KernelFixture(GreeterApp, CounterApp, config={"greeting": "Hi"}) as kernel:
        greet = next(s for s in kernel.runnables() if s.name == "greet")
        incr = next(s for s in kernel.runnables() if s.name == "increment")
        g = await greet.handler({"name": "X"})
        c = await incr.handler({})
        assert g["message"] == "Hi, X!"
        assert c["count"] == 1


# ── mock_runtime ─────────────────────────────────────────────────────


def test_mock_runtime_services():
    cfg = FakeConfig({"url": "http://test"})
    rt = mock_runtime(config=cfg, logger=FakeLogger())
    assert rt.config.get("url") == "http://test"
    assert rt.component_name == "test"


def test_mock_runtime_custom_name():
    rt = mock_runtime(component_name="my-app", factory_name="my-factory")
    assert rt.component_name == "my-app"
    assert rt.factory_name == "my-factory"


def test_mock_runtime_with_properties():
    rt = mock_runtime(properties={"target": "prod"})
    assert rt.properties["target"] == "prod"


# ── FakeConfig ───────────────────────────────────────────────────────


def test_fake_config_get_simple():
    cfg = FakeConfig({"name": "Alice"})
    assert cfg.get("name") == "Alice"
    assert cfg.get("missing", "default") == "default"


def test_fake_config_get_dotted():
    cfg = FakeConfig({"db": {"host": "localhost", "port": 5432}})
    assert cfg.get("db.host") == "localhost"
    assert cfg.get("db.port") == 5432
    assert cfg.get("db.missing", "x") == "x"


def test_fake_config_set():
    cfg = FakeConfig()
    cfg.set("app.url", "http://test")
    assert cfg.get("app.url") == "http://test"


def test_fake_config_all():
    data = {"a": 1, "b": 2}
    cfg = FakeConfig(data)
    assert cfg.all() == data


# ── FakeLogger ───────────────────────────────────────────────────────


def test_fake_logger_captures():
    logger = FakeLogger()
    logger.info("started")
    logger.error("connection failed")
    logger.debug("trace data", key="val")

    assert len(logger.entries) == 3
    assert logger.has("info", "started")
    assert logger.has("error", "connection failed")
    assert not logger.has("error", "started")


def test_fake_logger_clear():
    logger = FakeLogger()
    logger.info("msg")
    assert len(logger.entries) == 1
    logger.clear()
    assert len(logger.entries) == 0
