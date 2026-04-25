"""FastAPI integration test — prove the kernel works inside a real HTTP server.

Tests:
1. Kernel boots inside FastAPI lifespan
2. REST routes are auto-generated from @api + @runnable
3. Bus invocation works through HTTP
4. Internal runnables hidden from HTTP
5. Spell checker works end-to-end over HTTP
6. Multiple apps coexist with different route prefixes
7. Kernel status endpoint works
"""
import asyncio
import pytest
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from signalpy.kernel import (
    Kernel, component, provides, requires,
    runnable, lifecycle, api, effect, prop,
)
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider
from signalpy.providers.credentials import CredentialProvider
from signalpy.providers.storage import StorageProvider
from signalpy.providers.gateway import APIGateway
from signalpy.adapters.rest import RESTTransport


# ══════════════════════════════════════════════════════════════════════
# Test components
# ══════════════════════════════════════════════════════════════════════

class GreetParams(BaseModel):
    name: str = "world"

class MathParams(BaseModel):
    a: float = 0
    b: float = 0

@component("greeter", version="1.0")
@provides("IGreeter")
@requires(config="IConfig", logger="ILogger")
@api("rest", prefix="/greetings", version="v1")
class GreeterApp:
    @lifecycle.activate
    def activate(self):
        self._prefix = self.rt.config.get("greeter.prefix", "Hello")

    @runnable("hello", params=GreetParams, description="Greet someone")
    async def hello(self, params):
        return {"message": f"{self._prefix}, {params.name}!"}

    @runnable("_warmup", params=BaseModel, internal=True, description="Internal")
    async def warmup(self, params):
        return {"warmed": True}


@component("math", version="1.0")
@provides("IMath")
@requires(config="IConfig")
@api("rest", prefix="/math", version="v1")
class MathApp:
    @lifecycle.activate
    def activate(self):
        pass

    @runnable("add", params=MathParams, description="Add two numbers")
    async def add(self, params):
        return {"result": params.a + params.b}

    @runnable("multiply", params=MathParams, description="Multiply two numbers")
    async def multiply(self, params):
        return {"result": params.a * params.b}


@runtime_checkable
class IDictionary(Protocol):
    def check_word(self, word: str) -> bool: ...

class CheckParams(BaseModel):
    text: str = ""
    language: str = "EN"

@component("dict-en-http", version="1.0")
@provides(IDictionary)
@prop("_language", "language", "EN")
class HttpEnglishDict:
    @lifecycle.activate
    def activate(self):
        self.words = {"hello", "world", "welcome"}
    def check_word(self, word):
        return word.lower() in self.words

@component("spell-http", version="1.0", depends=["dict-en-http"])
@provides("ISpellChecker")
@requires(dicts=IDictionary, key="language")
@api("rest", prefix="/spell", version="v1")
class HttpSpellChecker:
    @lifecycle.activate
    def activate(self):
        self.by_lang = {}

    @effect
    def on_dict_change(self):
        """Replaces @bind/@unbind — auto-runs when dicts map changes."""
        self.by_lang = dict(self.rt.dicts)  # reactive read: {"EN": en_dict, ...}

    @runnable("check", params=CheckParams, description="Spell check")
    async def check(self, params):
        d = self.by_lang.get(params.language)
        if not d:
            return {"error": f"No dictionary for {params.language}"}
        misspelled = [w for w in params.text.split() if not d.check_word(w)]
        return {"misspelled": misspelled, "ok": not misspelled}


# ══════════════════════════════════════════════════════════════════════
# Kernel + FastAPI wiring (tested pattern)
# ══════════════════════════════════════════════════════════════════════

async def boot_kernel_with_fastapi(extra_components=None):
    """Boot kernel, get the FastAPI app from RESTTransport, return (kernel, app)."""
    from fastapi import FastAPI

    kernel = Kernel()
    components = [
        ConfigProvider, LoggingProvider, CredentialProvider,
        StorageProvider, APIGateway, RESTTransport,
        GreeterApp, MathApp,
    ]
    if extra_components:
        components.extend(extra_components)

    kernel.discover(components)
    await kernel.boot()

    # The RESTTransport creates its own FastAPI app with all kernel routes
    rest = kernel.registry.require("IRestAPI")
    return kernel, rest.app


# ══════════════════════════════════════════════════════════════════════
# Tests — use the RESTTransport's generated app directly
# ══════════════════════════════════════════════════════════════════════

class TestFastAPIBasic:

    @pytest.mark.asyncio
    async def test_health_endpoint(self):
        kernel, app = await boot_kernel_with_fastapi()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/health")
            assert r.status_code == 200
            assert r.json()["status"] == "healthy"
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_greeter_hello(self):
        kernel, app = await boot_kernel_with_fastapi()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/greetings/hello", json={"name": "Alice"})
            assert r.status_code == 200
            data = r.json()
            assert data["ok"] is True
            assert data["data"]["message"] == "Hello, Alice!"
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_greeter_default_name(self):
        kernel, app = await boot_kernel_with_fastapi()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/greetings/hello", json={})
            assert r.status_code == 200
            assert r.json()["data"]["message"] == "Hello, world!"
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_math_add(self):
        kernel, app = await boot_kernel_with_fastapi()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/math/add", json={"a": 3, "b": 4})
            assert r.status_code == 200
            assert r.json()["data"]["result"] == 7
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_math_multiply(self):
        kernel, app = await boot_kernel_with_fastapi()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/math/multiply", json={"a": 5, "b": 6})
            assert r.status_code == 200
            assert r.json()["data"]["result"] == 30
        await kernel.shutdown()


class TestFastAPIInternalRoutes:

    @pytest.mark.asyncio
    async def test_internal_runnable_not_exposed(self):
        kernel, app = await boot_kernel_with_fastapi()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/greetings/_warmup", json={})
            # Should be 404 or 405 — not a valid route
            assert r.status_code in (404, 405)
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_internal_still_on_bus(self):
        kernel, app = await boot_kernel_with_fastapi()
        result = await kernel.bus.invoke("greeter._warmup", {})
        assert result == {"warmed": True}
        await kernel.shutdown()


class TestFastAPIKernelStatus:

    @pytest.mark.asyncio
    async def test_kernel_status_route(self):
        kernel, app = await boot_kernel_with_fastapi()
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/kernel/status")
            assert r.status_code == 200
            data = r.json()
            assert "surfaces" in data
            assert "rest" in data["surfaces"]
        await kernel.shutdown()


class TestFastAPISpellChecker:

    @pytest.mark.asyncio
    async def test_spell_check_correct(self):
        kernel, app = await boot_kernel_with_fastapi([HttpEnglishDict, HttpSpellChecker])
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/spell/check", json={
                "text": "hello world", "language": "EN",
            })
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["ok"] is True
            assert data["misspelled"] == []
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_spell_check_misspelled(self):
        kernel, app = await boot_kernel_with_fastapi([HttpEnglishDict, HttpSpellChecker])
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/spell/check", json={
                "text": "hello wrld", "language": "EN",
            })
            data = r.json()["data"]
            assert data["ok"] is False
            assert "wrld" in data["misspelled"]
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_spell_check_unknown_language(self):
        kernel, app = await boot_kernel_with_fastapi([HttpEnglishDict, HttpSpellChecker])
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.post("/api/v1/spell/check", json={
                "text": "bonjour", "language": "FR",
            })
            data = r.json()["data"]
            assert "error" in data
        await kernel.shutdown()


class TestFastAPIMultipleApps:

    @pytest.mark.asyncio
    async def test_three_apps_coexist(self):
        kernel, app = await boot_kernel_with_fastapi([HttpEnglishDict, HttpSpellChecker])
        from httpx import AsyncClient, ASGITransport
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            # Greeter
            r = await c.post("/api/v1/greetings/hello", json={"name": "Bob"})
            assert r.json()["data"]["message"] == "Hello, Bob!"

            # Math
            r = await c.post("/api/v1/math/add", json={"a": 10, "b": 20})
            assert r.json()["data"]["result"] == 30

            # Spell
            r = await c.post("/api/v1/spell/check", json={
                "text": "hello", "language": "EN",
            })
            assert r.json()["data"]["ok"] is True
        await kernel.shutdown()
