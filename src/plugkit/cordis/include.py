"""Config-file plugin trees — port of `@cordisjs/plugin-include`."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

from .loader import EntryTree


class JsExpr(dict):
    def __init__(self, expr: str):
        super().__init__(__jsExpr=expr)


def _construct_js(loader, node):
    return JsExpr(node.value)


def _represent_js(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:js", data["__jsExpr"])


# pyyaml is an extra, and importing it here would make `import plugkit` fail on
# the bare install. Resolved on first YAML read or write instead.
_yaml_state: dict = {}


def _yaml():
    """The `yaml` module plus this port's loader, imported on first use."""
    if not _yaml_state:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - covered by test_bare_install
            raise ImportError(
                "reading or writing a YAML plugin tree needs pyyaml — "
                'install it with `pip install "plugkit[providers]"`'
            ) from exc

        class _CordisYamlLoader(yaml.SafeLoader):
            pass

        _CordisYamlLoader.add_constructor("tag:yaml.org,2002:js", _construct_js)
        yaml.add_representer(JsExpr, _represent_js)
        _yaml_state.update(module=yaml, loader=_CordisYamlLoader)
    return _yaml_state["module"], _yaml_state["loader"]

_WRITABLE = {".json": "application/json", ".yaml": "application/yaml", ".yml": "application/yaml"}


class Include(EntryTree):
    inject = ["loader"]

    def __init__(self, ctx, config: dict):
        super().__init__(ctx)
        self.config = config
        tree_ctx = self.ctx
        self.enable_logs = config.get("enableLogs")
        if self.enable_logs is None:
            entry = tree_ctx.fiber.entry
            self.enable_logs = entry.parent.tree.enable_logs if entry is not None else False

        self.filename = os.path.normpath(os.path.join(self.ctx.baseUrl or ".", config.get("path", "")))
        ext = os.path.splitext(self.filename)[1].lower()
        if ext not in _WRITABLE:
            raise Exception(f'extension "{ext}" not supported')
        self.type = _WRITABLE[ext]
        self.readonly = False
        self.ctx.baseUrl = os.path.dirname(self.filename) + os.sep

        self.content: Optional[str] = None
        self.data: Optional[list] = None
        self.write_task: Optional[asyncio.Task] = None

        include = self

        def on_update(config, _, next_):
            if config.get("path") != include.config.get("path"):
                return next_()
            include.root.update(include.data)

        ctx.on("internal/update", on_update)

    async def _check_access(self):
        if not self.type:
            return
        if not os.access(self.filename, os.W_OK):
            self.readonly = True

    async def read(self, forced: bool = False) -> bool:
        with open(self.filename, encoding="utf-8") as file:
            content = file.read()
        if not forced and self.content == content:
            return False
        self.content = content
        if self.type == "application/yaml":
            yaml, loader = _yaml()
            self.data = yaml.load(self.content, Loader=loader)
        else:
            self.data = json.loads(self.content)
        await self._check_access()
        return True

    def apply_patches(self, data: list) -> list:
        patches = self.config.get("patches")
        if not patches:
            return data

        entry_map = {}

        def build_map(entries):
            for entry in entries:
                if entry.get("id"):
                    entry_map[entry["id"]] = entry
                if entry.get("group") and isinstance(entry.get("config"), list):
                    build_map(entry["config"])

        build_map(data)

        for patch in patches:
            patch = dict(patch)
            id_ = patch.pop("id", None)
            insert = patch.pop("insert", None)
            name = patch.pop("name", None)

            if insert is not None:
                if id_:
                    target = entry_map.get(id_)
                    if target is None:
                        self.ctx.root.logger("loader").warn("patch insert: entry %C not found", id_)
                        continue
                    if not target.get("group"):
                        self.ctx.root.logger("loader").warn("patch insert: entry %C is not a group", id_)
                        continue
                    if not isinstance(target.get("config"), list):
                        target["config"] = []
                    target["config"].extend(insert)
                else:
                    data.extend(insert)
                continue

            if not id_:
                self.ctx.root.logger("loader").warn("patch: id is required for non-insert patches")
                continue

            target = entry_map.get(id_)
            if target is None:
                self.ctx.root.logger("loader").warn("patch: entry %C not found", id_)
                continue

            if name is not None and name != target.get("name"):
                self.ctx.root.logger(
                    "loader",
                ).warn("patch: name mismatch for %C (expected %C, got %C), skipping", id_, target.get("name"), name)
                continue

            for key, value in patch.items():
                if key == "id":
                    continue
                target[key] = value

        return data

    def __cordis_init__(self):
        include = self

        async def gen():
            try:
                await self.read()
            except Exception:
                if self.config.get("initial"):
                    self.write_file(self.config["initial"])
                    await self.read()
                else:
                    raise Exception(f"config file not found: {self.filename}")

            yield lambda: include.stop()
            await self.root.update(self.apply_patches(list(self.data)))

        return gen()

    def stop(self):
        self.root.stop()

    async def refresh(self):
        if not await self.read():
            return
        await self.root.update(self.data)

    def _write_file(self, config: list):
        if self.readonly:
            raise Exception("cannot overwrite readonly config")
        if self.type == "application/yaml":
            yaml, _ = _yaml()
            self.content = yaml.dump(config, Dumper=yaml.SafeDumper, allow_unicode=True, sort_keys=False)
        else:
            self.content = json.dumps(config, indent=2)
        with open(self.filename + ".tmp", "w", encoding="utf-8") as file:
            file.write(self.content)
        os.replace(self.filename + ".tmp", self.filename)

    def write_file(self, config: list):
        include = self

        async def task():
            include.write_task = None
            include._write_file(config)

        if self.write_task is not None:
            self.write_task.cancel()
        self.write_task = asyncio.ensure_future(task())

    def write(self):
        self.context.emit("loader/config-update")
        return self.write_file(self.root.data)
