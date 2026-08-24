"""`ctx.tools` — a tool registry with the five-stage execution pipeline.

This is the piece the waterfall fix in the kernel was for. dsh calls it the
widest and most useful extension surface it has, and the shape carries over
exactly, because it is the difference between "I can add a tool" and "I can add
a rule about everyone's tools without touching any of them".

Every call passes through five stages:

    1. tools/pre-execute    waterfall    Allow | Deny | Ask
    2. ctx.tools.guard()    monotonic    a reason string, or None
    3. tools/execute        waterfall    wraps the call — timeouts, retries, metrics
    4. tools/post-execute   waterfall    Accept | Block — change or reject the result
    5. tools/result         emit         observe only; everything is frozen

Which stage you want:

    ask a human first          stage 1   the only stage with an Ask
    a rule nobody can override stage 2   guards cannot allow, only deny
    timeout / retry / measure  stage 3   the only stage holding the whole call
    change or reject a result  stage 4   stage 5 is immutable
    log, audit, count          stage 5   doing it in 4 races other listeners

Stage 1 has no "rewrite the arguments" option, deliberately: by then the
arguments are already in the log and on screen, and letting a listener change
them would make history, audit, display and execution disagree.

Stage 2 is monotonic — a guard returns a denial reason or nothing, and there is
no allow. That is why registration order cannot turn a denial back into
permission, and it is what makes a guard a rule rather than a suggestion.

`Ask` fails closed. No approver registered, an approver that raises, or an
approver returning anything but True all become a denial.

Tools are duck-typed. Anything with `name`, `description`, `parameters` and
`execute` works, so a tool provider can stay a plain object:

    class SearchTool:                      # imports nothing from plugkit
        name = "search"
        description = "Search the index."
        parameters = {"type": "object", "properties": {"q": {"type": "string"}}}
        def execute(self, arguments, execution):
            return self.index.query(arguments["q"])
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable

from ..cordis import Service

log = logging.getLogger(__name__)

__all__ = [
    "ToolsService",
    "Tool",
    "ToolExecution",
    "ToolResult",
    "Allow",
    "Deny",
    "Ask",
    "Accept",
    "Block",
    "timeout_policy",
]


# ── data ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tool:
    """A convenience shape. Any object with these attributes also works."""

    name: str
    description: str
    execute: Callable[..., Any]
    parameters: dict = field(default_factory=dict)
    destructive: bool = False
    timeout_s: float | None = None


@dataclass(frozen=True)
class ToolExecution:
    """One call, frozen. Handed to every stage."""

    name: str
    arguments: dict
    id: str
    caller: Any = None
    tool: Any = None


@dataclass
class ToolResult:
    ok: bool
    value: Any = None
    error: dict | None = None

    @staticmethod
    def failure(code: str, message: str) -> "ToolResult":
        return ToolResult(ok=False, error={"code": code, "message": message})


# ── stage decisions ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class Ask:
    reason: str | None = None


@dataclass(frozen=True)
class Accept:
    """Stage 4: keep the result, optionally replacing its value."""

    value: Any = None
    replaced: bool = False

    @staticmethod
    def replacing(value: Any) -> "Accept":
        return Accept(value=value, replaced=True)


@dataclass(frozen=True)
class Block:
    """Stage 4: reject the result and give the caller feedback instead."""

    feedback: str


async def _maybe_await(value: Any) -> Any:
    """Await if awaitable. Sync and async listeners both work in a waterfall."""
    if inspect.isawaitable(value):
        return await value
    return value


@lru_cache(maxsize=None)
def _wants_execution_uncached(body: Any) -> bool:
    positional = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    try:
        parameters = list(inspect.signature(body).parameters.values())
    except (TypeError, ValueError):
        return True  # unintrospectable: assume the documented two-argument form
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters):
        return True
    return len([p for p in parameters if p.kind in positional]) >= 2


def _wants_execution(body: Any) -> bool:
    """Does this tool body take `(arguments, execution)` or just `(arguments)`?

    Resolved by reading the signature — never by calling the body and retrying
    on TypeError. A body that raises its own TypeError mentioning positional
    arguments would be invoked a second time, after its side effects had already
    happened. See `test_a_tool_body_never_runs_twice`.

    Introspects the callable as it will be *called* — a bound method, whose
    signature has already dropped `self`. Keying the cache on `__func__` instead
    would count `self` as a parameter and get every one-argument tool wrong.
    """
    try:
        return _wants_execution_uncached(body)
    except TypeError:  # unhashable callable — skip the cache, not the check
        return _wants_execution_uncached.__wrapped__(body)


class ToolsService(Service):
    """Provides `ctx.tools`."""

    provide = "tools"
    inject = ["points"]

    #: Point names. Prefixed so a composition can tell at a glance, from
    #: `ctx.points.names()`, which points belong to which service.
    TOOLS = "tools"
    GUARDS = "tools.guards"
    APPROVERS = "tools.approvers"

    # No __init__: there is no private state left to build. Tools, guards and
    # approvers all live in extension points.

    # ── registration ──────────────────────────────────────────────────

    def register(self, tool: Any):
        """Register a tool. Returns a disposer owned by the calling plugin.

        Raises if `name` is already taken — two tools under one name is a bug
        that would otherwise surface as the wrong one being called.
        """
        for attribute in ("name", "description", "execute"):
            if not hasattr(tool, attribute):
                raise TypeError(f"a tool needs a {attribute!r} attribute, {tool!r} has none")

        name = tool.name
        # Callers look tools up by string. A non-string name registers without
        # complaint and can never be reached, and breaks `names()`, which sorts
        # the keys.
        if not isinstance(name, str) or not name:
            raise TypeError(
                f"a tool name must be a non-empty string, {tool!r} has name={name!r}"
            )
        if not callable(tool.execute):
            raise TypeError(f"a tool's execute must be callable, {tool!r} has {tool.execute!r}")

        return self.ctx.points.add(self.TOOLS, tool, key=name, unique=True)

    def guard(self, guard: Callable[[ToolExecution], str | None]):
        """Register a stage-2 guard. Returns a disposer.

        Return a reason string to deny, or None to leave the call alone. There
        is no allow: guards only ever reduce permission, so no later
        registration can undo yours.
        """
        return self.ctx.points.add(self.GUARDS, guard)

    @property
    def _approver(self) -> Callable[[ToolExecution, str | None], Any] | None:
        """The most recently registered approver, or None."""
        return self.ctx.points.last(self.APPROVERS)

    def set_approver(self, approver: Callable[[ToolExecution, str | None], Any]):
        """Register who answers an `Ask`. Returns a disposer.

        Registered rather than injected so that `Ask` fails closed by default:
        a composition with no approver denies instead of hanging or allowing.

        The most recent registration wins. Disposing any registration removes
        only that one, in any order — `points.last()` reads the newest by
        registration sequence rather than restoring a saved value, so unloading
        an older approver cannot resurrect it over a newer one.
        """
        return self.ctx.points.add(self.APPROVERS, approver)

    # ── inspection ────────────────────────────────────────────────────

    def get(self, name: str) -> Any | None:
        return self.ctx.points.get(self.TOOLS, name)

    def list(self) -> list:
        return self.ctx.points.all(self.TOOLS)

    def names(self) -> list[str]:
        return sorted(tool.name for tool in self.list())

    # ── the pipeline ──────────────────────────────────────────────────

    async def execute(
        self,
        name: str,
        arguments: dict | None = None,
        *,
        caller: Any = None,
        id: str | None = None,
    ) -> ToolResult:
        """Run a tool through all five stages. Never raises."""
        tool = self.get(name)
        if tool is None:
            return ToolResult.failure("UNKNOWN_TOOL", f"no tool named {name!r}")

        execution = ToolExecution(
            name=name,
            arguments=dict(arguments or {}),
            id=id or uuid.uuid4().hex,
            caller=caller,
            tool=tool,
        )

        decision = await self._stage1(execution)
        if isinstance(decision, Deny):
            return self._denied(execution, decision.reason)

        reason = self._stage2(execution)
        if reason is not None:
            return self._denied(execution, reason)

        result = await self._stage3(execution)
        result = await self._stage4(execution, result)
        self._stage5(execution, result)
        return result

    async def _stage1(self, execution: ToolExecution):
        async def default():
            return Allow()

        decision = await _maybe_await(
            self.ctx.waterfall("tools/pre-execute", execution, default)
        )
        if isinstance(decision, Ask):
            return await self._resolve_ask(execution, decision)
        if isinstance(decision, (Allow, Deny)):
            return decision
        log.warning("tools/pre-execute returned %r, treating as deny", decision)
        return Deny("pre-execute listener returned an unrecognised decision")

    async def _resolve_ask(self, execution: ToolExecution, ask: Ask):
        if self._approver is None:
            return Deny(ask.reason or "approval required, but no approver is registered")
        try:
            answer = await _maybe_await(self._approver(execution, ask.reason))
        except Exception as exc:
            log.exception("approver raised for %s", execution.name)
            return Deny(f"approval failed: {exc}")
        if answer is True:
            return Allow()
        return Deny(ask.reason or "denied by approver")

    def _stage2(self, execution: ToolExecution) -> str | None:
        for guard in self.ctx.points.all(self.GUARDS):
            try:
                reason = guard(execution)
            except Exception as exc:
                log.exception("guard raised for %s", execution.name)
                return f"guard failed: {exc}"
            if reason:
                return reason
        return None

    async def _stage3(self, execution: ToolExecution) -> ToolResult:
        async def default():
            body = execution.tool.execute
            arguments = (
                (execution.arguments, execution)
                if _wants_execution(body)
                else (execution.arguments,)
            )
            value = await _maybe_await(body(*arguments))
            return ToolResult(ok=True, value=value)

        try:
            return await _maybe_await(
                self.ctx.waterfall("tools/execute", execution, default)
            )
        except asyncio.CancelledError:
            return ToolResult.failure("ABORTED", f"{execution.name} was cancelled")
        except Exception as exc:
            log.exception("tool %s raised", execution.name)
            return ToolResult.failure("TOOL_ERROR", str(exc))

    async def _stage4(self, execution: ToolExecution, result: ToolResult) -> ToolResult:
        async def default():
            return Accept()

        try:
            decision = await _maybe_await(
                self.ctx.waterfall("tools/post-execute", execution, result, default)
            )
        except Exception:
            log.exception("tools/post-execute raised for %s", execution.name)
            return result

        if isinstance(decision, Block):
            return ToolResult.failure("BLOCKED", decision.feedback)
        if isinstance(decision, Accept) and decision.replaced:
            return ToolResult(ok=result.ok, value=decision.value, error=result.error)
        return result

    def _stage5(self, execution: ToolExecution, result: ToolResult) -> None:
        try:
            self.ctx.emit("tools/result", execution, result)
        except Exception:
            # stage 5 is observation. A listener failing here must not be able
            # to change whether the call succeeded.
            log.exception("tools/result listener failed for %s", execution.name)

    def _denied(self, execution: ToolExecution, reason: str) -> ToolResult:
        result = ToolResult.failure("DENIED", reason)
        self._stage5(execution, result)
        return result


def timeout_policy(ctx, config=None):
    """Reference stage-3 plugin: enforce each tool's own `timeout_s`.

    A timeout notifies, it does not kill — a tool that ignores cancellation will
    not stop. Registration order decides semantics when combined with a retry
    wrapper: registered outer, the whole retry operation shares one clock;
    registered inner, each attempt gets its own.
    """

    async def wrap(execution, next_):
        budget = getattr(execution.tool, "timeout_s", None)
        if not budget:
            return await _maybe_await(next_())
        try:
            return await asyncio.wait_for(
                asyncio.ensure_future(_maybe_await(next_())), timeout=budget
            )
        except asyncio.TimeoutError:
            return ToolResult.failure(
                "TOOL_TIMEOUT", f"{execution.name} exceeded {budget}s"
            )

    ctx.on("tools/execute", wrap)


timeout_policy.name = "tool-timeout-policy"
timeout_policy.inject = ["tools"]
