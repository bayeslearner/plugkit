"""MCP Transport — renders the gateway's API surface as MCP tools.

Consumes: IGateway (the composed API surface)
Provides: IMCPServer

The MCP transport reads the "mcp" surface from the gateway and
generates a tool for each entry with typed input_schema.
"""
from __future__ import annotations

import logging
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)


@component("mcp-transport", version="0.2", depends=["gateway"])
@provides("IMCPServer")
@requires(config="IConfig", gateway="IGateway")
class MCPTransport:

    @lifecycle.activate
    def activate(self, rt):
        self._bus = rt.bus
        self._tools: list[dict] = []

        surface = rt.gateway.get_surface("mcp")
        if surface:
            for entry in surface.entries:
                self._tools.append({
                    "name": entry.runnable_name,
                    "description": entry.description or f"Invoke {entry.short_name}",
                    "group": entry.group,
                    "input_schema": {
                        "type": "object",
                        "properties": {},  # populated from params_model in real impl
                    },
                })
            log.info("MCP transport: %d tools", len(self._tools))
        else:
            log.warning("MCP transport: no API surface from gateway")

    async def handle_tool_call(self, tool_name: str, arguments: dict) -> Any:
        return await self._bus.invoke(tool_name, arguments)

    def list_tools(self) -> list[dict]:
        return list(self._tools)

    @lifecycle.deactivate
    def deactivate(self, rt):
        pass
