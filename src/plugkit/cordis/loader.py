"""Config-driven plugin loader — port of `@cordisjs/plugin-loader`.

Includes `Entry`, `EntryGroup`, `Group`, `EntryTree`, `Loader`, the `isolate`
plugin, and the `interpolate`/`evaluate` config expression helpers.
"""

from __future__ import annotations

import asyncio
import importlib
import random
from typing import Any, Optional

from .registry import Inject
from .service import Service
from .utils import ChainedDict, unique_symbol, this_

EntryTree_sep = ":"

_UNSET = object()


def _fire(coro):
    """Start a coroutine eagerly when a loop is running (JS promises start
    immediately); otherwise return it unstarted."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return coro
    return asyncio.ensure_future(coro)


# ---------------------------------------------------------------------------
# config utils (evaluate / interpolate)
# ---------------------------------------------------------------------------


def evaluate(ctx, expr: str) -> Any:
    """Port of the JS `with (ctx) { return eval(expr) }` — expressions use
    the `ctx` variable explicitly."""
    return eval(expr, {"ctx": ctx})  # noqa: S307 — config expressions are explicit opt-in


def interpolate(ctx, value: Any) -> Any:
    if is_js_expr(value):
        return evaluate(ctx, value["__jsExpr"])
    if not value or not isinstance(value, (dict, list)):
        return value
    if isinstance(value, list):
        return [interpolate(ctx, item) for item in value]
    return {key: interpolate(ctx, item) for key, item in value.items()}


def is_js_expr(value: Any) -> bool:
    return isinstance(value, dict) and "__jsExpr" in value


def deep_equal(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(deep_equal(x, y) for x, y in zip(a, b))
    return a == b


def _chain_get(ctx, key: str):
    obj = ctx
    while obj is not None:
        if key in obj.__dict__:
            return obj.__dict__[key]
        obj = obj.__dict__.get("_parent")
    return None


def _sort_keys(options: dict) -> None:
    # JS `sortKeys`: id/name first, config last, rest alphabetical
    order = []
    for key in ("id", "name"):
        if key in options:
            order.append((key, options.pop(key)))
    rest = sorted(options.items(), key=lambda item: item[0])
    config = None
    if "config" in options:
        config = ("config", options.pop("config"))
    result = dict(order)
    result.update(rest)
    if config is not None:
        result[config[0]] = config[1]
    options.clear()
    options.update(result)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


class Entry:
    key = "__cordis_entry__"

    def __init__(self, loader: "Loader"):
        self.loader = loader
        self.ctx = loader.ctx.extend({Entry.key: self})
        self.fiber: Optional[Any] = None
        self.options: dict = {}
        self.subgroup: Optional["EntryGroup"] = None
        self.subtree: Optional["EntryTree"] = None
        self._init_task = None
        self.realm = None
        self.ctx.emit("loader/entry-init", self)

    @property
    def context(self):
        return self.ctx

    @property
    def id(self) -> str:
        id_ = self.options.get("id")
        tree_entry = self.parent.tree.ctx.fiber.entry
        if tree_entry is not None:
            id_ = tree_entry.id + EntryTree_sep + id_
        return id_

    @property
    def disabled(self) -> bool:
        if self.options.get("group"):
            return False
        entry: Optional[Entry] = self
        while entry is not None:
            if entry.options.get("disabled"):
                return True
            entry = entry.parent.ctx.fiber.entry
        return False

    def evaluate(self, expr: str):
        return evaluate(self.ctx, expr)

    def _resolve_config(self, plugin):
        if getattr(plugin, "__cordis_group__", None):
            return self.options.get("config")
        return interpolate(self.ctx, self.options.get("config"))

    def _patch_context(self, diff: list):
        entry = self

        def default(*args):
            # JS: Object.setPrototypeOf(this.ctx, this.parent.ctx)
            entry.ctx.__dict__["_parent"] = entry.parent.ctx
            if entry.fiber is not None and entry.fiber.uid is not None and (
                "config" in diff or entry.options.get("group")
            ):
                entry.fiber.update(entry._resolve_config(entry.fiber.runtime.callback), True)

        self.context.waterfall("loader/patch-context", self, default)

    async def refresh(self):
        if self.fiber is not None:
            return
        if self.disabled:
            return
        await self.init()

    async def update(self, options: dict, create: bool = False, force: bool = False):
        legacy = dict(self.options)

        if create:
            self.options = options
        else:
            for key, value in options.items():
                if value is None:
                    self.options.pop(key, None)
                else:
                    self.options[key] = value
        _sort_keys(self.options)

        if self.disabled:
            if self.fiber is not None:
                self.fiber.dispose()
                # JS `await` drains the disposal chain (unload -> group stop
                # -> nested disposes) before the caller resumes
                for _ in range(20):
                    await asyncio.sleep(0)
            return

        if self.fiber is not None and self.fiber.uid is not None:
            diff = [
                key
                for key in set(self.options) | set(legacy)
                if not deep_equal(self.options.get(key), legacy.get(key))
            ]
            if not diff and not force:
                return
            self.context.emit("loader/partial-dispose", self, legacy, True)
            self._patch_context(diff)
            # JS `await` settles the restart's disposal chain (unload -> group
            # stop -> nested disposes) before the caller resumes
            for _ in range(20):
                await asyncio.sleep(0)
        else:
            await self.init()

    def get_outer_stack(self):
        entry: Optional[Entry] = self
        result = []
        while entry is not None:
            result.append(f"    at {entry.parent.tree.ctx.baseUrl}#{entry.options.get('id')}")
            entry = entry.parent.ctx.fiber.entry
        return result

    async def init(self):
        try:
            if self._init_task is None:
                self._init_task = asyncio.ensure_future(self._init())
            await self._init_task
            # JS `await promise` drains the microtask queue FIFO, so a child
            # fiber's reload — including a group plugin's constructor, its
            # first `__cordis_init__` yield, and the sync prefix of its
            # `update()` — settles before `create()` returns.  Flush bounded
            # loop iterations to reach the same ordering; genuinely pending
            # fibers (e.g. a LOADING plugin awaiting a future) stay pending.
            for _ in range(20):
                await asyncio.sleep(0)
        finally:
            self._init_task = None

        fiber = self.fiber
        if fiber is not None:
            # JS: `this.fiber.await().finally(...)` — fire-and-forget
            entry = self

            def done(task):
                if task.cancelled():
                    return
                task.exception()  # the fiber's own errors are already logged
                if not entry.loader.get_tasks():
                    entry.ctx.reflect.notify(["loader"])

            task = asyncio.ensure_future(fiber.await_())
            task.add_done_callback(done)

    async def _init(self):
        try:
            exports = await self.parent.tree.import_(self.options.get("name"), self.get_outer_stack)
        except BaseException as error:
            self.ctx.logger.error(error)
            return
        plugin = self.loader.unwrap_exports(exports)
        self._patch_context([])
        self.loader.show_log(self, "apply")
        self.fiber = self.ctx.registry.plugin(plugin, self._resolve_config(plugin), self.get_outer_stack)


# ---------------------------------------------------------------------------
# EntryGroup / Group
# ---------------------------------------------------------------------------


class EntryGroup:
    key = "__cordis_group__"

    def __init__(self, ctx, tree: "EntryTree"):
        self.ctx = ctx
        self.tree = tree
        self.data: list = []
        entry = ctx.fiber.entry
        if entry is not None:
            entry.subgroup = self

    @property
    def context(self):
        return self.ctx

    async def create(self, options: dict):
        id_ = self.tree.ensure_id(options)
        entry = self.tree.store.get(id_)
        if entry is None:
            entry = Entry(self.ctx.loader)
            self.tree.store[id_] = entry
        entry.parent = self
        await entry.update(options, True, True)
        return entry.id

    def unlink(self, options: dict):
        try:
            self.data.remove(options)
        except ValueError:
            pass

    def remove(self, id_: str, is_dispose: bool = False):
        entry = self.tree.store.get(id_)
        if entry is None:
            return
        if entry.fiber is not None:
            entry.fiber.dispose()
        if not is_dispose:
            self.unlink(entry.options)
        self.tree.store.pop(id_, None)
        self.context.emit("loader/partial-dispose", entry, entry.options, False)

    async def update(self, config: list):
        old_config = self.data
        self.data = config
        old_map = {item.get("id") or "undefined": item for item in old_config}
        new_map = {item.get("id") or "undefined": item for item in config}
        merged = dict(old_map)
        merged.update(new_map)
        ids = list(merged)
        for id_ in ids:
            if id_ in new_map:
                try:
                    await self.create(new_map[id_])
                except BaseException as error:
                    self.ctx.logger.error(error)
            else:
                self.remove(id_)
                # JS `Promise.all` interleaves: the removal's disposal chain
                # (including a group's `stop()`) settles before later creates
                # resume.  Flush pending event-loop work to match.
                for _ in range(20):
                    await asyncio.sleep(0)

    def stop(self):
        for options in list(self.data):
            self.remove(options["id"], True)


class Group(EntryGroup):
    initial = []
    __cordis_group__ = True

    def __init__(self, ctx, config):
        super().__init__(ctx, ctx.fiber.entry.parent.tree)
        self.config = config
        group = self

        def on_update(config, no_save, next_):
            _fire(group.update(config))

        ctx.on("internal/update", on_update)

    def __cordis_init__(self):
        group = self

        async def gen():
            yield lambda: group.stop()
            await group.update(self.config)

        return gen()


# ---------------------------------------------------------------------------
# EntryTree
# ---------------------------------------------------------------------------


class EntryTree:
    sep = EntryTree_sep

    def __init__(self, ctx):
        self.ctx = ctx.extend({"baseUrl": ctx.baseUrl})
        self.root = EntryGroup(self.ctx, self)
        self.store: dict = {}
        entry = self.ctx.fiber.entry
        if entry is not None:
            entry.subtree = self
        self.enable_logs: Optional[bool] = None

    @property
    def context(self):
        return self.ctx

    def entries(self):
        for entry in list(self.store.values()):
            yield entry
            if entry.subtree is None:
                continue
            yield from entry.subtree.entries()

    def get_tasks(self):
        result = []
        for entry in self.entries():
            task = entry._init_task
            if task is None and entry.fiber is not None:
                task = entry.fiber.inertia
            if task is not None:
                result.append(task)
        return result

    async def await_(self):
        while True:
            tasks = self.get_tasks()
            if not tasks:
                return
            await asyncio.gather(*(asyncio.ensure_future(task) for task in tasks), return_exceptions=True)

    def ensure_id(self, options: dict) -> str:
        if not options.get("id"):
            while True:
                id_ = random.Random().getrandbits(32).to_bytes(4, "big").hex()
                if id_ not in self.store:
                    options["id"] = id_
                    break
        return options["id"]

    def resolve(self, id_: str) -> Entry:
        parts = id_.split(self.sep)
        tree: Optional[EntryTree] = self
        final = parts.pop()
        for part in parts:
            entry = tree.store.get(part)
            if entry is None:
                raise Exception(f"cannot resolve entry {id_}")
            tree = entry.subtree
        entry = tree.store.get(final)
        if entry is None:
            raise Exception(f"cannot resolve entry {id_}")
        return entry

    def resolve_group(self, id_: Optional[str]) -> EntryGroup:
        if not id_:
            return self.root
        entry = self.resolve(id_)
        if entry.subgroup is None:
            raise Exception(f"entry {id_} is not a group")
        return entry.subgroup

    async def create(self, options: dict, parent: Optional[str] = None, position: int = float("inf")):
        group = self.resolve_group(parent)
        if isinstance(position, float):
            group.data.append(options)
        else:
            group.data.insert(position, options)
        group.tree.write()
        return await group.create(options)

    def remove(self, id_: str):
        entry = self.resolve(id_)
        entry.parent.remove(id_)
        entry.parent.tree.write()

    async def update(self, id_: str, options: dict, parent=_UNSET, position: Optional[int] = None):
        entry = self.resolve(id_)
        source = entry.parent
        if parent is not _UNSET:
            target = self.resolve_group(parent)
            source.unlink(entry.options)
            if position is None:
                target.data.append(entry.options)
            else:
                target.data.insert(position, entry.options)
            target.tree.write()
            entry.parent = target
        source.tree.write()
        await entry.update(options, False, True)

    async def import_(self, name: str, get_outer_stack=None):
        if name.startswith("cordis:"):
            return self.ctx.loader.builtins[name[7:]]
        if name == "@cordisjs/plugin-group":
            from .loader import Group

            return Group
        if name == "@cordisjs/plugin-include":
            from .include import Include

            return Include
        try:
            return importlib.import_module(name)
        except ImportError:
            raise

    def write(self):  # pragma: no cover — abstract
        raise NotImplementedError


# ---------------------------------------------------------------------------
# isolate plugin
# ---------------------------------------------------------------------------


class Realm:
    def __init__(self):
        self.store: dict = {}

    @property
    def suffix(self):
        raise NotImplementedError

    def access(self, key: str, create: bool = False):
        if key not in self.store:
            self.store[key] = unique_symbol(f"{key}{self.suffix}")
        return self.store[key]

    def delete(self, key: str):
        self.store.pop(key, None)

    @property
    def size(self) -> int:
        return len(self.store)


class LocalRealm(Realm):
    def __init__(self, entry: Entry):
        super().__init__()
        self.entry = entry

    @property
    def suffix(self):
        return "#" + self.entry.options.get("id", "")


class GlobalRealm(Realm):
    def __init__(self, label: str):
        super().__init__()
        self.label = label

    @property
    def suffix(self):
        return "@" + self.label


def isolate(ctx, config=None):
    """The `isolate` plugin — per-entry service realms."""
    realms: dict = {}
    delims: dict = {}

    def access(entry: Entry, name: str, create: bool = False):
        label = (entry.options.get("isolate") or {}).get(name)
        if not label:
            return None
        realm = None
        if label is True:
            if entry.realm is None:
                entry.realm = LocalRealm(entry)
            realm = entry.realm
        elif create:
            if label not in realms:
                realms[label] = GlobalRealm(label)
            realm = realms[label]
        else:
            realm = realms.get(label)
        if realm is None:
            return None
        return realm.access(name, create)

    def on_entry_init(entry):
        # JS: `Object.create(...)` — live chains, not copies
        entry.ctx.__dict__["_intercept"] = ChainedDict(parent=entry.ctx._effective_intercept())
        entry.ctx.__dict__["_isolate"] = ChainedDict(parent=entry.ctx._effective_isolate())

    ctx.on("loader/entry-init", on_entry_init)

    def patch_context(entry, next_):
        # step 1: generate new isolate map
        new_map = ChainedDict(parent=entry.parent.ctx._effective_isolate())
        for name in entry.options.get("isolate") or {}:
            value = access(entry, name, True)
            if value is not None:
                new_map[name] = value

        # step 2: generate service diff
        diff = {}
        old_map = entry.ctx.__dict__["_isolate"]
        for name in set(new_map) | set(delims):
            if new_map.get(name) is old_map.get(name):
                continue
            delim = delims.get(name)
            if delim is None:
                delim = delims[name] = unique_symbol(f"delim:{name}")
            entry.ctx.__dict__[delim.name] = unique_symbol(f"{name}#{entry.id}")
            for symbol_key in (old_map.get(name), new_map.get(name)):
                impl = entry.ctx.reflect.store.get(symbol_key) if symbol_key is not None else None
                if impl is None:
                    continue
                if impl.fiber is None:
                    entry.ctx.logger.warn(Exception(f"expected service {name} to be implemented"))
                    continue
                diff[name] = [old_map.get(name), new_map.get(name), entry.ctx.__dict__[delim.name], _chain_get(impl.fiber.ctx, delim.name)]
                if entry.ctx.__dict__[delim.name] is not _chain_get(impl.fiber.ctx, delim.name):
                    break

        # step 3: set prototype for transferred context
        entry.ctx.__dict__["_parent"] = entry.parent.ctx
        # JS `swap()` mutates the maps in place so children that chain to the
        # same map object stay live
        isolate_map = entry.ctx.__dict__["_isolate"]
        isolate_map.clear()
        isolate_map.update(new_map)
        isolate_map._parent = entry.parent.ctx._effective_isolate()
        intercept_map = entry.ctx.__dict__["_intercept"]
        intercept_map.clear()
        intercept_map.update(entry.options.get("intercept") or {})
        intercept_map._parent = entry.parent.ctx._effective_intercept()

        # step 4: reload fiber
        next_()

        # step 5: replace service impl
        for symbol1, symbol2, flag1, flag2 in diff.values():
            if flag1 is flag2 and entry.ctx.reflect.store.get(symbol1) and not entry.ctx.reflect.store.get(symbol2):
                entry.ctx.reflect.store[symbol2] = entry.ctx.reflect.store[symbol1]
                entry.ctx.reflect.store.pop(symbol1, None)

        # step 6: reflect notify
        ctx.reflect.notify(
            list(diff.keys()),
            lambda target, name: _isolate_filter(target, name, diff, delims),
        )

        # step 7: clean up delimiters
        for name in list(delims):
            if name not in new_map:
                entry.ctx.__dict__.pop(delims[name].name, None)

    ctx.on("loader/patch-context", patch_context)

    def partial_dispose(entry, legacy, active):
        for name, label in (legacy.get("isolate") or {}).items():
            if label is True:
                continue
            if active and (entry.options.get("isolate") or {}).get(name) == label:
                continue
            realm = realms.get(label)
            if realm is None:
                continue
            for other in ctx.loader.entries():
                if (other.options.get("isolate") or {}).get(name) == realm.label:
                    break
            else:
                realm.delete(name)
                if not realm.size:
                    realms.pop(realm.label, None)

    ctx.on("loader/partial-dispose", partial_dispose)


def _isolate_filter(target, name, diff, delims):
    symbol1, symbol2, flag1, flag2 = diff[name]
    symbol3 = target._effective_isolate().get(name)
    flag3 = _chain_get(target, delims[name].name)
    return (symbol1 is symbol3 or symbol2 is symbol3) and (flag1 is flag3) != (flag1 is flag2)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class Loader(EntryTree):
    def __init__(self, ctx, config: Optional[dict] = None):
        config = dict(config or {})
        super().__init__(ctx)
        if config.get("baseUrl"):
            self.ctx.baseUrl = config["baseUrl"]
        self.name = "loader"
        self.internal = None
        self.builtins: dict = {}
        self.__cordis_tracker__ = {"associate": "loader", "property": "ctx", "noShadow": True}

        ctx.reflect.provide("loader", self, getattr(type(self), "__cordis_check__", None))

        loader = self

        def save_config(config, no_save, next_):
            this = this_()  # the carrier is the fiber
            if this.entry is None or no_save or this.parent.fiber.entry is this.entry:
                return next_()
            this.entry.options["config"] = config
            this.entry.parent.tree.write()
            return next_()

        ctx.on("internal/update", save_config, {"global": True, "prepend": True})

        def log_reload(config, _, next_):
            this = this_()  # the carrier is the fiber
            if this.entry is None or this.parent.fiber.entry is this.entry:
                return next_()
            loader.show_log(this.entry, "reload")
            return next_()

        ctx.on("internal/update", log_reload, {"global": True})

        def on_internal_plugin(fiber):
            # 1. set fiber.entry
            parent_entry = _chain_get(fiber.parent, Entry.key)
            if parent_entry is not None and fiber.entry is None:
                fiber.entry = parent_entry
                Inject.resolve(parent_entry.options.get("inject"), fiber.inject)

            # 2. handle self-dispose
            if fiber.uid:
                return
            if fiber.entry is None:
                return
            if fiber.parent.fiber.entry is fiber.entry:
                return
            if not ctx.registry.has(fiber.runtime.callback):
                return
            if not fiber.entry.parent.tree.ctx.fiber.uid:
                return

            loader.show_log(fiber.entry, "unload")

            if fiber.entry.disabled:
                return

            fiber.entry.options["disabled"] = True
            fiber.entry.parent.tree.write()

        ctx.on("internal/plugin", on_internal_plugin)

        ctx.plugin(isolate)

    def write(self):
        # Loader's root tree is in-memory; writes are no-ops.
        pass

    def __cordis_check__(self):
        config = Service.__cordis_resolve_config__(self)
        if config.get("await") and self.get_tasks():
            return False
        return True

    def show_log(self, entry: Entry, type_: str):
        if entry.options.get("group") or not entry.parent.tree.enable_logs:
            return
        self.ctx.root.logger("loader").info("%s plugin %C", type_, entry.options.get("name"))

    def locate(self, fiber=None):
        if fiber is None:
            fiber = self.ctx.fiber
        while True:
            if fiber.entry is not None:
                return fiber.entry.id
            next_fiber = fiber.parent.fiber
            if fiber is next_fiber:
                return None
            fiber = next_fiber

    def exit(self):
        pass

    def unwrap_exports(self, exports):
        if exports is None:
            return exports
        default = getattr(exports, "default", None)
        if default is not None:
            return default
        return exports
