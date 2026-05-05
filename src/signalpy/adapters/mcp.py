"""MCP adapter — bridge between SignalPy kernel and FastMCP / MCP SDK.

Two modes:

  Container mode (SignalPy owns the MCP server):
    kernel.discover([..., MCPTransport])
    await kernel.boot()
    server = kernel.registry.require("IMCPServer")

  Library mode (mount into your existing FastMCP server):
    from fastmcp import FastMCP
    server = FastMCP("my-server")
    kernel = Kernel(); kernel.discover([...]); await kernel.boot()
    mount_mcp(server, kernel)

Spec 011: Tools call schema.handler directly — no bus.invoke.
"""
from __future__ import annotations

import logging
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)

try:
    from fastmcp import FastMCP as _FastMCP
    _HAS_FASTMCP = True
except ImportError:
    _FastMCP = None
    _HAS_FASTMCP = False


# ── Library mode: mount into an existing FastMCP server ───────────

def mount_mcp(server, kernel) -> int:
    """Mount kernel runnables as MCP tools on an existing FastMCP server.

    Uses kernel.runnables(transport="mcp") to discover operations
    and calls schema.handler directly.

    Args:
        server: A FastMCP server instance.
        kernel: A booted Kernel instance.

    Returns:
        Number of tools registered.
    """
    count = 0
    schemas = kernel.runnables(transport="mcp")

    for schema in schemas:
        tool_name = f"{schema.provider}.{schema.name}"
        _register_tool(server, tool_name, schema)
        count += 1

    log.info("mount_mcp: %d tools", count)
    return count


def _register_tool(server, tool_name: str, schema) -> None:
    """Register a single kernel runnable as an MCP tool."""
    if _HAS_FASTMCP and hasattr(server, 'tool'):
        @server.tool(name=tool_name,
                     description=schema.description or f"Invoke {tool_name}")
        async def _handler(s=schema, **kwargs) -> Any:
            return await s.handler(kwargs)
    else:
        if not hasattr(server, '_kernel_tools'):
            server._kernel_tools = []
        input_schema = {}
        if schema.params_model and hasattr(schema.params_model, 'model_json_schema'):
            input_schema = schema.params_model.model_json_schema()
        server._kernel_tools.append({
            "name": tool_name,
            "description": schema.description or f"Invoke {tool_name}",
            "input_schema": input_schema,
        })


# ── Container mode: SignalPy manages the MCP server ───────────────

@component("mcp-transport", version="0.3")
@provides("IMCPServer")
@requires(config="IConfig")
class MCPTransport:
    """Container mode — SignalPy creates and owns the MCP tool registry.

    Usage:
        kernel.discover([..., MCPTransport])
        await kernel.boot()
        server = kernel.registry.require("IMCPServer")
        tools = server.list_tools()
    """

    @lifecycle.activate
    def activate(self, rt):
        self._rt = rt
        self._schemas = {}  # tool_name → schema

        if _HAS_FASTMCP:
            self._server = _FastMCP(
                rt.config.get("mcp.name", "signalpy-kernel")
            )
        else:
            self._server = None

        log.info("MCP transport activated (FastMCP: %s)", _HAS_FASTMCP)

    async def handle_tool_call(self, tool_name: str, arguments: dict) -> Any:
        """Invoke a kernel runnable by tool name via schema.handler."""
        schema = self._schemas.get(tool_name)
        if schema is None:
            raise KeyError(f"No MCP tool {tool_name!r}")
        return await schema.handler(arguments)

    def list_tools(self) -> list[dict]:
        """Return the tool definitions."""
        tools = []
        for name, schema in self._schemas.items():
            input_schema = {}
            if schema.params_model and hasattr(schema.params_model, 'model_json_schema'):
                input_schema = schema.params_model.model_json_schema()
            tools.append({
                "name": name,
                "description": schema.description or f"Invoke {name}",
                "input_schema": input_schema,
            })
        return tools

    def get_server(self):
        """Return the FastMCP server instance (if FastMCP is installed)."""
        return self._server

    @lifecycle.deactivate
    def deactivate(self, rt):
        pass
