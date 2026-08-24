"""Port of packages/loader/tests/utils.ts — MockLoader harness."""

import inspect

from plugkit.cordis.loader import Group, Loader
from plugkit.cordis.utils import this_

from .conftest import Mock, sleep


async def install(root):
    await root.plugin(MockLoader)
    await sleep()  # let the isolate child fiber register its listeners
    return root.loader


class MockLoader(Loader):
    def __init__(self, ctx, config=None):
        super().__init__(ctx, config)
        self.data = []
        self.modules = {}

        def internal_get(prop, error, next_):
            this = this_()
            if this.fiber.runtime is None and prop == "loader":
                return this.get(prop)
            return next_()

        ctx.on("internal/get", internal_get)

    def write(self):
        self.data = self.root.data

    async def read(self, data):
        self.data = data
        await self.root.update(data)
        await self.await_()

    async def import_(self, name, get_outer_stack=None):
        import importlib

        if name == "@cordisjs/plugin-group":
            return Group
        if name in self.modules:
            return self.modules[name]
        return importlib.import_module(name)

    def mock(self, name, plugin):
        if callable(plugin):
            plugin_name = getattr(plugin, "__name__", None)
            if not plugin_name or plugin_name == "<lambda>":
                try:
                    plugin.__name__ = name
                except AttributeError:
                    pass
        wrapped = Mock(plugin)
        self.modules[name] = wrapped
        return wrapped

    def expect_enable(self, plugin):
        runtime = self.ctx.registry.get(plugin)
        assert runtime is not None

    def expect_disable(self, plugin):
        runtime = self.ctx.registry.get(plugin)
        assert runtime is None

    def expect_fiber(self, id_):
        entry = self.store[id_]
        assert entry is not None and entry.fiber is not None
        return entry.fiber
