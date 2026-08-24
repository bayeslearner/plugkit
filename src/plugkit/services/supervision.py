"""Supervision trees for fibers — the other thing 1.0 had that Cordis lacks.

Cordis has a `FAILED` state (`fiber.py:_get_state`, set when the plugin body or
its config raises) and does nothing with it. The fiber sits failed until someone
calls `update()` or `restart()` by hand. For a long-running backend that is not
a policy, it is the absence of one: a transient failure at boot — a database not
up yet, a token not yet minted — permanently removes a plugin from the system.

This is Erlang's answer, ported from 1.0's `lifecycle_manager`: declare what
should happen when a child dies, and let the supervisor carry it out.

    async def main():
        root = Context()
        await root.plugin(SupervisorService)
        db = await root.plugin(Database)
        root.supervisor.supervise(db, max_restarts=5, within=60)

Strategies, matching 1.0 and OTP:

  one_for_one   restart just the fiber that failed
  one_for_all   restart every fiber mounted under the same parent context
  rest_for_one  restart the failed fiber and everything mounted after it

Exceeding `max_restarts` within `within` seconds stops the restarts and emits
`supervisor/escalate`, leaving the fiber failed. That event is the seam: a parent
supervisor, an alerting plugin, or a process-level bail-out listens for it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ..cordis import FiberState, Service

log = logging.getLogger(__name__)

Strategy = Literal["one_for_one", "one_for_all", "rest_for_one"]
Backoff = Literal["constant", "linear", "exponential"]

__all__ = ["SupervisorService", "Policy", "compute_delay"]


def compute_delay(base: float, attempt: int, backoff: Backoff) -> float:
    """Delay before restart attempt `attempt` (1-based)."""
    if backoff == "constant":
        return base
    if backoff == "linear":
        return base * attempt
    return base * (2 ** (attempt - 1))


@dataclass
class Policy:
    strategy: Strategy = "one_for_one"
    max_restarts: int = 3
    within: float = 60.0
    backoff: Backoff = "exponential"
    base_delay: float = 0.5
    history: list[float] = field(default_factory=list)

    def record(self, now: float) -> int:
        """Record a failure and return how many happened inside the window."""
        self.history.append(now)
        self.history[:] = [t for t in self.history if now - t <= self.within]
        return len(self.history)


class SupervisorService(Service):
    """Provides `ctx.supervisor`."""

    provide = "supervisor"

    def __init__(self, ctx, config=None):
        super().__init__(ctx)
        self._policies: dict[int, tuple[Any, Policy]] = {}
        self._restarting: set[int] = set()
        # Captured at construction: `_siblings` matches on parent context, and
        # this service is a sibling of everything mounted beside it. Restarting
        # itself under one_for_all would wipe `_policies` and stop supervision
        # for every fiber it was watching — during the incident it exists for.
        self._own_fiber = ctx.fiber
        ctx.on("internal/status", self._on_status)

    # ── public ────────────────────────────────────────────────────────

    def supervise(self, fiber, **kwargs) -> Any:
        """Put a fiber under supervision. Returns a disposer.

        The disposer is registered as an effect of the *calling* plugin, so
        supervision ends when whoever asked for it unloads.
        """
        policy = Policy(**kwargs)

        def execute():
            self._policies[id(fiber)] = (fiber, policy)
            return lambda: self._policies.pop(id(fiber), None)

        return self.ctx.effect(execute, f"ctx.supervisor.supervise({fiber.name})")

    def policy_for(self, fiber) -> Policy | None:
        entry = self._policies.get(id(fiber))
        return entry[1] if entry else None

    # ── internals ─────────────────────────────────────────────────────

    def _on_status(self, fiber, old_state) -> None:
        if fiber.state is FiberState.DISPOSED:
            self._policies.pop(id(fiber), None)
            self._restarting.discard(id(fiber))
            return
        if fiber.state is not FiberState.FAILED:
            return
        entry = self._policies.get(id(fiber))
        if entry is None or id(fiber) in self._restarting:
            return
        _, policy = entry
        attempt = policy.record(time.monotonic())
        if attempt > policy.max_restarts:
            log.error(
                "supervisor: %s exceeded %d restarts in %.0fs — escalating",
                fiber.name, policy.max_restarts, policy.within,
            )
            self.ctx.emit("supervisor/escalate", fiber, fiber._error)
            return
        self._restarting.add(id(fiber))
        asyncio.ensure_future(self._restart(fiber, policy, attempt))

    async def _restart(self, fiber, policy: Policy, attempt: int) -> None:
        delay = compute_delay(policy.base_delay, attempt, policy.backoff)
        log.warning(
            "supervisor: restarting %s in %.2fs (attempt %d/%d, %s)",
            fiber.name, delay, attempt, policy.max_restarts, policy.strategy,
        )
        try:
            if delay:
                await asyncio.sleep(delay)
            targets = self._targets(fiber, policy.strategy)
            for target in targets:
                await self._revive(target)
        except Exception:
            log.exception("supervisor: restart of %s failed", fiber.name)
        finally:
            self._restarting.discard(id(fiber))

    def _targets(self, fiber, strategy: Strategy) -> list:
        if strategy == "one_for_one":
            return [fiber]
        siblings = sorted(self._siblings(fiber), key=lambda f: f.uid or 0)
        if strategy == "one_for_all":
            return siblings
        # rest_for_one — the failed fiber and everything mounted after it
        uid = fiber.uid or 0
        return [f for f in siblings if (f.uid or 0) >= uid]

    def _siblings(self, fiber) -> list:
        """Every live fiber mounted under the same parent context."""
        out = []
        for runtime in self.ctx.registry.values():
            for other in runtime.fibers:
                if other.uid is None or other.parent is not fiber.parent:
                    continue
                if other is self._own_fiber:
                    continue
                out.append(other)
        return out

    async def _revive(self, fiber) -> None:
        if fiber.uid is None:
            return
        fiber._error = None
        result = fiber.restart()
        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
            await result
