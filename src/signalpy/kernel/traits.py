"""Trait system — the core of Axis 1.

A trait is a cross-cutting concern that a component can have.
Traits are organized by level:

  L0  Kernel     — what makes something a component (always present)
  L1  Platform   — infrastructure (opt-in, kernel provides defaults)
  L2  App        — domain capabilities (apps define and contribute)
  L3  Instance   — per-instance parameterization (factory properties)

The TraitRegistry holds all known traits. New traits can be registered
at any level — the kernel doesn't hardcode the trait list.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Protocol, runtime_checkable


class Level(IntEnum):
    KERNEL = 0
    PLATFORM = 1
    APP = 2
    INSTANCE = 3


@dataclass(frozen=True)
class TraitDef:
    """Definition of a trait — lives in the TraitRegistry."""
    name: str
    level: Level
    contract: type | None = None          # Protocol/ABC that providers implement
    applicator: Callable | None = None    # fn(component, runtime) → apply the trait
    description: str = ""


class TraitRegistry:
    """Central registry of known traits.

    The kernel registers L0 traits at boot.  Platform components register
    L1 traits when they activate.  Apps can register L2 traits.
    """

    def __init__(self) -> None:
        self._traits: dict[str, TraitDef] = {}

    def define(
        self,
        name: str,
        level: Level,
        *,
        contract: type | None = None,
        applicator: Callable | None = None,
        description: str = "",
    ) -> TraitDef:
        """Register a new trait definition."""
        if name in self._traits:
            raise ValueError(f"Trait {name!r} already defined")
        td = TraitDef(
            name=name, level=level, contract=contract,
            applicator=applicator, description=description,
        )
        self._traits[name] = td
        return td

    def get(self, name: str) -> TraitDef:
        try:
            return self._traits[name]
        except KeyError:
            raise KeyError(f"Unknown trait {name!r}") from None

    def by_level(self, level: Level) -> list[TraitDef]:
        return sorted(
            (t for t in self._traits.values() if t.level == level),
            key=lambda t: t.name,
        )

    def all(self) -> list[TraitDef]:
        return sorted(self._traits.values(), key=lambda t: (t.level, t.name))

    def compute(self, meta) -> list[str]:
        """Compute which traits a component has from its ComponentMeta.

        This is the bridge between decorators and the trait system.
        Instead of requiring components to declare traits explicitly,
        the registry infers them from what the component actually does.
        """
        traits = []

        # L0 — every component has these
        traits.extend([IDENTIFIABLE, LIFECYCLE, DEPENDABLE, REGISTRABLE, FACTORYABLE])
        if meta.health_fn:
            traits.append(INSPECTABLE)

        # L1 — inferred from @requires
        contracts = set(meta.requires.values())
        if "IConfig" in contracts:
            traits.append(CONFIGURABLE)
        if "ILogger" in contracts or "ITracer" in contracts:
            traits.append(OBSERVABLE)
        if "ICredentials" in contracts or "IAuth" in contracts:
            traits.append(SECURED)
        if "IStorage" in contracts:
            traits.append(STORABLE)
        # Communicable — every component with runnables or subscriptions uses the bus
        if meta.runnables or meta.subscriptions:
            traits.append(COMMUNICABLE)

        # L2 — inferred from what the component contributes
        if meta.runnables:
            traits.append(RUNNABLE)
        if meta.subscriptions:
            traits.append(SUBSCRIBABLE)
        if meta.kinds:
            traits.append(KINDED)
        if meta.skills:
            traits.append(SKILLFUL)
        if meta.apis:
            traits.append(ROUTABLE)

        if hasattr(meta, "computed_defs") and (meta.computed_defs or getattr(meta, "effect_defs", [])):
            traits.append(REACTIVE)

        # L3 — inferred from properties/metadata
        if meta.version and meta.version != "0.0.0":
            traits.append(VERSIONED)
        if meta.property_defs:
            traits.append(TARGETED)
        if "ICredentials" in contracts or "IStorage" in contracts:
            traits.append(SCOPED)

        return traits

    def __contains__(self, name: str) -> bool:
        return name in self._traits

    def __len__(self) -> int:
        return len(self._traits)


# ── Built-in L0 trait names (registered by Kernel at boot) ──────────

IDENTIFIABLE = "identifiable"
LIFECYCLE = "lifecycle"
DEPENDABLE = "dependable"
REGISTRABLE = "registrable"
INSPECTABLE = "inspectable"
FACTORYABLE = "factoryable"

# ── L1 trait names (registered by platform components) ──────────────

OBSERVABLE = "observable"
CONFIGURABLE = "configurable"
SECURED = "secured"
STORABLE = "storable"
COMMUNICABLE = "communicable"

# ── L2 trait names (registered by apps) ─────────────────────────────

RUNNABLE = "runnable"
SKILLFUL = "skillful"
KINDED = "kinded"
ROUTABLE = "routable"
SUBSCRIBABLE = "subscribable"
ADAPTABLE = "adaptable"
REACTIVE = "reactive"    # has @computed or @effect

# ── L3 trait names ──────────────────────────────────────────────────

TARGETED = "targeted"
SCOPED = "scoped"
VERSIONED = "versioned"
