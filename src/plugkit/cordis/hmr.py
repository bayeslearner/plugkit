"""Hot module replacement — the Python equivalent of `@cordisjs/plugin-hmr`.

The JS package drives Node's internal ESM module loader and watches files
with chokidar.  The Python port watches the file system event-driven via
`watchdog` when it is installed (extra: `cordis[hmr]`), with an mtime-polling
fallback so the zero-dependency installation keeps working.

The reload itself is transactional, mirroring the JS engine's swap: every
changed module is re-imported *before* any fiber is disposed (the previous
module object is kept as backup), and a failure at any point restores the
backup modules and rebuilds the old fibers, so the system is never left
half-reloaded.  One limitation is inherent to Python's import system: the JS
engine classifies the whole import graph and transitively invalidates stale
entries, while `importlib` cannot rebind the references other modules hold,
so only the entries whose own module changed are reloaded here.
"""

from __future__ import annotations

import fnmatch
import importlib
import os
import sys
from typing import Any, Optional

from .service import Service

try:  # optional dependency — enables the event-driven watcher
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - covered by test_bare_install
    FileSystemEventHandler = None
    Observer = None

WATCHDOG_AVAILABLE = Observer is not None

# A class body runs at import, so `class _WatchHandler(None)` would make
# `import plugkit` fail on the bare install rather than fall back to polling.
_WatchBase = FileSystemEventHandler if WATCHDOG_AVAILABLE else object

_DEFAULT_IGNORED = ("**/node_modules", "**/.*", "cache", "data")


class _WatchHandler(_WatchBase):
    def __init__(self, hmr: "Hmr", is_ignored):
        self._hmr = hmr
        self._is_ignored = is_ignored

    def on_modified(self, event):
        if self._hmr._stopped or event.is_directory:
            return
        path = event.src_path
        if not self._is_ignored(path):
            self._hmr._on_file_event(path)

    def on_created(self, event):
        self.on_modified(event)

    def on_moved(self, event):
        path = event.dest_path
        if not self._is_ignored(path):
            self._hmr._on_file_event(path)


class Hmr(Service):
    inject = ["loader", "timer"]

    def __init__(self, ctx, config: Optional[dict] = None):
        super().__init__(ctx, "hmr")
        self.config = dict(config or {})
        self._snapshots: dict = {}
        self._dispose_timer = None
        self._observer = None
        self._debounced = None
        self._stashed: set = set()
        self._stopped = False

    def __cordis_init__(self):
        hmr = self

        async def gen():
            yield lambda: hmr.stop()
            hmr.start()

        return gen()

    def start(self):
        self.watch()
        if WATCHDOG_AVAILABLE:
            self._start_observer()
        else:
            self._dispose_timer = self.ctx.setInterval(self._poll, self.config.get("debounce", 100))

    def stop(self):
        self._stopped = True
        if self._dispose_timer is not None:
            self._dispose_timer()
            self._dispose_timer = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    # ------------------------------------------------------------------
    # watching
    # ------------------------------------------------------------------

    def _base_dir(self) -> str:
        # JS: `resolve(config.base || '.', ctx.baseUrl)`
        base = self.config.get("base") or "."
        if os.path.isabs(base):
            return os.path.abspath(base)
        ctx_base = getattr(self.ctx, "baseUrl", None) or os.getcwd()
        return os.path.abspath(os.path.join(ctx_base, base))

    def _is_ignored(self, path: str) -> bool:
        if os.path.basename(path).startswith("."):
            return True
        rel = os.path.relpath(path, self._base_dir())
        parts = rel.split(os.sep)
        if any(part in ("node_modules", "cache", "data") for part in parts):
            return True
        patterns = self.config.get("ignored") or list(_DEFAULT_IGNORED)
        for pattern in patterns:
            normalized = pattern.lstrip("/")
            if fnmatch.fnmatch(rel, normalized) or fnmatch.fnmatch(rel, normalized.rstrip("/") + "/*"):
                return True
        return False

    def _start_observer(self):
        base = self._base_dir()
        handler = _WatchHandler(self, self._is_ignored)
        self._observer = Observer()
        roots = self.config.get("root") or ["."]
        for root in roots:
            path = root if os.path.isabs(root) else os.path.join(base, root)
            if not os.path.isdir(path):
                self.ctx.logger.warn("hmr: watch root does not exist: %s", path)
                continue
            self._observer.schedule(handler, path, recursive=True)
        self._observer.start()
        self.ctx.logger.info("watching %o in %s", roots, base)

    def _get_mtime(self, name: str):
        try:
            module = importlib.import_module(name)
        except ImportError:
            return None
        filename = getattr(module, "__file__", None)
        if not filename:
            return None
        try:
            return os.path.getmtime(filename)
        except OSError:
            return None

    def watch(self):
        for entry in self.ctx.loader.entries():
            name = entry.options.get("name")
            if not name or name.startswith(("cordis:", "@")):
                continue
            self._snapshots[name] = self._get_mtime(name)

    # ------------------------------------------------------------------
    # event-driven path
    # ------------------------------------------------------------------

    def _on_file_event(self, path: str):
        if self._stopped:
            return
        if self._debounced is None:
            self._debounced = self.ctx.debounce(self._flush, self.config.get("debounce", 100))
        self._stashed.add(os.path.realpath(path))
        self._debounced()

    def _flush(self):
        stashed, self._stashed = self._stashed, set()
        if not stashed:
            return
        self.watch()
        names = self._match(stashed)
        if names:
            self.reload(names)
        self.ctx.emit("hmr/change", names)

    def _match(self, paths: set) -> list:
        matched = []
        for entry in self.ctx.loader.entries():
            name = entry.options.get("name")
            if not name or name.startswith(("cordis:", "@")):
                continue
            try:
                module = importlib.import_module(name)
            except ImportError:
                continue
            filename = getattr(module, "__file__", None)
            if not filename:
                continue
            real = os.path.realpath(filename)
            if any(real == os.path.realpath(path) for path in paths):
                matched.append(name)
        return matched

    # ------------------------------------------------------------------
    # polling fallback
    # ------------------------------------------------------------------

    def _poll(self):
        # watch dynamically — entries may load after the HMR service starts
        for entry in self.ctx.loader.entries():
            name = entry.options.get("name")
            if not name or name.startswith(("cordis:", "@")):
                continue
            if name not in self._snapshots:
                self._snapshots[name] = self._get_mtime(name)
        changed = []
        for name, mtime in list(self._snapshots.items()):
            current = self._get_mtime(name)
            if current is not None and current != mtime:
                changed.append(name)
                self._snapshots[name] = current
        if changed:
            self.reload(changed)
        self.ctx.emit("hmr/change", changed)

    # ------------------------------------------------------------------
    # reloading
    # ------------------------------------------------------------------

    def reload(self, names: list):
        loader = self.ctx.loader
        entries = [
            entry
            for entry in loader.entries()
            if entry.options.get("name") in names and entry.fiber is not None
        ]
        if not entries:
            return

        # Phase 1 — re-import every changed module before disposing anything.
        # `importlib.reload` re-executes the code inside the existing module
        # object, so a failure would leave it half-updated with no way back;
        # popping the cache and importing afresh instead leaves the previous
        # module object untouched, serving as the rollback backup exactly like
        # the JS engine's cache invalidate/restore.
        backup: dict = {}
        attempts: dict = {}
        try:
            for name in names:
                backup[name] = sys.modules.get(name)
                if backup[name] is not None:
                    sys.modules.pop(name, None)
                attempts[name] = loader.unwrap_exports(importlib.import_module(name))
        except BaseException as error:
            for name, old_module in backup.items():
                if old_module is not None:
                    sys.modules[name] = old_module
            self.ctx.logger.warn(error)
            return

        # Phase 2 — swap the stale entries' fibers, rolling back on failure.
        swapped: list = []
        try:
            for entry in entries:
                name = entry.options["name"]
                old_fiber = entry.fiber
                try:
                    self.ctx.registry.delete(old_fiber.runtime.callback)
                except BaseException as error:
                    self.ctx.logger.warn("failed to dispose plugin at %C", name)
                    self.ctx.logger.warn(error)
                    raise
                swapped.append((old_fiber, attempts[name]))
                self._swap_in(old_fiber, attempts[name])
                self.ctx.logger.info("reload plugin at %C", name)
        except BaseException as error:
            for name, old_module in backup.items():
                if old_module is not None:
                    sys.modules[name] = old_module
            for old_fiber, new_plugin in swapped:
                try:
                    self.ctx.registry.delete(new_plugin)
                    self._swap_in(old_fiber, old_fiber.runtime.callback)
                except BaseException as rollback_error:
                    self.ctx.logger.warn(rollback_error)
            self.ctx.logger.warn(error)

    def _swap_in(self, old_fiber, plugin):
        # JS: `oldFiber.parent.registry.plugin(plugin, oldFiber.config)` — the
        # replacement fiber keeps the original parent context, so entry-level
        # isolate/intercept keep applying after a reload.
        fiber = old_fiber.parent.registry.plugin(plugin, old_fiber.config)
        fiber.entry = old_fiber.entry
        if fiber.entry is not None:
            fiber.entry.fiber = fiber
        return fiber
