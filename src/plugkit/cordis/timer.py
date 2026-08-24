"""Timer service — port of `@cordisjs/plugin-timer`."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

from .service import Service

_second = 1000
_minute = 60 * _second
_hour = 60 * _minute
_day = 24 * _hour


def format_ms(ms: int) -> str:
    """Port of cosmokit `Time.format(ms)`."""
    abs_ms = abs(ms)
    if abs_ms >= _day - _hour // 2:
        return f"{round(ms / _day)}d"
    if abs_ms >= _hour - _minute // 2:
        return f"{round(ms / _hour)}h"
    if abs_ms >= _minute - _second // 2:
        return f"{round(ms / _minute)}m"
    if abs_ms >= _second:
        return f"{round(ms / _second)}s"
    return f"{ms}ms"


class RealClock:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    def now(self) -> int:
        return int(self.loop.time() * 1000)

    def call_later(self, delay: int, callback, *args):
        return self.loop.call_later(delay / 1000, callback, *args)

    def cancel(self, handle) -> None:
        handle.cancel()


_clock: Optional[Any] = None


def get_clock():
    global _clock
    if _clock is None:
        loop = asyncio.get_event_loop_policy().get_event_loop()
        _clock = RealClock(loop)
    return _clock


def set_clock(clock) -> None:
    global _clock
    _clock = clock


class TimerService(Service):
    def __init__(self, ctx, config=None):
        super().__init__(ctx, "timer")
        ctx.mixin("timer", ["timeout", "interval", "throttle", "debounce", "setTimeout", "setInterval"])

    # deprecated aliases
    def setTimeout(self, callback, delay):
        return self.timeout(callback, delay)

    def setInterval(self, callback, delay):
        return self.interval(callback, delay)

    def timeout(self, *args):
        callback = args[0] if len(args) > 1 or callable(args[0]) else None
        if callable(args[0]):
            delay = args[1]
        else:
            callback = None
            delay = args[0]

        if callback:
            service = self

            def effect():
                def fire():
                    dispose()
                    callback()

                timer = get_clock().call_later(delay, fire)

                def dispose():
                    get_clock().cancel(timer)

                return dispose

            return self.ctx.effect(effect, "ctx.timeout()")

        loop = asyncio.get_event_loop_policy().get_event_loop()
        future = loop.create_future()

        def effect():
            timer = get_clock().call_later(delay, lambda: future.set_result(None))

            def dispose():
                get_clock().cancel(timer)
                if not future.done():
                    future.set_exception(Exception("Context has been disposed"))

            return dispose

        dispose = self.ctx.effect(effect, "ctx.timeout()")
        future.add_done_callback(lambda f: dispose())
        return future

    def interval(self, *args):
        callback = args[0] if len(args) > 1 or callable(args[0]) else None
        if callable(args[0]):
            delay = args[1]
        else:
            callback = None
            delay = args[0]

        if callback:
            def effect():
                timer = [None]

                def tick():
                    callback()
                    timer[0] = get_clock().call_later(delay, tick)

                timer[0] = get_clock().call_later(delay, tick)

                def dispose():
                    if timer[0] is not None:
                        get_clock().cancel(timer[0])

                return dispose

            return self.ctx.effect(effect, "ctx.interval()")

        return _IntervalIterator(self.ctx, self, delay)

    def _schedule(self, label: str, trigger, is_disposed: bool = False):
        timer = None
        state = {"disposed": is_disposed}

        def effect():
            def dispose():
                state["disposed"] = True
                if timer is not None:
                    get_clock().cancel(timer)

            return dispose

        dispose = self.ctx.effect(effect, label)

        def wrapper(*args):
            nonlocal timer
            if timer is not None:
                get_clock().cancel(timer)
            timer = trigger(args, state)

        wrapper.dispose = dispose
        return wrapper

    def throttle(self, callback, delay, no_trailing: bool = False):
        state = {"last_call": -float("inf")}

        def execute(*args):
            state["last_call"] = get_clock().now()
            callback(*args)

        def trigger(args, is_disposed):
            now = get_clock().now()
            remaining = delay - now + state["last_call"]
            if remaining <= 0:
                execute(*args)
            elif not is_disposed["disposed"]:
                return get_clock().call_later(remaining, execute, *args)

        return self._schedule("ctx.throttle()", trigger, no_trailing)

    def debounce(self, callback, delay):
        def trigger(args, is_disposed):
            if is_disposed["disposed"]:
                return None
            return get_clock().call_later(delay, callback, *args)

        return self._schedule("ctx.debounce()", trigger)


class _IntervalIterator:
    """Port of the JS async-iterator returned by `ctx.interval(delay)`."""

    def __init__(self, ctx, service: TimerService, delay: int):
        self._service = service
        self._delay = delay
        self._done = None  # ('return', value) | ('throw', reason)
        self._next_task: Optional[asyncio.Future] = None
        self._timer = None
        self._dispose = ctx.effect(self._effect, "ctx.interval()")

    def _effect(self):
        iterator = self

        def tick():
            if iterator._next_task is not None and not iterator._next_task.done():
                iterator._next_task.set_result(None)
            iterator._timer = get_clock().call_later(iterator._delay, tick)

        self._timer = get_clock().call_later(self._delay, tick)

        def dispose():
            if self._timer is not None:
                get_clock().cancel(self._timer)
            if self._done is None:
                reason = Exception("Context has been disposed")
                self._done = ("throw", reason)
                if self._next_task is not None and not self._next_task.done():
                    self._next_task.set_exception(reason)

        return dispose

    def __aiter__(self):
        return self

    async def __anext__(self):
        loop = asyncio.get_event_loop_policy().get_event_loop()
        future = loop.create_future()
        self._next_task = future
        await future
        if self._done is not None:
            raise StopAsyncIteration
        return None

    async def aclose(self):
        if self._done is None:
            self._done = ("return", None)
        if self._next_task is not None and not self._next_task.done():
            self._next_task.set_result(None)
        self._dispose()

    async def athrow(self, reason: BaseException):
        if self._done is None:
            self._done = ("throw", reason)
        if self._next_task is not None and not self._next_task.done():
            self._next_task.set_exception(reason)
        self._dispose()
