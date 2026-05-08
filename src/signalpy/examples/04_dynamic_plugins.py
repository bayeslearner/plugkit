"""Example 04 — Dynamic Plugin System

A notification system where plugins (email, slack, sms) can be
hot-added and hot-removed at runtime. The dispatcher collects all
plugins as a list and reactively updates when plugins change.
Domain: notification / alerting.
Shows: list[C] aggregate injection, hot_add, hot_remove, @effect.

Run: PYTHONPATH=. python examples/04_dynamic_plugins.py
"""
import asyncio
from typing import Protocol, runtime_checkable
from pydantic import BaseModel
from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle, effect, prop


@runtime_checkable
class INotifier(Protocol):
    def notify(self, message: str) -> str: ...


class NotifyParams(BaseModel):
    message: str


# ── Plugins ──────────────────────────────────────────────────────

@component("email-notifier")
@provides(INotifier)
@prop("_channel", "channel", "email")
class EmailNotifier:
    @lifecycle.activate
    def activate(self):
        print("  [+] Email notifier loaded")
    def notify(self, message):
        return f"EMAIL: {message}"

@component("slack-notifier")
@provides(INotifier)
@prop("_channel", "channel", "slack")
class SlackNotifier:
    @lifecycle.activate
    def activate(self):
        print("  [+] Slack notifier loaded")
    def notify(self, message):
        return f"SLACK: {message}"


# ── Dispatcher ───────────────────────────────────────────────────

@component("dispatcher")
@requires(notifiers=list[INotifier])
class NotificationDispatcher:
    @lifecycle.activate
    def activate(self):
        self.plugin_count_log = []

    @effect
    def track_plugins(self):
        """Auto-tracks self.rt.notifiers. Re-runs when plugins change."""
        count = len(self.rt.notifiers)
        self.plugin_count_log.append(count)
        print(f"  [effect] {count} notifier(s) active")

    @runnable("send", params=NotifyParams, description="Send to all channels")
    async def send(self, params):
        results = []
        for notifier in self.rt.notifiers:
            results.append(notifier.notify(params.message))
        return {"sent": results}


async def main():
    kernel = Kernel()
    kernel.discover([EmailNotifier, SlackNotifier, NotificationDispatcher])
    await kernel.boot()

    print()

    # Find and call the runnable schema directly
    # Send to all channels
    r = await kernel.invoke("dispatcher.send", {"message": "Server is down!"})
    print(f"  Sent: {r['sent']}")

    # Hot-add SMS plugin
    print()
    @component("sms-notifier")
    @provides(INotifier)
    @prop("_channel", "channel", "sms")
    class SMSNotifier:
        @lifecycle.activate
        def activate(self):
            print("  [+] SMS notifier loaded")
        def notify(self, message):
            return f"SMS: {message}"

    await kernel.hot_add(SMSNotifier)

    r = await kernel.invoke("dispatcher.send", {"message": "Disk full!"})
    print(f"  Sent: {r['sent']}")

    # Hot-remove Slack
    print()
    await kernel.hot_remove("slack-notifier")
    print("  [-] Slack notifier removed")

    r = await kernel.invoke("dispatcher.send", {"message": "All clear."})
    print(f"  Sent: {r['sent']}")

    dispatcher = kernel.lifecycle.get_instance("dispatcher").instance
    print(f"\n  Plugin count history: {dispatcher.plugin_count_log}")

    await kernel.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
