"""Example 14 — Clean Unit Testing Pattern

Components are isolated by design: every dependency is injected via
@requires and accessed through self.rt. This means testing is trivial —
boot a minimal kernel with the component under test, the real
ConfigProvider (it's a leaf, no deps), and lightweight fakes for
anything else.

Domain: testing / quality.
Shows: minimal kernel for unit tests, lightweight fakes, testing
       @effect/@computed reactivity, testing runnables, bus assertions.

Three testing strategies demonstrated:
  1. Minimal kernel — real config + fake logger, test one component
  2. Reactive testing — verify @effect/@computed react to config changes
  3. Bus event testing — verify publish/subscribe interactions

Key insight: use the REAL ConfigProvider — it's Signal-backed, has zero
required deps, and gives you reactive config for free. Only fake the
things that have side effects (logger, storage, external APIs).

No kernel changes required.

Run: PYTHONPATH=src python -m signalpy.examples.unit_testing
"""
import asyncio
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    computed, effect, subscribe,
)
from signalpy.providers.config import ConfigProvider


# ══════════════════════════════════════════════════════════════════
# The component under test
# ══════════════════════════════════════════════════════════════════

class OrderParams(BaseModel):
    item: str = ""
    quantity: int = 1
    price: float = 0.0


@component("order-service", version="1.0")
@requires(config="IConfig", logger="ILogger")
class OrderService:
    """A service that processes orders. Has @computed and @effect."""

    @lifecycle.activate
    def activate(self):
        self._orders = []
        self._total_revenue = 0.0

    @computed
    def tax_rate(self):
        """Tax rate from config. Reactive — recomputes when config changes."""
        return self.rt.config.get("orders.tax_rate", 0.1)

    @effect
    def on_config_change(self):
        """Log when config changes. Auto-tracks deps."""
        rate = self.rt.config.get("orders.tax_rate", 0.1)
        self.rt.logger.info(f"Tax rate is now {rate}")

    @runnable("place", params=OrderParams, description="Place an order")
    async def place_order(self, params):
        tax = params.price * params.quantity * self.tax_rate()
        total = params.price * params.quantity + tax
        order = {
            "item": params.item,
            "quantity": params.quantity,
            "subtotal": params.price * params.quantity,
            "tax": round(tax, 2),
            "total": round(total, 2),
        }
        self._orders.append(order)
        self._total_revenue += total
        await self.rt.publish("order.placed", order)
        return order

    @runnable("revenue", params=BaseModel, description="Total revenue")
    async def revenue(self, params):
        return {"total": round(self._total_revenue, 2), "count": len(self._orders)}


# ══════════════════════════════════════════════════════════════════
# Lightweight fakes — only fake what has side effects
# ══════════════════════════════════════════════════════════════════

@component("fake-logger")
@provides("ILogger")
class FakeLogger:
    """Captures log calls for assertion. No actual I/O."""
    @lifecycle.activate
    def activate(self):
        self.messages = []

    def info(self, msg, **kw):
        self.messages.append(("info", msg))

    def warning(self, msg, **kw):
        self.messages.append(("warning", msg))

    def error(self, msg, **kw):
        self.messages.append(("error", msg))

    def debug(self, msg, **kw):
        self.messages.append(("debug", msg))


# ══════════════════════════════════════════════════════════════════
# Bus event collector — captures published events for assertion
# ══════════════════════════════════════════════════════════════════

@component("event-collector")
class EventCollector:
    """Subscribes to events and captures them for test assertions."""
    @lifecycle.activate
    def activate(self):
        self.events = []

    @subscribe("order.*", description="Capture all order events")
    def on_order_event(self, event_type, data):
        self.events.append({"type": event_type, "data": data})


# ══════════════════════════════════════════════════════════════════
# Demo: three testing strategies
# ══════════════════════════════════════════════════════════════════

async def main():
    # ── Strategy 1: Minimal kernel with real config + fake logger ──
    print("  === Strategy 1: Minimal kernel (real config + fake logger) ===")

    kernel = Kernel()
    kernel.discover([ConfigProvider, FakeLogger, OrderService])
    # Pre-configure with test defaults
    kernel.instantiate("config", properties={
        "defaults": {"orders": {"tax_rate": 0.08}}
    })
    await kernel.boot()

    # Test the runnable via schema handler
    result = await kernel.invoke("order-service.place", {
        "item": "Widget", "quantity": 3, "price": 10.0,
    })
    print(f"    Order: {result}")
    assert result["subtotal"] == 30.0
    assert result["tax"] == 2.4  # 30 * 0.08
    assert result["total"] == 32.4

    # Test revenue accumulation
    await kernel.invoke("order-service.place", {
        "item": "Gadget", "quantity": 1, "price": 100.0,
    })
    rev = await kernel.invoke("order-service.revenue", {})
    print(f"    Revenue: {rev}")
    assert rev["count"] == 2

    # Assert on the fake logger — @effect should have logged
    logger = kernel.lifecycle.get_instance("fake-logger").instance
    print(f"    Logger captured: {logger.messages}")
    assert any("0.08" in msg for _, msg in logger.messages)

    await kernel.shutdown()

    # ── Strategy 2: Reactive testing ──────────────────────────────
    print()
    print("  === Strategy 2: Reactive testing (@computed + @effect) ===")

    kernel = Kernel()
    kernel.discover([ConfigProvider, FakeLogger, OrderService])
    kernel.instantiate("config", properties={
        "defaults": {"orders": {"tax_rate": 0.1}}
    })
    await kernel.boot()

    svc = kernel.lifecycle.get_instance("order-service").instance
    config = kernel.registry.require("IConfig")
    logger = kernel.lifecycle.get_instance("fake-logger").instance

    # Initial tax rate
    assert svc.tax_rate() == 0.1
    print(f"    Initial tax rate: {svc.tax_rate()}")

    # Change config — @computed recomputes, @effect re-runs
    logger.messages.clear()
    config.set("orders.tax_rate", 0.15)

    print(f"    After config.set: {svc.tax_rate()}")
    assert svc.tax_rate() == 0.15  # @computed recomputed

    # @effect should have logged the new rate
    assert any("0.15" in msg for _, msg in logger.messages)
    print(f"    Effect logged: {logger.messages}")

    await kernel.shutdown()

    # ── Strategy 3: Bus event testing ─────────────────────────────
    print()
    print("  === Strategy 3: Bus event testing ===")

    kernel = Kernel()
    kernel.discover([ConfigProvider, FakeLogger, OrderService, EventCollector])
    kernel.instantiate("config", properties={
        "defaults": {"orders": {"tax_rate": 0.1}}
    })
    await kernel.boot()

    collector = kernel.lifecycle.get_instance("event-collector").instance
    await kernel.invoke("order-service.place", {
        "item": "Test Item", "quantity": 2, "price": 50.0,
    })

    print(f"    Events captured: {len(collector.events)}")
    assert len(collector.events) == 1
    assert collector.events[0]["type"] == "order.placed"
    assert collector.events[0]["data"]["item"] == "Test Item"
    print(f"    Event: {collector.events[0]}")

    await kernel.shutdown()

    print()
    print("  Unit testing demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
