"""The five-stage tool pipeline.

One test per stage, plus the properties that make each stage worth having:
stage 2 cannot be overridden, stage 4 can rewrite a result, stage 5 cannot
change whether the call succeeded.
"""

import asyncio

import pytest

from plugkit import (
    Accept,
    Allow,
    Ask,
    Block,
    ConfigService,
    Context,
    Deny,
    PointsService,
    Tool,
    ToolsService,
    timeout_policy,
)


async def settle(n=15):
    for _ in range(n):
        await asyncio.sleep(0)


class Adder:
    """A tool provider that imports nothing from the kernel."""

    name = "add"
    description = "Add two numbers."
    parameters = {"type": "object", "properties": {"a": {}, "b": {}}}
    destructive = False
    timeout_s = None

    def execute(self, arguments, execution=None):
        return arguments["a"] + arguments["b"]


async def boot(*plugins):
    root = Context()
    # ToolsService holds its tools, guards and approvers in extension points,
    # so `points` is a declared dependency rather than private state.
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    for plugin in plugins:
        await root.plugin(plugin)
    await settle()
    return root


def register_plugin(tool):
    def plugin(ctx, config=None):
        ctx.tools.register(tool)

    plugin.inject = ["tools"]
    plugin.name = f"tool-{getattr(tool, 'name', 'anon')}"
    return plugin


# ── registration ─────────────────────────────────────────────────────────


async def test_a_plain_object_is_a_valid_tool():
    root = await boot(register_plugin(Adder()))
    assert root.tools.names() == ["add"]
    result = await root.tools.execute("add", {"a": 2, "b": 3})
    assert result.ok and result.value == 5


async def test_registration_dies_with_the_plugin():
    root = await boot()
    fiber = await root.plugin(register_plugin(Adder()))
    await settle()
    assert root.tools.names() == ["add"]

    await fiber.dispose()
    await settle()
    assert root.tools.names() == [], "the tool outlived the plugin that registered it"


async def test_duplicate_name_is_rejected():
    root = await boot(register_plugin(Adder()))
    with pytest.raises(Exception):
        await root.plugin(register_plugin(Adder()))


async def test_unknown_tool_is_a_result_not_an_exception():
    root = await boot()
    result = await root.tools.execute("nope")
    assert not result.ok
    assert result.error["code"] == "UNKNOWN_TOOL"


async def test_a_raising_tool_becomes_a_structured_error():
    class Boom:
        name, description = "boom", "raises"
        parameters = {}

        def execute(self, arguments, execution=None):
            raise ValueError("kaboom")

    root = await boot(register_plugin(Boom()))
    result = await root.tools.execute("boom")
    assert not result.ok
    assert result.error["code"] == "TOOL_ERROR"
    assert "kaboom" in result.error["message"]


# ── stage 1: pre-execute ─────────────────────────────────────────────────


async def test_stage1_can_deny():
    def gate(ctx, config=None):
        async def listener(execution, next_):
            if execution.arguments.get("a", 0) < 0:
                return Deny("no negative numbers")
            return await next_()

        ctx.on("tools/pre-execute", listener)

    gate.inject = ["tools"]
    root = await boot(register_plugin(Adder()), gate)

    assert (await root.tools.execute("add", {"a": 1, "b": 1})).value == 2
    denied = await root.tools.execute("add", {"a": -1, "b": 1})
    assert not denied.ok and denied.error["message"] == "no negative numbers"


async def test_stage1_ask_fails_closed_without_an_approver():
    def gate(ctx, config=None):
        ctx.on("tools/pre-execute", lambda execution, next_: Ask("really?"))

    gate.inject = ["tools"]
    root = await boot(register_plugin(Adder()), gate)

    result = await root.tools.execute("add", {"a": 1, "b": 1})
    assert not result.ok, "Ask allowed the call with no approver registered"
    assert "really?" in result.error["message"]


async def test_stage1_ask_consults_the_approver():
    answers = []

    def gate(ctx, config=None):
        ctx.on("tools/pre-execute", lambda execution, next_: Ask("confirm"))
        ctx.tools.set_approver(lambda execution, reason: answers.append(reason) or True)

    gate.inject = ["tools"]
    root = await boot(register_plugin(Adder()), gate)

    result = await root.tools.execute("add", {"a": 1, "b": 1})
    assert result.ok and result.value == 2
    assert answers == ["confirm"]


async def test_stage1_approver_that_raises_denies():
    def gate(ctx, config=None):
        ctx.on("tools/pre-execute", lambda execution, next_: Ask("confirm"))

        def approver(execution, reason):
            raise RuntimeError("approval service is down")

        ctx.tools.set_approver(approver)

    gate.inject = ["tools"]
    root = await boot(register_plugin(Adder()), gate)
    result = await root.tools.execute("add", {"a": 1, "b": 1})
    assert not result.ok and "approval failed" in result.error["message"]


# ── stage 2: guards ──────────────────────────────────────────────────────


async def test_stage2_guard_denies():
    def policy(ctx, config=None):
        ctx.tools.guard(lambda e: "no big numbers" if e.arguments.get("a", 0) > 100 else None)

    policy.inject = ["tools"]
    root = await boot(register_plugin(Adder()), policy)

    assert (await root.tools.execute("add", {"a": 1, "b": 1})).ok
    blocked = await root.tools.execute("add", {"a": 999, "b": 1})
    assert not blocked.ok and blocked.error["message"] == "no big numbers"


async def test_stage2_cannot_be_overridden_by_stage1():
    """A guard is a rule, not a suggestion — stage 1 allowing does not help."""

    def permissive(ctx, config=None):
        ctx.on("tools/pre-execute", lambda execution, next_: Allow())

    def policy(ctx, config=None):
        ctx.tools.guard(lambda e: "forbidden")

    permissive.inject = policy.inject = ["tools"]
    root = await boot(register_plugin(Adder()), permissive, policy)

    result = await root.tools.execute("add", {"a": 1, "b": 1})
    assert not result.ok and result.error["message"] == "forbidden"


async def test_stage2_order_does_not_matter():
    def deny_all(ctx, config=None):
        ctx.tools.guard(lambda e: "denied")

    def allow_nothing(ctx, config=None):
        ctx.tools.guard(lambda e: None)  # guards have no allow

    deny_all.inject = allow_nothing.inject = ["tools"]

    for order in ((deny_all, allow_nothing), (allow_nothing, deny_all)):
        root = await boot(register_plugin(Adder()), *order)
        result = await root.tools.execute("add", {"a": 1, "b": 1})
        assert not result.ok, "registration order changed the answer"


# ── stage 3: wrapping ────────────────────────────────────────────────────


async def test_stage3_can_measure_the_whole_call():
    seen = []

    def metrics(ctx, config=None):
        async def wrap(execution, next_):
            seen.append(("start", execution.name))
            result = await next_()
            seen.append(("end", execution.name))
            return result

        ctx.on("tools/execute", wrap)

    metrics.inject = ["tools"]
    root = await boot(register_plugin(Adder()), metrics)
    await root.tools.execute("add", {"a": 1, "b": 1})
    assert seen == [("start", "add"), ("end", "add")]


async def test_stage3_timeout_policy():
    class Slow:
        name, description, parameters = "slow", "sleeps", {}
        timeout_s = 0.01

        async def execute(self, arguments, execution=None):
            await asyncio.sleep(1)
            return "done"

    root = await boot(register_plugin(Slow()), timeout_policy)
    result = await root.tools.execute("slow")
    assert not result.ok and result.error["code"] == "TOOL_TIMEOUT"


async def test_stage3_timeout_leaves_untimed_tools_alone():
    root = await boot(register_plugin(Adder()), timeout_policy)
    result = await root.tools.execute("add", {"a": 1, "b": 1})
    assert result.ok and result.value == 2


# ── stage 4: post-execute ────────────────────────────────────────────────


async def test_stage4_can_replace_the_value():
    def redact(ctx, config=None):
        async def listener(execution, result, next_):
            return Accept.replacing("[redacted]")

        ctx.on("tools/post-execute", listener)

    redact.inject = ["tools"]
    root = await boot(register_plugin(Adder()), redact)
    result = await root.tools.execute("add", {"a": 1, "b": 1})
    assert result.ok and result.value == "[redacted]"


async def test_stage4_can_block_with_feedback():
    def veto(ctx, config=None):
        async def listener(execution, result, next_):
            return Block("that answer is not acceptable")

        ctx.on("tools/post-execute", listener)

    veto.inject = ["tools"]
    root = await boot(register_plugin(Adder()), veto)
    result = await root.tools.execute("add", {"a": 1, "b": 1})
    assert not result.ok
    assert result.error["code"] == "BLOCKED"
    assert result.error["message"] == "that answer is not acceptable"


async def test_stage4_default_leaves_the_result_alone():
    def observer(ctx, config=None):
        async def listener(execution, result, next_):
            return await next_()

        ctx.on("tools/post-execute", listener)

    observer.inject = ["tools"]
    root = await boot(register_plugin(Adder()), observer)
    assert (await root.tools.execute("add", {"a": 2, "b": 3})).value == 5


# ── stage 5: observation ─────────────────────────────────────────────────


async def test_stage5_sees_every_outcome_including_denials():
    seen = []

    def audit(ctx, config=None):
        ctx.on("tools/result", lambda execution, result: seen.append((execution.name, result.ok)))

    def policy(ctx, config=None):
        ctx.tools.guard(lambda e: "nope" if e.arguments.get("a") == 0 else None)

    audit.inject = policy.inject = ["tools"]
    root = await boot(register_plugin(Adder()), audit, policy)

    await root.tools.execute("add", {"a": 1, "b": 1})
    await root.tools.execute("add", {"a": 0, "b": 1})
    assert seen == [("add", True), ("add", False)]


async def test_stage5_failure_cannot_change_the_call():
    def broken_audit(ctx, config=None):
        def listener(execution, result):
            raise RuntimeError("audit backend is down")

        ctx.on("tools/result", listener)

    broken_audit.inject = ["tools"]
    root = await boot(register_plugin(Adder()), broken_audit)

    result = await root.tools.execute("add", {"a": 2, "b": 3})
    assert result.ok and result.value == 5, "an observer broke the call"


# ── composition ──────────────────────────────────────────────────────────


async def test_a_policy_plugin_needs_no_change_to_any_tool():
    """The point of the whole design: one plugin, a rule over everyone's tools."""

    class Deleter:
        name, description, parameters = "delete", "deletes things", {}

        def execute(self, arguments, execution=None):
            return f"deleted {arguments['what']}"

    def safety(ctx, config=None):
        ctx.tools.guard(
            lambda e: "refusing to delete the root filesystem"
            if e.arguments.get("what") == "/"
            else None
        )

    safety.inject = ["tools"]
    root = await boot(register_plugin(Adder()), register_plugin(Deleter()), safety)

    assert (await root.tools.execute("delete", {"what": "tmp"})).value == "deleted tmp"
    stopped = await root.tools.execute("delete", {"what": "/"})
    assert not stopped.ok
    assert (await root.tools.execute("add", {"a": 1, "b": 1})).ok


# ── the values a listener receives ───────────────────────────────────────


async def test_listeners_receive_a_frozen_ToolExecution():
    """`ToolExecution` is exported because listeners are handed one."""
    from plugkit import ToolExecution

    seen = []

    def observer(ctx, config=None):
        ctx.on("tools/result", lambda execution, result: seen.append(execution))

    observer.inject = ["tools"]
    root = await boot(register_plugin(Adder()), observer)
    await root.tools.execute("add", {"a": 1, "b": 2}, caller="me")

    execution = seen[0]
    assert isinstance(execution, ToolExecution)
    assert execution.name == "add"
    assert execution.arguments == {"a": 1, "b": 2}
    assert execution.caller == "me"
    assert execution.id

    with pytest.raises(Exception):
        execution.name = "changed"      # frozen dataclass


async def test_execute_returns_a_ToolResult():
    """`ToolResult` is exported because `execute()` returns one."""
    from plugkit import ToolResult

    root = await boot(register_plugin(Adder()))

    ok = await root.tools.execute("add", {"a": 2, "b": 3})
    assert isinstance(ok, ToolResult)
    assert (ok.ok, ok.value, ok.error) == (True, 5, None)

    bad = await root.tools.execute("missing")
    assert isinstance(bad, ToolResult)
    assert bad.ok is False
    assert bad.error["code"] == "UNKNOWN_TOOL"
