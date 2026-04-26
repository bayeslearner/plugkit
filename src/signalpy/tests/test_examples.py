"""Tests for commercial pattern examples (08-13).

Each test verifies the example's core scenario works end-to-end.
"""
import asyncio
import pytest
from pydantic import BaseModel

from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    computed, effect, prop,
)
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider
from signalpy.providers.credentials import CredentialProvider


# ══════════════════════════════════════════════════════════════════
# Example 08: Secret Rotation
# ══════════════════════════════════════════════════════════════════

class TestSecretRotation:
    @pytest.mark.asyncio
    async def test_effect_fires_on_credential_change(self):
        """Credential change in config triggers consumer @effect."""
        reconnect_log = []

        @component("test-db", depends=["config", "credentials"])
        @requires(config="IConfig", creds="ICredentials")
        class TestDB:
            @lifecycle.activate
            def activate(self):
                self._key = None

            @effect
            def on_cred_change(self):
                key = self.rt.creds.get("api_key", target="prod")
                if key and key != self._key:
                    self._key = key
                    reconnect_log.append(key)

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, CredentialProvider, TestDB])
        kernel.instantiate("config", properties={
            "defaults": {
                "apps": {"test-db": {"targets": {"prod": {"api_key": "key-v1"}}}}
            }
        })
        await kernel.boot()

        assert reconnect_log == ["key-v1"]

        # Rotate the secret
        config = kernel.registry.require("IConfig")
        config.set("apps.test-db.targets.prod.api_key", "key-v2")
        assert reconnect_log == ["key-v1", "key-v2"]

        # Rotate again
        config.set("apps.test-db.targets.prod.api_key", "key-v3")
        assert reconnect_log == ["key-v1", "key-v2", "key-v3"]

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_vault_rotates_via_bus(self):
        """Vault component rotates secrets via bus runnable."""
        from signalpy.examples import secret_rotation
        DatabaseClient = secret_rotation.DatabaseClient
        VaultSimulator = secret_rotation.VaultSimulator

        kernel = Kernel()
        kernel.discover([
            ConfigProvider, LoggingProvider, CredentialProvider,
            DatabaseClient, VaultSimulator,
        ])
        kernel.instantiate("config", properties={
            "defaults": {
                "apps": {"db-client": {"targets": {"prod": {"api_key": "initial"}}}}
            }
        })
        await kernel.boot()

        db = kernel.lifecycle.get_instance("db-client").instance
        assert len(db.connection_log) == 1  # initial connect

        await kernel.bus.invoke("vault.rotate", {})
        await asyncio.sleep(0.01)
        assert len(db.connection_log) == 2  # rotated once

        await kernel.bus.invoke("vault.rotate", {})
        await asyncio.sleep(0.01)
        assert len(db.connection_log) == 3  # rotated twice

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# Example 09: A/B Testing
# ══════════════════════════════════════════════════════════════════

class TestABTesting:
    @pytest.mark.asyncio
    async def test_split_routing(self):
        """A/B router directs traffic based on config split percentage."""
        from signalpy.examples.ab_testing import SearchV1, SearchV2, ABRouter

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, SearchV1, SearchV2, ABRouter])
        kernel.instantiate("config", properties={
            "defaults": {"ab": {"search": {"v2_percent": 0}}}
        })
        await kernel.boot()

        v1 = kernel.lifecycle.get_instance("search-v1").instance
        v2 = kernel.lifecycle.get_instance("search-v2").instance
        config = kernel.registry.require("IConfig")

        # 0% v2 — all traffic on v1
        for uid in ["a", "b", "c"]:
            await kernel.bus.invoke("search-router.search", {"query": "q", "user_id": uid})
        assert v1._call_count == 3
        assert v2._call_count == 0

        # 100% v2 — all traffic on v2
        config.set("ab.search.v2_percent", 100)
        v1._call_count = 0
        v2._call_count = 0
        for uid in ["a", "b", "c"]:
            await kernel.bus.invoke("search-router.search", {"query": "q", "user_id": uid})
        assert v1._call_count == 0
        assert v2._call_count == 3

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_consistent_bucketing(self):
        """Same user_id always routes to the same variant."""
        from signalpy.examples.ab_testing import SearchV1, SearchV2, ABRouter

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, SearchV1, SearchV2, ABRouter])
        kernel.instantiate("config", properties={
            "defaults": {"ab": {"search": {"v2_percent": 50}}}
        })
        await kernel.boot()

        # Same user_id, called twice — should get same variant
        r1 = await kernel.bus.invoke("search-router.search", {"query": "q", "user_id": "alice"})
        r2 = await kernel.bus.invoke("search-router.search", {"query": "q", "user_id": "alice"})
        assert r1["variant"] == r2["variant"]

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# Example 10: Multi-Tenant Isolation
# ══════════════════════════════════════════════════════════════════

class TestMultiTenant:
    @pytest.mark.asyncio
    async def test_target_routing_isolates_tenants(self):
        """Each tenant routes to its own instance via target param."""
        from signalpy.examples.multi_tenant import TenantDatabase

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, CredentialProvider, TenantDatabase])
        kernel.instantiate("config", properties={
            "defaults": {
                "tenants": {
                    "t1": {"db_url": "postgres://t1"},
                    "t2": {"db_url": "postgres://t2"},
                }
            }
        })
        kernel.instantiate("tenant-db", "tenant-db-t1", {"target": "t1"})
        kernel.instantiate("tenant-db", "tenant-db-t2", {"target": "t2"})
        await kernel.boot()

        r1 = await kernel.bus.invoke("tenant-db.query", {"table": "x", "target": "t1"})
        r2 = await kernel.bus.invoke("tenant-db.query", {"table": "x", "target": "t2"})
        assert r1["tenant"] == "t1"
        assert r1["db_url"] == "postgres://t1"
        assert r2["tenant"] == "t2"
        assert r2["db_url"] == "postgres://t2"

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_tenant_config_change_propagates(self):
        """Changing a tenant's config reactively updates its computed."""
        from signalpy.examples.multi_tenant import TenantDatabase

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, CredentialProvider, TenantDatabase])
        kernel.instantiate("config", properties={
            "defaults": {"tenants": {"t1": {"db_url": "postgres://old"}}}
        })
        kernel.instantiate("tenant-db", "tenant-db-t1", {"target": "t1"})
        await kernel.boot()

        config = kernel.registry.require("IConfig")
        config.set("tenants.t1.db_url", "postgres://new")

        r = await kernel.bus.invoke("tenant-db.query", {"table": "x", "target": "t1"})
        assert r["db_url"] == "postgres://new"

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# Example 11: Extension Bundle
# ══════════════════════════════════════════════════════════════════

class TestExtensionBundle:
    @pytest.mark.asyncio
    async def test_hot_add_bundle_and_cross_component_calls(self):
        """Hot-adding a bundle wires up cross-component subscriptions."""
        from signalpy.examples.extension_bundle import (
            CoreApp, SlackNotifier, SlackWebhookReceiver, SlackCommandHandler,
        )

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, CoreApp])
        kernel.instantiate("config", properties={"defaults": {}})
        await kernel.boot()

        # No Slack yet
        assert not kernel.bus.has_handler("slack-notifier.notify")

        # Install bundle
        for cls in [SlackNotifier, SlackWebhookReceiver, SlackCommandHandler]:
            await kernel.hot_add(cls)
        assert kernel.bus.has_handler("slack-notifier.notify")

        # Core work triggers Slack notification via @subscribe
        await kernel.bus.invoke("core-app.do_work", {})
        await asyncio.sleep(0.01)

        notifier = kernel.lifecycle.get_instance("slack-notifier").instance
        assert len(notifier.sent) == 1
        assert "Work completed" in notifier.sent[0]

        # Remove bundle
        for name in ["slack-commands", "slack-webhook", "slack-notifier"]:
            await kernel.hot_remove(name)
        assert not kernel.bus.has_handler("slack-notifier.notify")

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# Example 12: Circuit Breaker
# ══════════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_breaker_opens_after_threshold_failures(self):
        """Circuit opens after N consecutive failures, closes on recovery."""
        from signalpy.examples.circuit_breaker import ExternalAPI, PaymentService

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, ExternalAPI, PaymentService])
        kernel.instantiate("config", properties={
            "defaults": {"circuit": {"failure_threshold": 2}}
        })
        await kernel.boot()

        svc = kernel.lifecycle.get_instance("payment-svc").instance

        # Normal operation
        r = await kernel.bus.invoke("payment-svc.pay", {"amount": 10})
        assert r["paid"] is True
        assert svc.breaker_state() == "closed"

        # Take API down
        await kernel.bus.invoke("ext-api.set_health", {"healthy": False})

        # 2 failures → breaker opens (threshold=2)
        await kernel.bus.invoke("payment-svc.pay", {"amount": 10})
        r = await kernel.bus.invoke("payment-svc.pay", {"amount": 10})
        assert svc.breaker_state() == "open"

        # Next call fails fast (circuit open)
        r = await kernel.bus.invoke("payment-svc.pay", {"amount": 10})
        assert r["error"] == "circuit open"

        # Recover API → breaker closes via probe
        await kernel.bus.invoke("ext-api.set_health", {"healthy": True})
        r = await kernel.bus.invoke("payment-svc.pay", {"amount": 10})
        assert r["paid"] is True
        assert svc.breaker_state() == "closed"

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# Example 13: Audit Trail
# ══════════════════════════════════════════════════════════════════

class TestAuditTrail:
    @pytest.mark.asyncio
    async def test_audit_captures_events_and_policy_enables_logging(self):
        """Audit trail captures bus events; policy enables invoke logging."""
        from signalpy.examples.audit_trail import (
            AccountService, NotificationService, AuditTrail,
        )

        kernel = Kernel()
        kernel.discover([
            ConfigProvider, LoggingProvider,
            AccountService, NotificationService, AuditTrail,
        ])
        kernel.instantiate("config", properties={"defaults": {}})
        kernel.set_policy("accounts", {"audit": True})
        await kernel.boot()

        # Transfer triggers event → audit captures it
        await kernel.bus.invoke("accounts.transfer", {
            "from_account": "alice", "to_account": "bob", "amount": 50.0,
        })
        await asyncio.sleep(0.01)

        trail = kernel.lifecycle.get_instance("audit-trail").instance
        assert len(trail.entries) == 1
        assert trail.entries[0]["event"] == "account.transfer"
        assert trail.entries[0]["data"]["amount"] == 50.0

        # Notification was sent via cross-component call
        notif = kernel.lifecycle.get_instance("notifications").instance
        assert len(notif.sent) == 1
        assert "bob" in notif.sent[0]

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# Example 14: Unit Testing Patterns
# ══════════════════════════════════════════════════════════════════

class TestUnitTestingPatterns:
    @pytest.mark.asyncio
    async def test_minimal_kernel_with_fakes(self):
        """Boot only the component under test + minimal fakes."""
        from signalpy.examples.unit_testing import OrderService, FakeLogger

        kernel = Kernel()
        kernel.discover([ConfigProvider, FakeLogger, OrderService])
        kernel.instantiate("config", properties={
            "defaults": {"orders": {"tax_rate": 0.08}}
        })
        await kernel.boot()

        r = await kernel.bus.invoke("order-service.place", {
            "item": "Widget", "quantity": 2, "price": 25.0,
        })
        assert r["subtotal"] == 50.0
        assert r["tax"] == 4.0  # 50 * 0.08
        assert r["total"] == 54.0

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_reactive_computed_updates_on_config_change(self):
        """@computed reflects config changes instantly."""
        from signalpy.examples.unit_testing import OrderService, FakeLogger

        kernel = Kernel()
        kernel.discover([ConfigProvider, FakeLogger, OrderService])
        kernel.instantiate("config", properties={
            "defaults": {"orders": {"tax_rate": 0.1}}
        })
        await kernel.boot()

        svc = kernel.lifecycle.get_instance("order-service").instance
        assert svc.tax_rate() == 0.1

        config = kernel.registry.require("IConfig")
        config.set("orders.tax_rate", 0.2)
        assert svc.tax_rate() == 0.2

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_bus_event_capture_with_collector(self):
        """EventCollector captures bus events for assertion."""
        from signalpy.examples.unit_testing import OrderService, FakeLogger, EventCollector

        kernel = Kernel()
        kernel.discover([ConfigProvider, FakeLogger, OrderService, EventCollector])
        kernel.instantiate("config", properties={"defaults": {}})
        await kernel.boot()

        await kernel.bus.invoke("order-service.place", {
            "item": "Test", "quantity": 1, "price": 10.0,
        })

        collector = kernel.lifecycle.get_instance("event-collector").instance
        assert len(collector.events) == 1
        assert collector.events[0]["type"] == "order.placed"

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════
# Example 15: Hot Code Update
# ══════════════════════════════════════════════════════════════════

class TestHotUpdate:
    @pytest.mark.asyncio
    async def test_hot_update_preserves_state(self):
        """hot_update replaces the class but preserves snapshot state."""
        from signalpy.examples.hot_update import SearchV1, SearchV2

        kernel = Kernel()
        kernel.discover([ConfigProvider, LoggingProvider, SearchV1])
        kernel.instantiate("config", properties={"defaults": {}})
        await kernel.boot()

        # Build state in V1
        await kernel.bus.invoke("search.index_doc", {"id": "a", "text": "hello world"})
        await kernel.bus.invoke("search.index_doc", {"id": "b", "text": "hello there"})
        await kernel.bus.invoke("search.search", {"query": "hello"})

        status = await kernel.bus.invoke("search.status", {})
        assert status["version"] == "1.0"
        assert status["docs"] == 2
        assert status["queries"] == 1

        # Hot update to V2
        await kernel.hot_update(SearchV2)

        # State preserved, version changed
        status = await kernel.bus.invoke("search.status", {})
        assert status["version"] == "2.0"
        assert status["docs"] == 2       # index preserved
        assert status["queries"] == 1    # count preserved

        # V2 search returns scored results
        r = await kernel.bus.invoke("search.search", {"query": "hello"})
        assert r["engine"] == "v2-scored"
        assert len(r["results"]) == 2
        assert all("score" in hit for hit in r["results"])
        assert r["total_queries"] == 2   # count continued

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_hot_update_fallback_dict_snapshot(self):
        """Without @lifecycle.snapshot, hot_update uses __dict__ fallback."""
        @component("simple-svc", version="1.0")
        @provides("ISimple")
        class SimpleV1:
            @lifecycle.activate
            def activate(self):
                self.counter = 0
                self.data = {"key": "v1"}

            @runnable("inc", params=BaseModel, description="Increment")
            async def inc(self, params):
                self.counter += 1
                return {"counter": self.counter}

        @component("simple-svc", version="2.0")
        @provides("ISimple")
        class SimpleV2:
            @lifecycle.activate
            def activate(self):
                self.counter = 0
                self.data = {}

            @runnable("inc", params=BaseModel, description="Increment v2")
            async def inc(self, params):
                self.counter += 10  # v2 increments by 10
                return {"counter": self.counter}

        kernel = Kernel()
        kernel.discover([SimpleV1])
        await kernel.boot()

        await kernel.bus.invoke("simple-svc.inc", {})
        await kernel.bus.invoke("simple-svc.inc", {})
        # counter = 2, data = {"key": "v1"}

        await kernel.hot_update(SimpleV2)

        # __dict__ fallback should have preserved counter and data
        svc = kernel.lifecycle.get_instance("simple-svc").instance
        assert svc.counter == 2
        assert svc.data == {"key": "v1"}

        # V2 behavior (increments by 10)
        r = await kernel.bus.invoke("simple-svc.inc", {})
        assert r["counter"] == 12  # 2 + 10

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_plugin_loader_discovers_and_hot_updates(self):
        """PluginLoader scans directory, imports .py files, hot_adds and hot_updates."""
        import shutil
        import tempfile
        from pathlib import Path
        from signalpy.providers.plugin_loader import PluginLoader

        here = Path(__file__).parent.parent / "examples" / "hot_update_demo"

        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)

            kernel = Kernel()
            kernel.discover([ConfigProvider, LoggingProvider, PluginLoader])
            kernel.instantiate("config", properties={"defaults": {}})
            kernel.instantiate("plugin-loader", properties={
                "plugin_dir": str(plugin_dir),
                "kernel": kernel,
            })
            await kernel.boot()

            # Deploy v1
            shutil.copy2(here / "search_v1.py", plugin_dir / "search.py")
            await kernel.bus.invoke("plugin-loader.scan", {})

            await kernel.bus.invoke("search.index_doc", {"id": "1", "text": "hello"})
            await kernel.bus.invoke("search.search", {"query": "hello"})
            status = await kernel.bus.invoke("search.status", {})
            assert status["version"] == "1.0"
            assert status["docs"] == 1
            assert status["queries"] == 1

            # Deploy v2 (overwrite)
            shutil.copy2(here / "search_v2.py", plugin_dir / "search.py")
            await kernel.bus.invoke("plugin-loader.scan", {})

            status = await kernel.bus.invoke("search.status", {})
            assert status["version"] == "2.0"
            assert status["docs"] == 1      # preserved
            assert status["queries"] == 1   # preserved

            await kernel.shutdown()
