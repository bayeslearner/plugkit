"""Example 12 — Health Check / Circuit Breaker

A payment service depends on an external API. When the API becomes
unhealthy, the circuit breaker opens and requests fail fast instead
of timing out. When the API recovers, the breaker closes and traffic
resumes — all driven by reactive state.

Domain: resilience / fault tolerance.
Shows: @computed health status, @effect watching state changes,
       Signal-driven circuit breaker, config-driven thresholds.

No kernel changes required.

Run: PYTHONPATH=src python -m signalpy.examples.circuit_breaker
"""
import asyncio
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    computed, effect, Signal,
)
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider


# ── External API (simulated) ────────────────────────────────────

@component("ext-api", version="1.0")
@provides("IExternalAPI")
class ExternalAPI:
    """Simulated external API that can go up/down."""

    @lifecycle.activate
    def activate(self):
        self._healthy = Signal(True)
        self.call_count = 0

    @runnable("call", params=BaseModel, description="Call the external API")
    async def call(self, params):
        if not self._healthy.peek():
            raise ConnectionError("External API is down")
        self.call_count += 1
        return {"ok": True}

    @runnable("set_health", params=BaseModel, description="Simulate health change")
    async def set_health(self, params):
        healthy = params.get("healthy", True) if isinstance(params, dict) else True
        self._healthy.set(healthy)
        return {"healthy": healthy}

    @lifecycle.health
    def health(self):
        return {"healthy": self._healthy.peek()}


# ── Circuit breaker wrapping the API ────────────────────────────

class PayParams(BaseModel):
    amount: float = 0.0


@component("payment-svc", version="1.0")
@requires(config="IConfig", ext_api="IExternalAPI")
class PaymentService:
    """Payment service with a circuit breaker around the external API.

    The breaker tracks consecutive failures. When failures exceed the
    threshold (from config), the circuit opens and requests fail fast.
    A probe call checks if the API recovered — if so, the circuit closes.
    """

    @lifecycle.activate
    def activate(self):
        self._consecutive_failures = 0
        self._state = Signal("closed")  # closed | open | half-open
        self.event_log = []

    @computed
    def breaker_state(self):
        """Current circuit breaker state. Reactive."""
        return self._state.get()

    @computed
    def failure_threshold(self):
        """Max failures before opening. Reactive — config-driven."""
        return self.rt.config.get("circuit.failure_threshold", 3)

    @effect
    def on_state_change(self):
        """Log circuit breaker state transitions."""
        state = self._state.get()
        self.event_log.append(f"breaker:{state}")
        print(f"    [circuit-breaker] State: {state}")

    @runnable("pay", params=PayParams, description="Process a payment")
    async def pay(self, params):
        state = self._state.peek()

        if state == "open":
            # Try a probe call (half-open)
            self._state.set("half-open")
            try:
                await self.rt.ext_api.call({})
                # Success — close the breaker
                self._consecutive_failures = 0
                self._state.set("closed")
            except Exception:
                self._state.set("open")
                return {"error": "circuit open", "amount": params.amount}

        try:
            await self.rt.ext_api.call({})
            self._consecutive_failures = 0
            return {"paid": True, "amount": params.amount}
        except Exception:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold():
                self._state.set("open")
            return {"error": "api_failure", "failures": self._consecutive_failures}

    @runnable("status", params=BaseModel, description="Breaker status")
    async def status(self, params):
        return {
            "state": self.breaker_state(),
            "failures": self._consecutive_failures,
            "threshold": self.failure_threshold(),
        }


# ── Main ────────────────────────────────────────────────────────

async def main():
    kernel = Kernel()
    kernel.discover([ConfigProvider, LoggingProvider, ExternalAPI, PaymentService])
    kernel.instantiate("config", properties={
        "defaults": {"circuit": {"failure_threshold": 3}}
    })
    await kernel.boot()

    print()
    print("  === Normal operation (API healthy) ===")
    r = await kernel.invoke("payment-svc.pay", {"amount": 100.0})
    print(f"    Pay $100: {r}")
    r = await kernel.invoke("payment-svc.pay", {"amount": 200.0})
    print(f"    Pay $200: {r}")

    # Take the API down
    print()
    print("  === API goes down ===")
    await kernel.invoke("ext-api.set_health", {"healthy": False})

    for i in range(4):
        r = await kernel.invoke("payment-svc.pay", {"amount": 50.0})
        status = await kernel.invoke("payment-svc.status", {})
        print(f"    Attempt {i+1}: {r}  (breaker: {status['state']})")

    # Bring API back
    print()
    print("  === API recovers ===")
    await kernel.invoke("ext-api.set_health", {"healthy": True})

    r = await kernel.invoke("payment-svc.pay", {"amount": 300.0})
    print(f"    Pay $300 (probe): {r}")

    status = await kernel.invoke("payment-svc.status", {})
    print(f"    Final status: {status}")

    svc = kernel.lifecycle.get_instance("payment-svc").instance
    print(f"    Event log: {svc.event_log}")

    await kernel.shutdown()
    print()
    print("  Circuit breaker demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
