"""Fake clock for deterministic timer tests."""

import asyncio


class FakeClock:
    """A virtual clock replacing the event loop's `call_later`."""

    def __init__(self):
        self._now = 0
        self._timers = []  # list of handles
        self._seq = 0

    def now(self) -> int:
        return self._now

    def call_later(self, delay, callback, *args):
        self._seq += 1
        handle = {"id": self._seq, "at": self._now + delay, "callback": callback, "args": args, "cancelled": False}
        self._timers.append(handle)
        return handle

    def cancel(self, handle):
        handle["cancelled"] = True

    async def advance(self, ms: int):
        # JS async functions run synchronously to their first await, so an
        # iterator's first `next()` future is already registered when the
        # clock fires; flush one tick so Python tasks reach the same point.
        await asyncio.sleep(0)
        self._now += ms
        while True:
            ready = [t for t in self._timers if not t["cancelled"] and t["at"] <= self._now]
            if not ready:
                break
            ready.sort(key=lambda t: (t["at"], t["id"]))
            for t in ready:
                self._timers.remove(t)
                t["callback"](*t["args"])
                await asyncio.sleep(0)
        await asyncio.sleep(0)
