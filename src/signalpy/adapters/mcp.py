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

    Reads the gateway's "mcp" surface if available, otherwise falls back
    to exposing all bus handlers directly.

    Args:
        server: A FastMCP server instance.
        kernel: A booted Kernel instance.

    Returns:
        Number of tools registered.
    """
    bus = kernel.bus
    gateway = kernel.registry.require_optional("IGateway")
    count = 0

    entries = []
    if gateway:
        surface = gateway.get_surface("mcp")
        if surface:
            entries = surface.entries

    if entries:
        for entry in entries:
            _register_tool(server, entry.runnable_name, entry.description,
                          entry.params_model, bus)
            count += 1
        log.info("mount_mcp: %d tools from gateway surface", count)
    else:
        # No gateway — register all bus handlers as tools
        for handler_name in bus.handlers:
            _register_tool(server, handler_name, f"Invoke {handler_name}", None, bus)
            count += 1
        log.info("mount_mcp: %d tools from bus handlers (no gateway)", count)

    return count


def _register_tool(server, runnable_name: str, description: str,
                   params_model, bus) -> None:
    """Register a single kernel runnable as an MCP tool on a FastMCP server."""
    if _HAS_FASTMCP and hasattr(server, 'tool'):
        # FastMCP — use the @server.tool() pattern
        @server.tool(name=runnable_name, description=description or f"Invoke {runnable_name}")
        async def _handler(**kwargs) -> Any:
            return await bus.invoke(runnable_name, kwargs)
    else:
        # Generic MCP server — store tool definition for manual wiring
        if not hasattr(server, '_kernel_tools'):
            server._kernel_tools = []
        schema = {}
        if params_model and hasattr(params_model, 'model_json_schema'):
            schema = params_model.model_json_schema()
        server._kernel_tools.append({
            "name": runnable_name,
            "description": description or f"Invoke {runnable_name}",
            "input_schema": schema,
        })


def _build_tool_list(kernel) -> list[dict]:
    """Build a list of tool definitions from the kernel's gateway or bus."""
    bus = kernel.bus
    gateway = kernel.registry.require_optional("IGateway")
    tools = []

    if gateway:
        surface = gateway.get_surface("mcp")
        if surface:
            for entry in surface.entries:
                schema = {}
                if entry.params_model and hasattr(entry.params_model, 'model_json_schema'):
                    schema = entry.params_model.model_json_schema()
                tools.append({
                    "name": entry.runnable_name,
                    "description": entry.description or f"Invoke {entry.short_name}",
                    "group": entry.group,
                    "input_schema": schema,
                })
            return tools

    # Fallback: all bus handlers
    for handler_name in bus.handlers:
        tools.append({
            "name": handler_name,
            "description": f"Invoke {handler_name}",
            "group": "",
            "input_schema": {"type": "object", "properties": {}},
        })
    return tools


# ── Container mode: SignalPy manages the MCP server ───────────────

@component("mcp-transport", version="0.3", depends=["gateway"])
@provides("IMCPServer")
@requires(config="IConfig", gateway="IGateway")
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
        self._bus = rt.bus
        self._tools = _build_tool_list_from_gateway(rt.gateway, rt.bus)

        if _HAS_FASTMCP:
            self._server = _FastMCP(
                rt.config.get("mcp.name", "signalpy-kernel")
            )
            for tool in self._tools:
                _register_tool(self._server, tool["name"], tool["description"],
                              None, rt.bus)
            log.info("MCP transport: %d tools on FastMCP server", len(self._tools))
        else:
            self._server = None
            log.info("MCP transport: %d tools (no FastMCP — use list_tools())",
                     len(self._tools))

    async def handle_tool_call(self, tool_name: str, arguments: dict) -> Any:
        """Invoke a kernel runnable by tool name."""
        return await self._bus.invoke(tool_name, arguments)

    def list_tools(self) -> list[dict]:
        """Return the tool definitions."""
        return list(self._tools)

    def get_server(self):
        """Return the FastMCP server instance (if FastMCP is installed)."""
        return self._server

    @lifecycle.deactivate
    def deactivate(self, rt):
        pass


def _build_tool_list_from_gateway(gateway, bus) -> list[dict]:
    """Build tool list from gateway surface."""
    tools = []
    surface = gateway.get_surface("mcp") if gateway else None
    if surface:
        for entry in surface.entries:
            schema = {}
            if entry.params_model and hasattr(entry.params_model, 'model_json_schema'):
                schema = entry.params_model.model_json_schema()
            tools.append({
                "name": entry.runnable_name,
                "description": entry.description or f"Invoke {entry.short_name}",
                "group": entry.group,
                "input_schema": schema,
            })
    else:
        for handler_name in bus.handlers:
            tools.append({
                "name": handler_name,
                "description": f"Invoke {handler_name}",
                "group": "",
                "input_schema": {"type": "object", "properties": {}},
            })
    return tools
