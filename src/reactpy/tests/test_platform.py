"""Tests for platform providers: config, credentials, storage, auth, workspace, gateway."""
import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from pydantic import BaseModel

from reactpy.kernel import Kernel, component, provides, requires, runnable, lifecycle, api
from reactpy.providers.config import ConfigProvider
from reactpy.providers.logging_provider import LoggingProvider
from reactpy.providers.credentials import CredentialProvider
from reactpy.providers.storage import StorageProvider
from reactpy.providers.auth import AuthProvider
from reactpy.providers.workspace import WorkspaceProvider
from reactpy.providers.gateway import APIGateway
from reactpy.adapters.rest import RESTTransport
from reactpy.adapters.mcp import MCPTransport
from reactpy.adapters.cli import CLITransport


# ── Helper: boot a kernel with platform + test apps ───────────────


class GreetParams(BaseModel):
    value: str = "default"


@component("test-app", version="1.0", depends=["config", "logging"])
@provides("ITestApp")
@requires(config="IConfig", logger="ILogger")
@api("rest", prefix="/test", version="v1")
@api("mcp", name="test-tools")
class TestApp:
    @lifecycle.activate
    def activate(self, rt):
        self._greeting = rt.config.get("test-app.greeting", "hi")

    @runnable("greet", params=GreetParams, description="Greet")
    async def greet(self, rt, params):
        val = params.get("value", "default") if isinstance(params, dict) else "default"
        return {"greeting": f"{self._greeting}, {val}"}


async def boot_test_kernel(extra_components=None, config_defaults=None):
    kernel = Kernel()
    components = [
        ConfigProvider,
        LoggingProvider,
        CredentialProvider,
        StorageProvider,
        APIGateway,
    ]
    if extra_components:
        components.extend(extra_components)
    kernel.discover(components)

    # Pre-instantiate config so we can set properties before boot
    if config_defaults:
        ci = kernel.instantiate("config")
        ci.properties = {"defaults": config_defaults}

    await kernel.boot()
    return kernel


# ── ConfigProvider ─────────────────────────────────────────────────


class TestConfigProvider:
    @pytest.mark.asyncio
    async def test_dotted_path_get(self):
        kernel = await boot_test_kernel(config_defaults={
            "app": {"name": "test", "nested": {"key": "value"}},
        })
        config = kernel.registry.require("IConfig")
        assert config.get("app.name") == "test"
        assert config.get("app.nested.key") == "value"
        assert config.get("app.missing", "fallback") == "fallback"
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_runtime_set(self):
        kernel = await boot_test_kernel()
        config = kernel.registry.require("IConfig")
        config.set("dynamic.key", 42)
        assert config.get("dynamic.key") == 42
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_env_override(self):
        os.environ["KERNEL_CFG_TEST__ENVKEY"] = "from_env"
        try:
            kernel = await boot_test_kernel()
            config = kernel.registry.require("IConfig")
            assert config.get("test.envkey") == "from_env"
            await kernel.shutdown()
        finally:
            del os.environ["KERNEL_CFG_TEST__ENVKEY"]


# ── CredentialProvider ─────────────────────────────────────────────


class TestCredentialProvider:
    @pytest.mark.asyncio
    async def test_scoped_credential_get(self):
        kernel = await boot_test_kernel(config_defaults={
            "apps": {"my-app": {"targets": {"prod": {"token": "secret123"}}}},
        })
        creds = kernel.registry.require("ICredentials")
        # Direct (unscoped) call — needs app param
        val = creds.get("token", target="prod", app="my-app")
        assert val == "secret123"
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_missing_credential_returns_empty(self):
        kernel = await boot_test_kernel()
        creds = kernel.registry.require("ICredentials")
        assert creds.get("nonexistent", app="no-app") == ""
        await kernel.shutdown()


# ── StorageProvider ────────────────────────────────────────────────


class TestStorageProvider:
    @pytest.mark.asyncio
    async def test_put_get_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            kernel = await boot_test_kernel(config_defaults={
                "storage": {"root": tmpdir},
            })
            storage = kernel.registry.require("IStorage")

            await storage.put("test.txt", b"hello world", prefix="test-app")
            data = await storage.get("test.txt", prefix="test-app")
            assert data == b"hello world"

            files = await storage.list(prefix="test-app")
            assert "test.txt" in files

            await storage.delete("test.txt", prefix="test-app")
            data = await storage.get("test.txt", prefix="test-app")
            assert data is None

            await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            kernel = await boot_test_kernel(config_defaults={
                "storage": {"root": tmpdir},
            })
            storage = kernel.registry.require("IStorage")

            with pytest.raises(ValueError, match="Path traversal"):
                await storage.put("../../etc/passwd", b"hacked")

            with pytest.raises(ValueError, match="Path traversal"):
                await storage.get("../../../etc/shadow")

            await kernel.shutdown()


# ── AuthProvider ───────────────────────────────────────────────────


class TestAuthProvider:
    @pytest.mark.asyncio
    async def test_authenticate_valid_token(self):
        kernel = await boot_test_kernel(
            extra_components=[AuthProvider],
            config_defaults={
                "auth": {
                    "tokens": {"valid-token": {"identity": "user1", "roles": ["admin"]}},
                    "policies": {"admin": ["*"]},
                },
            },
        )
        auth = kernel.registry.require("IAuth")
        identity = auth.authenticate("valid-token")
        assert identity["subject"] == "user1"
        assert identity["authenticated"] is True
        assert "admin" in identity["roles"]
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_authenticate_invalid_token(self):
        kernel = await boot_test_kernel(
            extra_components=[AuthProvider],
            config_defaults={"auth": {"tokens": {}}},
        )
        auth = kernel.registry.require("IAuth")
        with pytest.raises(ValueError, match="Invalid token"):
            auth.authenticate("bad-token")
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_authorize(self):
        kernel = await boot_test_kernel(
            extra_components=[AuthProvider],
            config_defaults={
                "auth": {
                    "policies": {
                        "reader": ["read", "list"],
                        "admin": ["*"],
                    },
                },
            },
        )
        auth = kernel.registry.require("IAuth")
        assert auth.authorize({"roles": ["reader"]}, "read") is True
        assert auth.authorize({"roles": ["reader"]}, "delete") is False
        assert auth.authorize({"roles": ["admin"]}, "anything") is True
        assert auth.authorize({"roles": []}, "read") is False
        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_auth_disabled(self):
        kernel = await boot_test_kernel(
            extra_components=[AuthProvider],
            config_defaults={"auth": {"enabled": False}},
        )
        auth = kernel.registry.require("IAuth")
        identity = auth.authenticate("")
        assert identity["authenticated"] is False
        assert auth.authorize(identity, "anything") is True
        await kernel.shutdown()


# ── WorkspaceProvider ──────────────────────────────────────────────


class TestWorkspaceProvider:
    @pytest.mark.asyncio
    async def test_workspace_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            kernel = await boot_test_kernel(
                extra_components=[WorkspaceProvider],
                config_defaults={"workspace": {"root": tmpdir}},
            )
            ws = kernel.registry.require("IWorkspace")
            assert ws.root == Path(tmpdir).resolve()
            assert ws.path("sub", "dir") == Path(tmpdir).resolve() / "sub" / "dir"
            await kernel.shutdown()


# ── Gateway + Transport Integration ───────────────────────────────


class TestGatewayIntegration:
    @pytest.mark.asyncio
    async def test_api_surface_composition(self):
        kernel = await boot_test_kernel(
            extra_components=[TestApp, RESTTransport, MCPTransport],
            config_defaults={"test-app": {"greeting": "hello"}},
        )

        gw = kernel.registry.require("IGateway")

        # REST surface should have test-app entries
        rest_surface = gw.get_surface("rest")
        assert rest_surface is not None
        rest_names = [e.short_name for e in rest_surface.entries]
        assert "greet" in rest_names

        # MCP surface should also have them
        mcp_surface = gw.get_surface("mcp")
        assert mcp_surface is not None
        mcp_names = [e.short_name for e in mcp_surface.entries]
        assert "greet" in mcp_names

        # CLI surface should NOT have them (no @api("cli"))
        cli_surface = gw.get_surface("cli")
        cli_names = [e.short_name for e in cli_surface.entries] if cli_surface else []
        assert "greet" not in cli_names

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_bus_invocation_through_gateway(self):
        kernel = await boot_test_kernel(
            extra_components=[TestApp],
            config_defaults={"test-app": {"greeting": "hey"}},
        )

        result = await kernel.bus.invoke("test-app.greet", {"value": "world"})
        assert result == {"greeting": "hey, world"}

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_internal_runnable_excluded_from_surface(self):
        @component("surface-test", version="1.0", depends=["config", "logging"])
        @requires(config="IConfig", logger="ILogger")
        @api("rest", prefix="/stest")
        class SurfaceTestApp:
            @runnable("public", params=BaseModel, description="Public")
            async def public(self, rt, params):
                return {"public": True}

            @runnable("_hidden", params=BaseModel, internal=True, description="Hidden")
            async def hidden(self, rt, params):
                return {"hidden": True}

        kernel = await boot_test_kernel(
            extra_components=[SurfaceTestApp, RESTTransport],
        )

        gw = kernel.registry.require("IGateway")
        rest_surface = gw.get_surface("rest")
        rest_names = [e.runnable_name for e in rest_surface.entries]
        assert "surface-test.public" in rest_names
        assert "surface-test._hidden" not in rest_names

        # But internal IS on bus
        assert kernel.bus.has_handler("surface-test._hidden")

        await kernel.shutdown()
