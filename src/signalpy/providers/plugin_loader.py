"""PluginLoader — watch a directory, auto-discover and hot-load components.

Provides: IPluginLoader
Requires: IConfig

Scans a directory for .py files, imports them, finds @component classes,
and calls kernel.hot_add() or kernel.hot_update() as appropriate.

The kernel reference is passed via properties at instantiation time.
This is a platform component — it's reasonable for it to have kernel access.
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle
from signalpy.kernel.component import has_meta, get_meta
log = logging.getLogger(__name__)


@component("plugin-loader", version="0.1")
@provides("IPluginLoader")
@requires(config="IConfig")
class PluginLoader:
    """Watches a directory for .py files and hot-loads @component classes.

    Usage:
        kernel.instantiate("plugin-loader", properties={
            "plugin_dir": "/path/to/plugins",
            "kernel": kernel,  # direct reference for hot_add/hot_update
        })

    Or via schema.handler:
        schema = next(s for s in kernel.runnables() if s.name == "scan")
        await schema.handler({})
    """

    @lifecycle.activate
    def activate(self):
        self._kernel = self.rt.properties.get("kernel")
        self._plugin_dir = self.rt.properties.get(
            "plugin_dir",
            self.rt.config.get("plugins.dir", ""),
        )
        self._loaded_modules: dict[str, Any] = {}   # filename → module
        self._loaded_factories: dict[str, str] = {}  # factory_name → filename
        self.load_log: list[str] = []

    async def scan(self, params=None):
        """Scan the plugin directory. New files → hot_add. Changed files → hot_update."""
        if not self._plugin_dir:
            return {"error": "No plugin_dir configured"}
        if self._kernel is None:
            return {"error": "No kernel reference — pass kernel in properties"}

        plugin_path = Path(self._plugin_dir)
        if not plugin_path.is_dir():
            return {"error": f"Plugin dir does not exist: {plugin_path}"}

        results = []
        for py_file in sorted(plugin_path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            result = await self._load_file(py_file)
            results.extend(result)

        return {"loaded": results}

    async def load_module(self, module_path: str):
        """Import a module by dotted path and hot-add its components."""
        if self._kernel is None:
            return {"error": "No kernel reference"}
        try:
            mod = importlib.import_module(module_path)
            classes = _find_components(mod)
            results = []
            for cls in classes:
                result = await self._add_or_update(cls, module_path)
                results.append(result)
            return {"loaded": results}
        except Exception as e:
            log.exception("Failed to load module: %s", params.module_path)
            return {"error": str(e)}

    async def status(self):
        return {
            "plugin_dir": self._plugin_dir,
            "loaded_modules": list(self._loaded_modules.keys()),
            "loaded_factories": dict(self._loaded_factories),
            "load_log": list(self.load_log),
        }

    async def _load_file(self, py_file: Path) -> list[dict]:
        """Import a .py file, find components, hot_add or hot_update."""
        filename = py_file.name
        module_name = f"_plugin_{py_file.stem}"

        # Import or reload
        # Always re-import from file (handles both new and updated files)
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                return [{"file": filename, "error": "Could not create module spec"}]
            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)
            self._loaded_modules[filename] = mod
        except Exception as e:
            log.exception("Failed to import: %s", filename)
            return [{"file": filename, "error": str(e)}]

        # Find @component classes in the module
        classes = _find_components(mod)
        results = []
        for cls in classes:
            result = await self._add_or_update(cls, filename)
            results.append(result)
        return results

    async def _add_or_update(self, cls: type, source: str) -> dict:
        """Hot-add a new component or hot-update an existing one."""
        meta = get_meta(cls)
        factory_name = meta.factory_name

        if factory_name in self._loaded_factories:
            # Already loaded — hot_update
            try:
                await self._kernel.hot_update(cls)
                self._loaded_factories[factory_name] = source
                msg = f"updated:{factory_name}"
                self.load_log.append(msg)
                log.info("Plugin updated: %s from %s", factory_name, source)
                return {"factory": factory_name, "action": "updated", "source": source}
            except Exception as e:
                log.exception("Failed to hot_update: %s", factory_name)
                return {"factory": factory_name, "error": str(e)}
        else:
            # New — hot_add
            try:
                await self._kernel.hot_add(cls)
                self._loaded_factories[factory_name] = source
                msg = f"added:{factory_name}"
                self.load_log.append(msg)
                log.info("Plugin added: %s from %s", factory_name, source)
                return {"factory": factory_name, "action": "added", "source": source}
            except Exception as e:
                log.exception("Failed to hot_add: %s", factory_name)
                return {"factory": factory_name, "error": str(e)}


def _find_components(module) -> list[type]:
    """Scan a module for @component-decorated classes."""
    return [
        obj for obj in vars(module).values()
        if isinstance(obj, type) and has_meta(obj)
    ]
