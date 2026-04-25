"""Tests for iPOPO parity features: dynamic injection, bind/unbind,
service ranking, requires_map, requires_best, service factory,
config admin, erroneous retry, and bus-level auth enforcement.

These tests follow the spell-checker tutorial pattern from iPOPO:
multiple dictionaries → aggregated into a spell checker → used by a client.
"""
import asyncio
import pytest
from pydantic import BaseModel

from reactpy.kernel import (
    Kernel, component, provides, requires, requires_aggregate, requires_map,
    requires_best, runnable, lifecycle, api, bind, unbind, prop,
)
from reactpy.kernel.component import get_meta, _finalize_meta
from reactpy.kernel.lifecycle_manager import State
from reactpy.kernel.registry import ServiceRegistry


# ══════════════════════════════════════════════════════════════════════
# Tier 1: aggregate, optional, bind/unbind, property, ranking
# ══════════════════════════════════════════════════════════════════════


# ── Test components (spell-checker pattern) ──────────────────────────

@component("dict-en", version="1.0")
@provides("IDictionary")
@prop("_language", "language", "EN")
@prop("_ranking", "service.ranking", 0)
class EnglishDict:
    @lifecycle.activate
    def activate(self):
        self.words = {"hello", "world", "welcome", "to", "the"}

    def check_word(self, word: str) -> bool:
        return word.lower() in self.words


@component("dict-fr", version="1.0")
@provides("IDictionary")
@prop("_language", "language", "FR")
@prop("_ranking", "service.ranking", 1)
class FrenchDict:
    @lifecycle.activate
    def activate(self):
        self.words = {"bonjour", "le", "monde"}

    def check_word(self, word: str) -> bool:
        return word.lower() in self.words


class CheckParams(BaseModel):
    text: str = ""
    language: str = "EN"


@component("spell-checker", version="1.0")
@provides("ISpellChecker")
@requires_aggregate("dictionaries", "IDictionary")
class SpellCheckerAggregate:
    """Spell checker using aggregate injection — gets all dictionaries."""

    @lifecycle.activate
    def activate(self):
        self.languages = {}

    @bind("dictionaries")
    def on_dict_added(self, service, properties):
        lang = properties.get("language", "??")
        self.languages[lang] = service

    @unbind("dictionaries")
    def on_dict_removed(self, service, properties):
        lang = properties.get("language", "??")
        self.languages.pop(lang, None)

    @runnable("check", params=CheckParams, description="Check spelling")
    async def check(self, params):
        lang = params.language
        text = params.text
        dictionary = self.languages.get(lang)
        if dictionary is None:
            return {"error": f"No dictionary for {lang}"}
        words = text.split()
        misspelled = [w for w in words if not dictionary.check_word(w)]
        return {"misspelled": misspelled}


@component("spell-checker-map", version="1.0")
@provides("ISpellCheckerMap")
@requires_map("dicts", "IDictionary", key="language")
class SpellCheckerMap:
    """Spell checker using map injection — gets dicts keyed by language."""

    @runnable("check", params=CheckParams, description="Check spelling")
    async def check(self, params):
        dictionary = self.rt.dicts.get(params.language)
        if dictionary is None:
            return {"error": f"No dictionary for {params.language}"}
        words = params.text.split()
        misspelled = [w for w in words if not dictionary.check_word(w)]
        return {"misspelled": misspelled}


@component("best-consumer", version="1.0", depends=["dict-en", "dict-fr"])
@requires_best("best_dict", "IDictionary")
class BestDictConsumer:
    """Consumes only the highest-ranked dictionary."""

    @lifecycle.activate
    def activate(self):
        self.got_dict = self.rt.best_dict is not None


# ── Tier 1 Tests ─────────────────────────────────────────────────────


class TestAggregateRequires:
    @pytest.mark.asyncio
    async def test_aggregate_injects_all_services(self):
        """@requires_aggregate injects a list of all matching services."""
        kernel = Kernel()
        kernel.discover([EnglishDict, FrenchDict, SpellCheckerAggregate])
        await kernel.boot()

        sc_ci = kernel.lifecycle.get_instance("spell-checker")
        dicts = sc_ci.instance.rt.dictionaries
        assert isinstance(dicts, list)
        assert len(dicts) == 2

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_aggregate_spell_check_works(self):
        """Aggregate injection allows the spell checker to work."""
        kernel = Kernel()
        kernel.discover([EnglishDict, FrenchDict, SpellCheckerAggregate])
        await kernel.boot()

        result = await kernel.bus.invoke("spell-checker.check", {
            "text": "hello world foo",
            "language": "EN",
        })
        assert result["misspelled"] == ["foo"]

        result = await kernel.bus.invoke("spell-checker.check", {
            "text": "bonjour bar",
            "language": "FR",
        })
        assert result["misspelled"] == ["bar"]

        await kernel.shutdown()


class TestOptionalRequires:
    @pytest.mark.asyncio
    async def test_optional_missing_injects_none(self):
        """Optional dependency injects None when not available."""
        @component("opt-consumer", version="1.0")
        @requires_best("maybe", "INonexistent", optional=True)
        class OptConsumer:
            @lifecycle.activate
            def activate(self):
                self.has_it = self.rt.maybe is not None

        kernel = Kernel()
        kernel.discover([OptConsumer])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("opt-consumer")
        assert ci.state == State.ACTIVE
        assert ci.instance.has_it is False

        await kernel.shutdown()


class TestBindUnbind:
    @pytest.mark.asyncio
    async def test_bind_called_on_service_arrival(self):
        """@bind callback is called when a matching service appears."""
        kernel = Kernel()
        kernel.discover([EnglishDict, FrenchDict, SpellCheckerAggregate])
        await kernel.boot()

        sc = kernel.lifecycle.get_instance("spell-checker").instance
        # bind should have been called for both dictionaries
        assert "EN" in sc.languages
        assert "FR" in sc.languages

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_bind_on_hot_add(self):
        """@bind callback fires when a new service is hot-added."""
        @component("dict-de", version="1.0")
        @provides("IDictionary")
        @prop("_language", "language", "DE")
        class GermanDict:
            @lifecycle.activate
            def activate(self):
                self.words = {"hallo", "welt"}
            def check_word(self, word):
                return word.lower() in self.words

        kernel = Kernel()
        kernel.discover([EnglishDict, SpellCheckerAggregate])
        await kernel.boot()

        sc = kernel.lifecycle.get_instance("spell-checker").instance
        assert "EN" in sc.languages
        assert "DE" not in sc.languages

        # Hot-add German dictionary
        await kernel.hot_add(GermanDict)
        assert "DE" in sc.languages

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_unbind_on_hot_remove(self):
        """@unbind callback fires when a service is hot-removed."""
        kernel = Kernel()
        kernel.discover([EnglishDict, FrenchDict, SpellCheckerAggregate])
        await kernel.boot()

        sc = kernel.lifecycle.get_instance("spell-checker").instance
        assert "FR" in sc.languages

        # Hot-remove French dictionary
        await kernel.hot_remove("dict-fr")
        assert "FR" not in sc.languages

        await kernel.shutdown()


class TestPropertyDecorator:
    def test_prop_sets_default_on_instance(self):
        """@prop sets the default value on the component instance."""
        kernel = Kernel()
        kernel.discover([EnglishDict])
        # We need to manually check after boot
        # The prop decorator stores PropertyDef in meta

        meta = get_meta(EnglishDict)
        _finalize_meta(EnglishDict)
        prop_defs = meta.property_defs
        assert len(prop_defs) == 2
        assert any(p.prop_name == "language" and p.default == "EN" for p in prop_defs)
        assert any(p.prop_name == "service.ranking" and p.default == 0 for p in prop_defs)

    @pytest.mark.asyncio
    async def test_prop_values_propagate_to_service_registry(self):
        """@prop values appear in service registry properties."""
        kernel = Kernel()
        kernel.discover([EnglishDict])
        await kernel.boot()

        entries = kernel.registry.query("IDictionary")
        assert len(entries) == 1
        assert entries[0].properties["language"] == "EN"
        assert entries[0].properties["service.ranking"] == 0

        await kernel.shutdown()


class TestServiceRanking:
    def test_require_returns_highest_ranked(self):
        """require() returns the service with lowest ranking value (highest priority)."""
        reg = ServiceRegistry()
        reg.provide("IFoo", "low-priority", "p1", {"service.ranking": 10})
        reg.provide("IFoo", "high-priority", "p2", {"service.ranking": 1})
        assert reg.require("IFoo") == "high-priority"

    def test_require_all_sorted_by_ranking(self):
        """require_all() returns services sorted by ranking."""
        reg = ServiceRegistry()
        reg.provide("IFoo", "c", "p1", {"service.ranking": 10})
        reg.provide("IFoo", "a", "p2", {"service.ranking": 1})
        reg.provide("IFoo", "b", "p3", {"service.ranking": 5})
        assert reg.require_all("IFoo") == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_requires_best_gets_highest_ranked(self):
        """@requires_best injects the highest-ranked service."""
        kernel = Kernel()
        kernel.discover([EnglishDict, FrenchDict, BestDictConsumer])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("best-consumer")
        # EN has ranking 0, FR has ranking 1 → EN wins
        best = ci.instance.rt.best_dict
        assert hasattr(best, "words")
        assert "hello" in best.words  # It's the English dict

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════════
# Tier 2: requires_map, requires_best, service factory
# ══════════════════════════════════════════════════════════════════════


class TestRequiresMap:
    @pytest.mark.asyncio
    async def test_map_injection_keyed_by_property(self):
        """@requires_map injects a dict keyed by the specified property."""
        kernel = Kernel()
        kernel.discover([EnglishDict, FrenchDict, SpellCheckerMap])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("spell-checker-map")
        dicts = ci.instance.rt.dicts
        assert isinstance(dicts, dict)
        assert "EN" in dicts
        assert "FR" in dicts

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_map_spell_check_works(self):
        """Map-injected spell checker correctly routes by language."""
        kernel = Kernel()
        kernel.discover([EnglishDict, FrenchDict, SpellCheckerMap])
        await kernel.boot()

        result = await kernel.bus.invoke("spell-checker-map.check", {
            "text": "hello foo",
            "language": "EN",
        })
        assert result["misspelled"] == ["foo"]

        await kernel.shutdown()


class TestServiceFactory:
    def test_factory_creates_per_consumer_instances(self):
        """Service Factory creates a separate instance per consumer."""
        reg = ServiceRegistry()

        class MyFactory:
            def __init__(self):
                self.instances = {}

            def get_service(self, consumer_name):
                instance = f"instance-for-{consumer_name}"
                self.instances[consumer_name] = instance
                return instance

            def unget_service(self, consumer_name, instance):
                self.instances.pop(consumer_name, None)

        factory = MyFactory()
        reg.provide("IMyService", factory, "factory-provider", factory=True)

        # Two consumers get different instances
        svc_a = reg.require_for("IMyService", "consumer-a")
        svc_b = reg.require_for("IMyService", "consumer-b")
        assert svc_a == "instance-for-consumer-a"
        assert svc_b == "instance-for-consumer-b"
        assert svc_a != svc_b

        # Same consumer gets cached instance
        svc_a2 = reg.require_for("IMyService", "consumer-a")
        assert svc_a2 is svc_a

    def test_unget_service_cleanup(self):
        """unget_service releases factory-produced instances."""
        reg = ServiceRegistry()

        class MyFactory:
            def __init__(self):
                self.alive = set()

            def get_service(self, consumer_name):
                self.alive.add(consumer_name)
                return f"svc-{consumer_name}"

            def unget_service(self, consumer_name, instance):
                self.alive.discard(consumer_name)

        factory = MyFactory()
        reg.provide("IFoo", factory, "p1", factory=True)

        reg.require_for("IFoo", "c1")
        assert "c1" in factory.alive

        reg.unget_service("IFoo", "c1")
        assert "c1" not in factory.alive

    def test_non_factory_require_for_works_normally(self):
        """require_for on a non-factory service works like require."""
        reg = ServiceRegistry()
        reg.provide("IFoo", "normal-svc", "p1")
        assert reg.require_for("IFoo", "any-consumer") == "normal-svc"


# ══════════════════════════════════════════════════════════════════════
# Tier 3: ConfigAdmin, erroneous retry, auth enforcement
# ══════════════════════════════════════════════════════════════════════


class TestConfigAdmin:
    @pytest.mark.asyncio
    async def test_managed_service_receives_updates(self):
        """ConfigAdmin pushes config updates to managed services."""
        from reactpy.providers.configadmin import ConfigAdminProvider
        from reactpy.providers.config import ConfigProvider

        kernel = Kernel()
        kernel.discover([ConfigProvider, ConfigAdminProvider])
        await kernel.boot()

        # Get the ConfigAdmin instance
        ca = kernel.registry.require("IConfigAdmin")

        # Register a mock managed service
        received = []

        class MockManaged:
            def updated(self, props):
                received.append(props)

        ca.register_managed("test.pid", MockManaged())

        # Push a config update
        ca.update("test.pid", {"key": "value"})
        assert len(received) == 1
        assert received[0] == {"key": "value"}

        # Delete config
        ca.delete("test.pid")
        assert len(received) == 2
        assert received[1] is None

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_managed_factory_receives_updates(self):
        """ConfigAdmin pushes config to managed service factories."""
        from reactpy.providers.configadmin import ConfigAdminProvider
        from reactpy.providers.config import ConfigProvider

        kernel = Kernel()
        kernel.discover([ConfigProvider, ConfigAdminProvider])
        await kernel.boot()

        ca = kernel.registry.require("IConfigAdmin")

        events = []

        class MockFactory:
            def updated(self, pid, props):
                events.append(("updated", pid, props))

            def deleted(self, pid):
                events.append(("deleted", pid))

        ca.register_factory("sms.sender", MockFactory())

        # Factory config uses factory PID as prefix
        ca.update("sms.sender.instance1", {"phone": "+1234"})
        assert len(events) == 1
        assert events[0] == ("updated", "sms.sender.instance1", {"phone": "+1234"})

        ca.delete("sms.sender.instance1")
        assert len(events) == 2
        assert events[1] == ("deleted", "sms.sender.instance1")

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_configadmin_bus_runnables(self):
        """ConfigAdmin runnables are accessible via the bus."""
        from reactpy.providers.configadmin import ConfigAdminProvider
        from reactpy.providers.config import ConfigProvider

        kernel = Kernel()
        kernel.discover([ConfigProvider, ConfigAdminProvider])
        await kernel.boot()

        # Update via bus
        await kernel.bus.invoke("configadmin.update", {
            "pid": "test.pid",
            "properties": {"a": 1},
        })

        # Get via bus
        result = await kernel.bus.invoke("configadmin.get", {"pid": "test.pid"})
        assert result == {"a": 1}

        # Delete via bus
        await kernel.bus.invoke("configadmin.delete", {"pid": "test.pid"})
        result = await kernel.bus.invoke("configadmin.get", {"pid": "test.pid"})
        assert result == {}

        await kernel.shutdown()


class TestErroneousRetry:
    @pytest.mark.asyncio
    async def test_failed_activation_enters_errored_state(self):
        """A component that throws during activation enters ERRORED state."""
        @component("failing", version="1.0")
        class FailingComponent:
            fail = True

            @lifecycle.activate
            def activate(self):
                if self.__class__.fail:
                    raise RuntimeError("Activation failed!")

        kernel = Kernel()
        kernel.discover([FailingComponent])

        # Boot catches the error, logs it, and continues (component is ERRORED)
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("failing")
        assert ci.state == State.ERRORED
        assert ci.error is not None

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_retry_erroneous_recovers(self):
        """retry_erroneous re-attempts activation and recovers."""
        attempt = [0]

        @component("retry-me", version="1.0")
        class RetryComponent:
            @lifecycle.activate
            def activate(self):
                attempt[0] += 1
                if attempt[0] < 2:
                    raise RuntimeError("Not ready yet")
                self.recovered = True

        kernel = Kernel()
        kernel.discover([RetryComponent])

        try:
            await kernel.boot()
        except RuntimeError:
            pass

        ci = kernel.lifecycle.get_instance("retry-me")
        assert ci.state == State.ERRORED

        # Retry — this time activation should succeed
        ci = await kernel.retry_erroneous("retry-me")
        assert ci.state == State.ACTIVE
        assert ci.instance.recovered is True

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_retry_non_errored_raises(self):
        """Retrying a non-ERRORED component raises."""
        @component("healthy-comp", version="1.0")
        class HealthyComp:
            pass

        kernel = Kernel()
        kernel.discover([HealthyComp])
        await kernel.boot()

        with pytest.raises(RuntimeError, match="not ERRORED"):
            await kernel.retry_erroneous("healthy-comp")

        await kernel.shutdown()


class TestAuthEnforcement:
    @pytest.mark.asyncio
    async def test_requires_action_blocks_without_token(self):
        """A runnable with requires_action blocks calls without a token."""
        from reactpy.providers.auth import AuthProvider
        from reactpy.providers.config import ConfigProvider

        @component("protected-app", version="1.0")
        class ProtectedApp:
            @runnable("admin_op", params=BaseModel, description="Admin only",
                      requires_action="admin.delete")
            async def admin_op(self, params):
                return {"deleted": True}

        kernel = Kernel()
        kernel.discover([ConfigProvider, AuthProvider, ProtectedApp])

        # Configure auth with a valid token
        config = kernel.lifecycle.get_instance("config")
        # We need to set config before boot
        kernel._policies = {}

        await kernel.boot()

        # Set up auth config manually on the auth instance
        auth_ci = kernel.lifecycle.get_instance("auth")
        auth_ci.instance._enabled = True
        auth_ci.instance._tokens = {
            "admin-token": {"identity": "admin-user", "roles": ["admin"]},
            "reader-token": {"identity": "reader-user", "roles": ["reader"]},
        }
        auth_ci.instance._policies = {
            "admin": ["*"],
            "reader": ["read"],
        }

        # Call without token → PermissionError
        with pytest.raises(PermissionError, match="requires authentication"):
            await kernel.bus.invoke("protected-app.admin_op", {})

        # Call with valid admin token → success
        result = await kernel.bus.invoke("protected-app.admin_op", {
            "__auth_token__": "admin-token",
        })
        assert result == {"deleted": True}

        # Call with reader token (wrong role/action) → PermissionError
        with pytest.raises(PermissionError, match="Not authorized"):
            await kernel.bus.invoke("protected-app.admin_op", {
                "__auth_token__": "reader-token",
            })

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_requires_role_checks_role(self):
        """A runnable with requires_role checks the caller's role."""
        from reactpy.providers.auth import AuthProvider
        from reactpy.providers.config import ConfigProvider

        @component("role-app", version="1.0")
        class RoleApp:
            @runnable("superuser_op", params=BaseModel, description="Superuser only",
                      requires_role="superuser")
            async def superuser_op(self, params):
                return {"super": True}

        kernel = Kernel()
        kernel.discover([ConfigProvider, AuthProvider, RoleApp])
        await kernel.boot()

        # Set up auth
        auth_ci = kernel.lifecycle.get_instance("auth")
        auth_ci.instance._enabled = True
        auth_ci.instance._tokens = {
            "su-token": {"identity": "su", "roles": ["superuser"]},
            "normal-token": {"identity": "normal", "roles": ["user"]},
        }

        # Superuser token → success
        result = await kernel.bus.invoke("role-app.superuser_op", {
            "__auth_token__": "su-token",
        })
        assert result == {"super": True}

        # Normal token → PermissionError
        with pytest.raises(PermissionError, match="Role.*required"):
            await kernel.bus.invoke("role-app.superuser_op", {
                "__auth_token__": "normal-token",
            })

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_no_auth_service_allows_all(self):
        """Without an IAuth provider, auth-protected runnables still work
        (no enforcement possible)."""
        @component("unprotected-app", version="1.0")
        class UnprotectedApp:
            @runnable("op", params=BaseModel, description="No auth svc",
                      requires_action="some.action")
            async def op(self, params):
                return {"ok": True}

        kernel = Kernel()
        kernel.discover([UnprotectedApp])
        await kernel.boot()

        # No IAuth provider → requires_action is a no-op, but we still
        # need a token (the check happens before looking for IAuth)
        # Actually, the check looks for IAuth first — if not found, no enforcement
        result = await kernel.bus.invoke("unprotected-app.op", {})
        assert result == {"ok": True}

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════════
# Integration: spell-checker end-to-end (like the iPOPO tutorial)
# ══════════════════════════════════════════════════════════════════════


class TestSpellCheckerE2E:
    """Full spell-checker tutorial ported from iPOPO to our microkernel."""

    @pytest.mark.asyncio
    async def test_full_spell_checker_flow(self):
        """
        1. Boot with EN + FR dictionaries + spell checker
        2. Check English text → correct words pass, misspelled caught
        3. Check French text → same
        4. Hot-add a German dict → bind fires, German available
        5. Hot-remove French dict → unbind fires, French unavailable
        """
        kernel = Kernel()
        kernel.discover([EnglishDict, FrenchDict, SpellCheckerAggregate])
        await kernel.boot()

        # English check
        result = await kernel.bus.invoke("spell-checker.check", {
            "text": "hello world",
            "language": "EN",
        })
        assert result["misspelled"] == []

        result = await kernel.bus.invoke("spell-checker.check", {
            "text": "hello xyz",
            "language": "EN",
        })
        assert result["misspelled"] == ["xyz"]

        # French check
        result = await kernel.bus.invoke("spell-checker.check", {
            "text": "bonjour le monde",
            "language": "FR",
        })
        assert result["misspelled"] == []

        # Hot-add German
        @component("dict-de", version="1.0")
        @provides("IDictionary")
        @prop("_language", "language", "DE")
        class GermanDict:
            @lifecycle.activate
            def activate(self):
                self.words = {"hallo", "welt"}
            def check_word(self, word):
                return word.lower() in self.words

        await kernel.hot_add(GermanDict)
        sc = kernel.lifecycle.get_instance("spell-checker").instance
        assert "DE" in sc.languages

        result = await kernel.bus.invoke("spell-checker.check", {
            "text": "hallo welt",
            "language": "DE",
        })
        assert result["misspelled"] == []

        # Hot-remove French
        await kernel.hot_remove("dict-fr")
        assert "FR" not in sc.languages

        result = await kernel.bus.invoke("spell-checker.check", {
            "text": "bonjour",
            "language": "FR",
        })
        assert "error" in result  # No dictionary for FR

        await kernel.shutdown()
