"""CLI Transport — renders kernel runnables as Click commands.

Spec 011: Commands call schema.handler directly — no bus.invoke.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)


@component("cli-transport", version="0.3")
@provides("ICLI")
@requires(config="IConfig")
class CLITransport:

    @lifecycle.activate
    def activate(self, rt):
        self._rt = rt
        log.info("CLI transport: ready")

    def build_cli(self, kernel: Any = None):
        import click

        @click.group()
        def cli():
            """Microkernel CLI"""
            pass

        if kernel is None:
            return cli

        # Build subcommands from kernel.runnables_by_component
        grouped = kernel.runnables_by_component(transport="cli")
        all_schemas = {}

        for comp_name, info in grouped.items():
            @cli.group(name=comp_name)
            def group_cmd():
                pass

            for schema in info["schemas"]:
                full_name = f"{schema.provider}.{schema.name}"
                all_schemas[full_name] = schema
                _desc = schema.description or f"Invoke {schema.name}"

                @group_cmd.command(name=schema.name, help=_desc)
                @click.argument("params", nargs=-1)
                def run_cmd(params, s=schema):
                    parsed = _parse_params(params)
                    try:
                        result = asyncio.run(s.handler(parsed))
                        click.echo(json.dumps(result, indent=2, default=str))
                    except Exception as exc:
                        click.echo(f"Error: {exc}", err=True)

        # Flat run command as fallback
        for schema in kernel.runnables():
            full_name = f"{schema.provider}.{schema.name}"
            if full_name not in all_schemas:
                all_schemas[full_name] = schema

        @cli.command()
        @click.argument("runnable_name")
        @click.argument("params", nargs=-1)
        def run(runnable_name, params):
            """Invoke any runnable by full name."""
            schema = all_schemas.get(runnable_name)
            if schema is None:
                click.echo(f"Error: No runnable {runnable_name!r}", err=True)
                return
            parsed = _parse_params(params)
            try:
                result = asyncio.run(schema.handler(parsed))
                click.echo(json.dumps(result, indent=2, default=str))
            except Exception as exc:
                click.echo(f"Error: {exc}", err=True)

        @cli.command()
        def runnables():
            """List all available runnables."""
            for name in sorted(all_schemas.keys()):
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


def _parse_params(params: tuple) -> dict:
    """Parse CLI key=value params into a dict."""
    parsed = {}
    for p in params:
        if "=" in p:
            k, v = p.split("=", 1)
            try:
                v = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                pass
            parsed[k] = v
    return parsed
