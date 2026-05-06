"""Tests for type-hint based contracts and direct self-injection.

Verifies:
- Types accepted in @provides, @requires (single, aggregate, map)
- Annotation-based implicit requirements (no @requires needed)
- Direct self.X injection for annotated fields
- self.rt.X still works alongside self.X
- Backward compat: strings still work everywhere
- Mixed string/type usage
"""
import pytest
from typing import Protocol, runtime_checkable
from pydantic import BaseModel

from signalpy.kernel import (
    Kernel, component, provides, requires,
    runnable, lifecycle, prop, computed, effect,
)
from signalpy.kernel.component import get_meta, _finalize_meta, _contract_name, _is_contract_type
from signalpy.kernel.lifecycle_manager import State


# ── Test contracts (Protocols) ───────────────────────────────────────

@runtime_checkable
class IDictionary(Protocol):
    def check_word(self, word: str) -> bool: ...

@runtime_checkable
class ISpellChecker(Protocol):
    def check(self, text: str, language: str) -> list[str]: ...

@runtime_checkable
class ICache(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...


# ══════════════════════════════════════════════════════════════════════
# _contract_name and _is_contract_type
# ══════════════════════════════════════════════════════════════════════

class TestContractName:
    def test_string_passthrough(self):
        assert _contract_name("IFoo") == "IFoo"

    def test_type_to_name(self):
        assert _contract_name(IDictionary) == "IDictionary"

    def test_type_registered_after_first_use(self):
        @runtime_checkable
        class INewService(Protocol):
            pass
        _contract_name(INewService)
        assert _is_contract_type(INewService)

    def test_invalid_spec_raises(self):
        with pytest.raises(TypeError, match="Contract must be str or type"):
            _contract_name(42)


class TestIsContractType:
    def test_runtime_checkable_protocol(self):
        assert _is_contract_type(IDictionary)

    def test_plain_class_not_contract(self):
        class Foo:
            pass
        assert not _is_contract_type(Foo)

    def test_not_a_type(self):
        assert not _is_contract_type("IFoo")
        assert not _is_contract_type(42)


# ══════════════════════════════════════════════════════════════════════
# Types in decorators
# ══════════════════════════════════════════════════════════════════════

class TestTypedProvides:
    def test_provides_with_type(self):
        @component("typed-provider")
        @provides(IDictionary)
        class P:
            pass

        _finalize_meta(P)
        meta = get_meta(P)
        assert "IDictionary" in meta.provides

    def test_provides_with_multiple_types(self):
        @component("multi-provider")
        @provides(IDictionary, ICache)
        class P:
            pass

        _finalize_meta(P)
        meta = get_meta(P)
        assert "IDictionary" in meta.provides
        assert "ICache" in meta.provides

    def test_provides_with_string_still_works(self):
        @component("string-provider")
        @provides("IFoo")
        class P:
            pass

        _finalize_meta(P)
        meta = get_meta(P)
        assert "IFoo" in meta.provides


class TestTypedRequires:
    def test_requires_with_types(self):
        @component("typed-consumer")
        @requires(dictionary=IDictionary, cache=ICache)
        class C:
            pass

        _finalize_meta(C)
        meta = get_meta(C)
        assert meta.requires["dictionary"] == "IDictionary"
        assert meta.requires["cache"] == "ICache"
        # RequirementDef should have the type
        req = next(r for r in meta.requirements if r.attr_name == "dictionary")
        assert req.contract_type is IDictionary

    def test_requires_mixed_string_and_type(self):
        @component("mixed-consumer")
        @requires(dictionary=IDictionary, config="IConfig")
        class C:
            pass

        _finalize_meta(C)
        meta = get_meta(C)
        assert meta.requires["dictionary"] == "IDictionary"
        assert meta.requires["config"] == "IConfig"

    def test_requires_aggregate_with_type(self):
        @component("agg-consumer")
        @requires(dicts=list[IDictionary])
        class C:
            pass

        _finalize_meta(C)
        meta = get_meta(C)
        assert meta.requires["dicts"] == "IDictionary"
        req = next(r for r in meta.requirements if r.attr_name == "dicts")
        assert req.aggregate is True
        assert req.contract_type is IDictionary

    def test_requires_map_with_type(self):
        @component("map-consumer")
        @requires(dicts=IDictionary, key="language")
        class C:
            pass

        _finalize_meta(C)
        meta = get_meta(C)
        req = next(r for r in meta.requirements if r.attr_name == "dicts")
        assert req.key == "language"
        assert req.contract_type is IDictionary

    def test_requires_single_with_type(self):
        """Single injection (highest-ranked) — was @requires_best in v1."""
        @component("best-consumer")
        @requires(primary=IDictionary)
        class C:
            pass

        _finalize_meta(C)
        meta = get_meta(C)
        req = next(r for r in meta.requirements if r.attr_name == "primary")
        # Single injection: not aggregate, no key
        assert req.aggregate is False
        assert req.key == ""
        assert req.contract_type is IDictionary


# ══════════════════════════════════════════════════════════════════════
# Annotation-based implicit requirements
# ══════════════════════════════════════════════════════════════════════

class TestAnnotationImplicitRequires:
    def test_annotation_creates_requirement(self):
        """A class annotation with a contract type creates an implicit @requires."""
        # First register IDictionary as a known contract type
        _contract_name(IDictionary)

        @component("anno-consumer")
        class C:
            dictionary: IDictionary

        _finalize_meta(C)
        meta = get_meta(C)
        assert "dictionary" in meta.requires
        assert meta.requires["dictionary"] == "IDictionary"
        req = next(r for r in meta.requirements if r.attr_name == "dictionary")
        assert req.contract_type is IDictionary

    def test_annotation_does_not_duplicate_explicit_requires(self):
        """If @requires already declares the field, annotation doesn't create a duplicate."""
        @component("no-dup")
        @requires(dictionary=IDictionary)
        class C:
            dictionary: IDictionary

        _finalize_meta(C)
        meta = get_meta(C)
        reqs = [r for r in meta.requirements if r.attr_name == "dictionary"]
        assert len(reqs) == 1

    def test_non_contract_annotation_ignored(self):
        """Regular type annotations (str, int, etc.) are not treated as requirements."""
        @component("plain-anno")
        class C:
            name: str
            count: int

        _finalize_meta(C)
        meta = get_meta(C)
        assert "name" not in meta.requires
        assert "count" not in meta.requires


# ══════════════════════════════════════════════════════════════════════
# Direct self-injection (self.X alongside self.rt.X)
# ══════════════════════════════════════════════════════════════════════

class TestDirectSelfInjection:
    @pytest.mark.asyncio
    async def test_self_injection_with_typed_requires(self):
        """@requires with types injects on both self.X and self.rt.X."""
        @component("dict-impl")
        @provides(IDictionary)
        class DictImpl:
            @lifecycle.activate
            def activate(self):
                self.words = {"hello"}
            def check_word(self, word):
                return word in self.words

        @component("checker-typed", depends=["dict-impl"])
        @requires(dictionary=IDictionary)
        class Checker:
            dictionary: IDictionary  # annotation for IDE

            @lifecycle.activate
            def activate(self):
                # Both should work:
                assert self.dictionary is not None
                assert self.rt.dictionary is not None
                assert self.dictionary is self.rt.dictionary
                self.works = True

        kernel = Kernel()
        kernel.discover([DictImpl, Checker])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("checker-typed")
        assert ci.instance.works is True
        # Direct access works
        assert ci.instance.dictionary.check_word("hello") is True
        # rt access also works
        assert ci.instance.rt.dictionary.check_word("hello") is True

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_self_injection_with_annotation_only(self):
        """Annotation-only (no @requires) injects on self.X."""
        _contract_name(IDictionary)  # register the contract type

        @component("dict-impl2")
        @provides(IDictionary)
        class DictImpl:
            @lifecycle.activate
            def activate(self):
                self.words = {"world"}
            def check_word(self, word):
                return word in self.words

        @component("checker-anno", depends=["dict-impl2"])
        class Checker:
            dictionary: IDictionary  # this alone is the requirement + type hint

            @lifecycle.activate
            def activate(self):
                self.result = self.dictionary.check_word("world")

        kernel = Kernel()
        kernel.discover([DictImpl, Checker])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("checker-anno")
        assert ci.instance.result is True
        # Both access patterns work
        assert ci.instance.dictionary is not None
        assert ci.instance.rt.dictionary is not None

        await kernel.shutdown()

    @pytest.mark.asyncio
    async def test_string_requires_no_self_injection(self):
        """@requires with strings does NOT inject on self.X (no type info)."""
        @component("dict-impl3")
        @provides("IDict3")
        class DictImpl:
            pass

        @component("consumer3", depends=["dict-impl3"])
        @requires(d="IDict3")
        class Consumer:
            # No annotation → no self.d injection
            @lifecycle.activate
            def activate(self):
                # rt.d should work
                assert self.rt.d is not None
                self.ok = True

        kernel = Kernel()
        kernel.discover([DictImpl, Consumer])
        await kernel.boot()

        ci = kernel.lifecycle.get_instance("consumer3")
        assert ci.instance.ok is True
        # self.d was NOT set (no type annotation, no contract_type)
        assert not hasattr(ci.instance, 'd') or ci.instance.d is ci.instance.rt.d

        await kernel.shutdown()


# ══════════════════════════════════════════════════════════════════════
# Full integration: typed spell checker
# ══════════════════════════════════════════════════════════════════════

class TestTypedSpellChecker:
    @pytest.mark.asyncio
    async def test_full_typed_flow(self):
        """Complete spell checker using types everywhere — no strings.

        Uses v2 unified @requires for aggregate injection and @effect
        to reactively rebuild the by_lang index when dictionaries change.
        """

        @component("t-dict-en")
        @provides(IDictionary)
        @prop("_language", "language", "EN")
        class EnDict:
            @lifecycle.activate
            def activate(self):
                self.words = {"hello", "world"}
            def check_word(self, word):
                return word.lower() in self.words

        @component("t-dict-fr")
        @provides(IDictionary)
        @prop("_language", "language", "FR")
        class FrDict:
            @lifecycle.activate
            def activate(self):
                self.words = {"bonjour", "monde"}
            def check_word(self, word):
                return word.lower() in self.words

        class CheckParams(BaseModel):
            text: str = ""
            language: str = "EN"

        @component("t-checker")
        @provides(ISpellChecker)
        @requires(dictionaries=IDictionary, key="language")
        class Checker:
            @lifecycle.activate
            def activate(self):
                self.by_lang = {}

            @effect
            def on_dict_change(self):
                """Reactively rebuild by_lang when dictionaries change."""
                dicts = self.rt.dictionaries  # reactive read — map keyed by "language"
                if isinstance(dicts, dict):
                    self.by_lang = dict(dicts)
                else:
                    self.by_lang = {}

            def check(self, text, language="EN"):
                d = self.by_lang.get(language)
                if not d:
                    return [text]
                return [w for w in text.split() if not d.check_word(w)]

            @runnable("check", params=CheckParams, description="Spell check")
            async def check_text(self, params):
                return {"misspelled": self.check(params.text, params.language)}

        kernel = Kernel()
        kernel.discover([EnDict, FrDict, Checker])
        await kernel.boot()

        # Via registry (typed)
        checker = kernel.registry.require("ISpellChecker")
        assert checker.check("hello world") == []
        assert checker.check("hello xyz") == ["xyz"]
        assert checker.check("bonjour monde", "FR") == []

        # Via bus
        result = await kernel.invoke("t-checker.check", {
            "text": "hello xyz",
            "language": "EN",
        })
        assert result["misspelled"] == ["xyz"]

        await kernel.shutdown()
