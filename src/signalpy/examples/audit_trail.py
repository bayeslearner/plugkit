"""Example 13 — Audit Trail / Observability

Every bus invocation is logged. Policies control which components
get audited. An audit component subscribes to bus events and
builds a queryable trail.

Domain: compliance / observability.
Shows: kernel.set_policy(audit=True), bus audit logging,
       @subscribe for event capture, policy-driven instrumentation.

No kernel changes required.

Run: PYTHONPATH=src python -m signalpy.examples.audit_trail
"""
import asyncio
import json
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    subscribe, effect,
)
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider


# ── Business components ─────────────────────────────────────────

class TransferParams(BaseModel):
    from_account: str = ""
    to_account: str = ""
    amount: float = 0.0


@component("accounts", version="1.0")
@requires(config="IConfig")
class AccountService:
    """Manages user accounts."""

    @lifecycle.activate
    def activate(self):
        self._balances = {"alice": 1000.0, "bob": 500.0, "charlie": 250.0}

    @runnable("transfer", params=TransferParams, description="Transfer money")
    async def transfer(self, params):
        if params.from_account not in self._balances:
            return {"error": f"Unknown account: {params.from_account}"}
        if self._balances[params.from_account] < params.amount:
            return {"error": "Insufficient funds"}
        self._balances[params.from_account] -= params.amount
        self._balances.setdefault(params.to_account, 0.0)
        self._balances[params.to_account] += params.amount
        # Publish event for audit trail
        await self.rt.publish("account.transfer", {
            "from": params.from_account,
            "to": params.to_account,
            "amount": params.amount,
        })
        return {"ok": True, "balances": dict(self._balances)}

    @runnable("balance", params=BaseModel, description="Get balances")
    async def balance(self, params):
        return dict(self._balances)


class NotifyParams(BaseModel):
    user: str = ""
    message: str = ""


@component("notifications", version="1.0")
@requires(config="IConfig")
class NotificationService:
    """Sends notifications to users."""

    @lifecycle.activate
    def activate(self):
        self.sent = []

    @runnable("send", params=NotifyParams, description="Send a notification")
    async def send(self, params):
        self.sent.append(f"{params.user}: {params.message}")
        return {"sent": True}

    @subscribe("account.transfer", description="Notify on transfer")
    async def on_transfer(self, event_type, data):
        # Direct call to own method (no bus round-trip needed)
        await self.send(NotifyParams(
            user=data["to"],
            message=f"You received ${data['amount']:.2f} from {data['from']}",
        ))


# ── Audit trail component ──────────────────────────────────────

@component("audit-trail", version="1.0")
@requires(config="IConfig")
class AuditTrail:
    """Captures bus events into a queryable audit log.

    Subscribes to all events matching patterns configured in config.
    """

    @lifecycle.activate
    def activate(self):
        self.entries = []

    @subscribe("account.*", description="Audit all account events")
    def on_account_event(self, event_type, data):
        entry = {"event": event_type, "data": data}
        self.entries.append(entry)
        print(f"    [audit] {json.dumps(entry)}")

    @runnable("query", params=BaseModel, description="Query audit entries")
    async def query(self, params):
        event_filter = params.get("event", None) if isinstance(params, dict) else None
        if event_filter:
            return [e for e in self.entries if e["event"] == event_filter]
        return list(self.entries)


# ── Main ────────────────────────────────────────────────────────

async def main():
    kernel = Kernel()
    kernel.discover([
        ConfigProvider, LoggingProvider,
        AccountService, NotificationService, AuditTrail,
    ])
    kernel.instantiate("config", properties={"defaults": {}})

    # Enable audit policy on account service — logs all invoke/publish
    kernel.set_policy("accounts", {"audit": True})
    kernel.set_policy("notifications", {"audit": True})

    await kernel.boot()

    print()
    print("  === Transfers with audit trail ===")
    await kernel.invoke("accounts.transfer", {
        "from_account": "alice", "to_account": "bob", "amount": 150.0,
    })
    await asyncio.sleep(0.01)

    await kernel.invoke("accounts.transfer", {
        "from_account": "bob", "to_account": "charlie", "amount": 75.0,
    })
    await asyncio.sleep(0.01)

    # Query the audit trail
    print()
    print("  === Audit trail ===")
    trail = kernel.lifecycle.get_instance("audit-trail").instance
    for entry in trail.entries:
        print(f"    {entry}")

    # Check balances
    print()
    print("  === Final balances ===")
    balances = await kernel.invoke("accounts.balance", {})
    for name, bal in sorted(balances.items()):
        print(f"    {name}: ${bal:.2f}")

    # Check notifications
    notif = kernel.lifecycle.get_instance("notifications").instance
    print()
    print("  === Notifications sent ===")
    for n in notif.sent:
        print(f"    {n}")

    await kernel.shutdown()
    print()
    print("  Audit trail demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
