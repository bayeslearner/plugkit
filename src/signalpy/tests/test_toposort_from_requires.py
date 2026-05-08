"""Toposort uses @requires contracts as well as explicit depends=.

Before this change, `lifecycle_manager.resolve_all()` only used
`meta.dependencies` (the @component(depends=[...]) list) for ordering.
A component with `@requires(gateway=IToolGateway)` but no
`depends=["tool-gateway"]` would activate alphabetically alongside its
provider — triggering "no provider found" warnings and relying on the
reactive registry listener to fix the wiring later.

Now the toposort also walks `meta.requirements`. Scalar non-aggregate
non-optional requirements imply ordering. `@requires` is the single
source of truth — `depends=` is an optional hint for cases where the
contract isn't enough (e.g. ordering without a contract dependency).
"""
import pytest

from signalpy.kernel import (
    Kernel, component, provides, requires, lifecycle, runnable,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@component("contract-provider", version="1.0")
@provides("IThing")
class ContractProvider:
    @lifecycle.activate
    def activate(self):
        self.activated = True


@component("contract-consumer", version="1.0")
@requires(thing="IThing")
class ContractConsumer:
    """No depends= clause — yet activates AFTER ContractProvider via @requires."""
    @lifecycle.activate
    def activate(self):
        # If toposort were broken, self.rt.thing wouldn't exist yet.
        assert self.rt.thing is not None


# Aggregate consumer must NOT block on its providers (cycle-tolerant).
@component("aggregate-host", version="1.0")
@requires(plugins="IThing")  # treated as scalar — not aggregate
class _AggregateHostScalar: ...


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_requires_drives_toposort_without_depends():
    """A consumer with @requires(X) activates after the X provider, even
    without an explicit depends= clause."""
    kernel = Kernel()
    kernel.discover([ContractConsumer, ContractProvider])  # reverse order
    await kernel.boot()

    boot = [b["name"] for b in kernel.boot_order()]
    assert boot.index("contract-provider") < boot.index("contract-consumer"), (
        f"Provider must boot before consumer; got: {boot}"
    )

    consumer_ci = kernel.lifecycle.get_instance("contract-consumer")
    assert consumer_ci.instance.rt.thing is not None
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_aggregate_requires_does_not_block_boot():
    """list[X] aggregate @requires must NOT add a toposort edge — the
    consumer boots empty and the registry listener fills the list as
    providers come online."""
    from typing import Protocol, runtime_checkable

    @runtime_checkable
    class IPlugin(Protocol): ...

    @component("agg-host")
    @requires(plugins=list[IPlugin])
    class AggHost:
        @lifecycle.activate
        def activate(self):
            # boot proceeds even if no plugins yet — list[]
            assert isinstance(self.rt.plugins, list)

    @component("plug-a")
    @provides(IPlugin)
    class PlugA: ...

    kernel = Kernel()
    kernel.discover([AggHost, PlugA])
    await kernel.boot()
    # Both activated; relative order doesn't matter for aggregate
    boot = [b["name"] for b in kernel.boot_order()]
    assert "agg-host" in boot
    assert "plug-a" in boot
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_optional_requires_does_not_block_boot():
    """`optional=True` @requires must not add a toposort edge — the
    consumer boots even if no provider exists."""
    @component("optional-consumer")
    @requires(thing="IUnseen", optional=True)
    class OptConsumer:
        @lifecycle.activate
        def activate(self):
            assert self.rt.thing is None

    kernel = Kernel()
    kernel.discover([OptConsumer])
    await kernel.boot()
    boot = [b["name"] for b in kernel.boot_order()]
    assert "optional-consumer" in boot
    await kernel.shutdown()


@pytest.mark.asyncio
async def test_depends_still_works_for_non_contract_ordering():
    """`depends=[factory]` still pins boot order even when no @requires
    references the dependency (e.g. lifecycle ordering, stub creation)."""
    @component("seed")
    class Seed:
        @lifecycle.activate
        def activate(self):
            pass

    @component("planted", depends=["seed"])
    class Planted:
        """Boots after Seed but doesn't @require any contract from it."""
        @lifecycle.activate
        def activate(self):
            pass

    kernel = Kernel()
    kernel.discover([Planted, Seed])
    await kernel.boot()
    boot = [b["name"] for b in kernel.boot_order()]
    assert boot.index("seed") < boot.index("planted")
    await kernel.shutdown()
