"""`ctx.loader` — describe an application as a YAML file.

The kernel's `Loader` is abstract: it manages the entry tree, reconciles a new
entry list against the running one, and handles groups and isolation, but it does
not know how to turn the string `"myapp.database"` into a plugin. That is
deliberate — the same tree drives a Python import, a plugin registered by hand,
or something fetched over a network.

`FileLoader` is the concrete one for ordinary Python applications: a plugin name
is an importable module path.

    # app.yml
    - id: db
      name: myapp.database
      config:
        dsn: postgres://prod
    - id: greet
      name: myapp.greeter
      config:
        prefix: hi

    root = Context()
    await load_app(root, "app.yml")
    root.greeter("world")

A module is a plugin when it has an `apply(ctx, config)` function, and may also
carry `name` and `inject`:

    # myapp/greeter.py
    name = "greeter"
    inject = ["database"]

    def apply(ctx, config=None):
        prefix = (config or {}).get("prefix", "hello")
        ctx.provide("greeter", lambda who: f"{prefix} {who}")

Entry order in the file carries no meaning. `inject` decides activation, so a
plugin listed first that needs one listed last waits.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from ..cordis.include import Include
from ..cordis.loader import Group, Loader

__all__ = ["FileLoader", "load_app"]


async def _settle(ticks: int = 10) -> None:
    for _ in range(ticks):
        await asyncio.sleep(0)


class FileLoader(Loader):
    """Provides `ctx.loader`. Resolves plugin names as Python module paths."""

    def __init__(self, ctx, config: dict | None = None):
        super().__init__(ctx, config)
        # Plugins registered by hand, checked before the import path when an
        # entry is created directly. See `register` for the limitation.
        self.modules: dict[str, Any] = {}
        self.data: list = []

    async def import_(self, name: str, get_outer_stack=None) -> Any:
        """Resolve a plugin name to a plugin.

        Order: a hand-registered module, then the group builtin, then an
        ordinary Python import. A dotted path may also name an attribute —
        `myapp.plugins:database` imports `myapp.plugins` and takes `database`
        off it.
        """
        if name in self.modules:
            return self.modules[name]
        if name in ("group", "@cordisjs/plugin-group"):
            return Group
        if ":" in name:
            module_path, _, attribute = name.partition(":")
            return getattr(importlib.import_module(module_path), attribute)
        return importlib.import_module(name)

    def register(self, name: str, plugin: Any) -> None:
        """Make `plugin` resolvable under `name` without importing anything.

        Applies to entries created through `loader.create()`. It does **not**
        reach entries listed inside a YAML file loaded by `Include` — those
        resolve through the include's own tree and fall through to the import
        path. Verified in `test_a_plugin_can_be_registered_by_hand`.

        To substitute a plugin that a file names, give the module the name the
        file expects and put it on the import path.
        """
        self.modules[name] = plugin

    def write(self) -> None:
        """Persist the entry tree. A file-backed app writes through `Include`."""
        self.data = self.root.data

    async def read(self, data: list) -> None:
        self.data = data
        await self.root.update(data)
        await self.await_()


async def load_app(ctx, path: str, **options: Any):
    """Mount `FileLoader` and load `path`. Returns the loader.

    Equivalent to mounting the loader, registering `Include`, and creating an
    include entry, which is three steps nobody wants to remember.

        root = Context()
        await load_app(root, "app.yml")

    `options` are passed to `Include`, so `patches=[...]` works here too.
    """
    await ctx.plugin(FileLoader)
    # The loader mounts child fibers that register `loader/entry-init` and
    # `loader/patch-context` listeners. Creating an entry before they exist
    # raises KeyError('_isolate') from loader.py:patch_context, because
    # entry-init is what puts the isolate map on the entry's context.
    await _settle()
    loader = ctx.loader
    loader.register("__include__", Include)
    await loader.create({"name": "__include__", "config": {"path": path, **options}})
    await _settle(60)      # let every entry in the file finish activating
    return loader
