"""One watcher's callback and its invocation chain.

Shared by `ctx.config.watch` and `ctx.points.watch`, which are the same idea over
two different sources of change: a callback, a disposer owned by the plugin that
registered it, and no reactive vocabulary on the surface.

The shape is DeepSeek Harness's settings service
(`packages/settings/settings/src/index.ts`), including the two rules that are
easy to leave out:

- **Invocations of one watcher are serialized.** An async callback still running
  when the next change lands must not be entered twice. Upstream chains a promise
  tail per watcher; this chains a task.
- **A disposed watcher is not called**, including for a change that lands while
  its plugin is being torn down. The flag is checked again after the await,
  because "active when queued" and "active when it runs" are different questions.

A callback that raises is logged and dropped. A config write is not the place to
discover that some unrelated plugin's watcher has a bug, and a raising watcher
must not stop the next invocation of itself or the watchers after it.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable

__all__ = ["Watcher"]


class Watcher:
    """A callback plus the serialization and liveness rules around it."""

    __slots__ = ("callback", "label", "logger", "active", "_tail")

    def __init__(self, callback: Callable[..., Any], label: str, logger: Any) -> None:
        self.callback = callback
        self.label = label
        self.logger = logger
        self.active = True
        self._tail: asyncio.Task | None = None

    def dispose(self) -> None:
        """Stop calling the callback. Anything already queued checks this flag."""
        self.active = False

    def fire(self, *args: Any) -> None:
        if not self.active:
            return
        pending = self._tail
        if pending is not None and not pending.done():
            self._tail = asyncio.ensure_future(self._after(pending, args))
            return
        result = self._call(args)
        if inspect.isawaitable(result):
            self._tail = asyncio.ensure_future(self._settle(result))

    async def _after(self, pending: asyncio.Task, args: tuple) -> None:
        try:
            await pending
        except Exception:  # already logged by the invocation that raised
            pass
        if not self.active:
            return
        result = self._call(args)
        if inspect.isawaitable(result):
            await self._settle(result)

    async def _settle(self, result: Any) -> None:
        try:
            await result
        except Exception as reason:
            self._failed(reason)

    def _call(self, args: tuple) -> Any:
        try:
            return self.callback(*args)
        except Exception as reason:
            self._failed(reason)
            return None

    def _failed(self, reason: BaseException) -> None:
        warn = getattr(self.logger, "warn", None) or getattr(self.logger, "warning", None)
        if warn is not None:
            warn("watcher for %s failed: %r", self.label, reason)
