"""Example 11 — Mini App / Extension Bundle

Install a "Slack integration" that brings 3 components at once.
A single hot_add_bundle() call adds SlackNotifier, SlackWebhookReceiver,
and SlackCommandHandler. They communicate via bus events and calls.
Remove the bundle — all three go away.

Domain: plugin / extension system.
Shows: multi-component bundles, hot_add, hot_remove, cross-component
       bus calls, @subscribe for event fan-out.

No kernel changes required.

Run: PYTHONPATH=src python -m signalpy.examples.extension_bundle
"""
import asyncio
from typing import Protocol, runtime_checkable
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    effect, subscribe,
)
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider


# ── Contracts ──────────────────────────────────────────────────

@runtime_checkable
class ISlackNotifier(Protocol):
    async def notify(self, params) -> dict: ...


# ── Core app that the extension integrates with ─────────────────

class AlertParams(BaseModel):
    channel: str = "general"
    message: str = ""


@component("core-app", version="1.0")
@requires(config="IConfig")
class CoreApp:
    """The main application. Extensions integrate by subscribing to its events."""

    @lifecycle.activate
    def activate(self):
        self.action_log = []

    @runnable("do_work", params=BaseModel, description="Do some work")
    async def do_work(self, params):
        self.action_log.append("work-done")
        # Publish an event that extensions can subscribe to
        await self.rt.publish("core.work_completed", {"result": "ok"})
        return {"status": "completed"}


# ── Slack extension bundle (3 components) ───────────────────────

@component("slack-notifier", version="1.0")
@provides(ISlackNotifier)
@requires(config="IConfig")
class SlackNotifier:
    """Sends notifications to Slack channels."""

    @lifecycle.activate
    def activate(self):
        self.sent = []

    @runnable("notify", params=AlertParams, description="Send a Slack notification")
    async def notify(self, params):
        msg = f"#{params.channel}: {params.message}"
        self.sent.append(msg)
        print(f"    [slack-notifier] {msg}")
        return {"sent": True, "channel": params.channel}


@component("slack-webhook", version="1.0")
@requires(config="IConfig")
class SlackWebhookReceiver:
    """Receives Slack webhooks and publishes events on the bus."""

    @lifecycle.activate
    def activate(self):
        self.received = []

    @runnable("receive", params=BaseModel, description="Receive a Slack webhook")
    async def receive(self, params):
        payload = dict(params)
        self.received.append(payload)
        await self.rt.publish("slack.webhook_received", payload)
        return {"ack": True}


@component("slack-commands", version="1.0")
@requires(config="IConfig", notifier=ISlackNotifier)
class SlackCommandHandler:
    """Handles /slash commands from Slack. Also listens for core events."""

    @lifecycle.activate
    def activate(self):
        self.handled = []

    @subscribe("core.work_completed", description="Auto-notify Slack when core work completes")
    async def on_work_completed(self, event_type, data):
        self.handled.append(("work_completed", data))
        # Direct call via @requires injection (no bus round-trip)
        await self.rt.notifier.notify(AlertParams(
            channel="ops",
            message=f"Work completed: {data.get('result', '?')}",
        ))

    @runnable("handle", params=BaseModel, description="Handle a /slash command")
    async def handle(self, params):
        cmd = params.get("command", "/unknown") if isinstance(params, dict) else "/unknown"
        self.handled.append(("command", cmd))
        return {"handled": cmd}


# Convenience: the bundle as a list
SLACK_BUNDLE = [SlackNotifier, SlackWebhookReceiver, SlackCommandHandler]


# ── Main ────────────────────────────────────────────────────────

async def main():
    kernel = Kernel()
    kernel.discover([ConfigProvider, LoggingProvider, CoreApp])
    kernel.instantiate("config", properties={"defaults": {}})
    await kernel.boot()

    print("  === Core app running (no extensions) ===")
    await kernel.invoke("core-app.do_work", {})
    assert kernel.get_schema("slack-notifier.notify") is None
    print("    No Slack handler registered yet.")

    # Install the Slack extension bundle
    print()
    print("  === Installing Slack bundle (3 components) ===")
    for cls in SLACK_BUNDLE:
        await kernel.hot_add(cls)
    slack_schemas = [s.name for s in kernel.runnables() if "slack" in s.provider]
    print(f"    Runnables now: {slack_schemas}")

    # Core work now triggers Slack notification automatically
    print()
    print("  === Core app does work → auto-notifies Slack ===")
    await kernel.invoke("core-app.do_work", {})
    await asyncio.sleep(0.01)  # let async subscribe handler run

    notifier = kernel.lifecycle.get_instance("slack-notifier").instance
    commands = kernel.lifecycle.get_instance("slack-commands").instance
    print(f"    Notifier sent: {notifier.sent}")
    print(f"    Commands handled: {commands.handled}")

    # Direct Slack calls also work
    print()
    print("  === Direct Slack calls ===")
    await kernel.invoke("slack-notifier.notify", {
        "channel": "dev", "message": "Deploy successful"
    })

    # Remove the bundle
    print()
    print("  === Removing Slack bundle ===")
    for name in ["slack-commands", "slack-webhook", "slack-notifier"]:
        await kernel.hot_remove(name)
    assert kernel.get_schema("slack-notifier.notify") is None
    print("    Slack handlers removed.")

    await kernel.shutdown()
    print()
    print("  Extension bundle demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
