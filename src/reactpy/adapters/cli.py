"""CLI Transport — renders the gateway's API surface as Click commands.

Consumes: IGateway (the composed API surface)
Provides: ICLI
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from reactpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)


@component("cli-transport", version="0.2", depends=["gateway"])
@provides("ICLI")
@requires(config="IConfig", gateway="IGateway")
class CLITransport:

    @lifecycle.activate
    def activate(self, rt):
        self._bus = rt.bus
        self._gateway = rt.gateway
        log.info("CLI transport: ready")

    def build_cli(self, kernel: Any = None):
        import click

        @click.group()
        def cli():
            """Microkernel CLI"""
            pass

        # Build subcommands from gateway's CLI surface
        surface = self._gateway.get_surface("cli")
        if surface:
            for group_name, entries in surface.groups().items():
                @cli.group(name=group_name)
                def group_cmd():
                    pass

                for entry in entries:
                    _name = entry.runnable_name
                    _desc = entry.description or f"Invoke {entry.short_name}"
                    _bus = self._bus

                    @group_cmd.command(name=entry.short_name, help=_desc)
                    @click.argument("params", nargs=-1)
                    def run_cmd(params, name=_name, bus=_bus):
                        parsed = {}
                        for p in params:
                            if "=" in p:
                                k, v = p.split("=", 1)
                                try:
                                    v = json.loads(v)
                                except (json.JSONDecodeError, ValueError):
                                    pass
                                parsed[k] = v
                        try:
                            result = asyncio.run(bus.invoke(name, parsed))
                            click.echo(json.dumps(result, indent=2, default=str))
                        except Exception as exc:
                            click.echo(f"Error: {exc}", err=True)

        # Flat run command as fallback
        all_runnables = []
        for s in self._gateway.surfaces().values():
            all_runnables.extend(e.runnable_name for e in s.entries)
        all_runnables = sorted(set(all_runnables))

        @cli.command()
        @click.argument("runnable_name")
        @click.argument("params", nargs=-1)
        def run(runnable_name, params):
            """Invoke any runnable by full name."""
            parsed = {}
            for p in params:
                if "=" in p:
                    k, v = p.split("=", 1)
                    try:
                        v = json.loads(v)
                    except (json.JSONDecodeError, ValueError):
                        pass
                    parsed[k] = v
            try:
                result = asyncio.run(self._bus.invoke(runnable_name, parsed))
                click.echo(json.dumps(result, indent=2, default=str))
            except Exception as exc:
                click.echo(f"Error: {exc}", err=True)

        @cli.command()
        def runnables():
            """List all available runnables."""
            for name in all_runnables:
                click.echo(f"  {name}")

        @cli.command()
        def status():
            """Show kernel status."""
            if kernel:
                click.echo(json.dumps(kernel.status(), indent=2, default=str))

        return cli

    @lifecycle.deactivate
    def deactivate(self, rt):
        pass
