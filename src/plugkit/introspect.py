"""The system describes itself.

A running application knows which plugins are mounted, what each provides, what
each is waiting for, and why one failed. None of it was reachable.

    from plugkit import describe, format_tree

    print(format_tree(describe(root)))

    plugkit — 4 fibers, 3 services
    ├─ [1] database          ACTIVE    provides database
    ├─ [2] greeter           PENDING   waiting for: cache
    ├─ [3] http              ACTIVE    provides server
    └─ [4] admin             FAILED    ConnectionRefusedError: [Errno 61]

`describe` is a function rather than a service on purpose. You need to inspect a
system that did not plan to be inspected — a production process misbehaving right
now, or a test written three releases ago. A debugging facility you must remember
to mount is unavailable at the moment it is wanted.

Everything it returns is a string, number, list, dict or None, so a snapshot
survives `json.dumps`, a socket, or a log file. It holds uids rather than fibers,
so keeping one does not keep the system alive.
"""

from __future__ import annotations

import json
import time
from typing import Any

__all__ = ["describe", "format_tree", "DIAGNOSTICS"]

#: The point `describe` collects extra per-plugin facts from. A plugin knows
#: things the kernel cannot — a pool's size, a queue's depth, a token's expiry.
DIAGNOSTICS = "diagnostics"


def _state_name(fiber: Any) -> str:
    state = getattr(fiber, "state", None)
    return getattr(state, "name", str(state))


def _error_text(fiber: Any) -> str | None:
    error = getattr(fiber, "_error", None)
    if error is None:
        return None
    if isinstance(error, BaseException):
        return f"{type(error).__name__}: {error}"
    return str(error)


# `fiber.store` looks like the answer to "what does this fiber provide" and is
# not: it is the resolution cache `Context._default_get` walks, so it holds
# everything the fiber can *see*, including its parents' services. The
# authoritative record is `reflect.store`, where each `Impl` names its provider.


def _all_fibers(ctx: Any) -> list:
    """Every live fiber, ordered by mount sequence.

    A fiber whose `uid` is None has been disposed; the registry can still be
    holding it, and reporting it would describe a system that no longer exists.
    """
    seen: dict[int, Any] = {}
    for runtime in ctx.registry.values():
        for fiber in runtime.fibers:
            if getattr(fiber, "uid", None) is None:
                continue
            seen[fiber.uid] = fiber
    return [seen[uid] for uid in sorted(seen)]


def _reachable_services(ctx: Any) -> dict[str, int]:
    """Service name -> the uid of the fiber providing it."""
    services: dict[str, int] = {}
    for impl in getattr(ctx.reflect, "store", {}).values():
        name = getattr(impl, "name", None)
        fiber = getattr(impl, "fiber", None)
        uid = getattr(fiber, "uid", None)
        if isinstance(name, str) and uid is not None:
            services[name] = uid
    return services


def _serialisable(value: Any) -> Any:
    """`value` if it survives `json.dumps`, its `repr` otherwise.

    A diagnostic is arbitrary plugin code and may return anything — a connection
    object, a datetime, a set. Letting that through would break the snapshot's
    one structural promise for everyone, on one plugin's mistake. Same principle
    as catching the exception below: a broken diagnostic degrades to a string
    instead of breaking the tool you reached for because something was already
    wrong.
    """
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _collect_diagnostics(ctx: Any) -> dict[str, Any]:
    """Call every contribution to the `diagnostics` point.

    A contribution that raises is reported rather than propagated.
    """
    points = getattr(ctx, "points", None)
    if points is None:
        return {}
    try:
        entries = points.entries(DIAGNOSTICS)
    except Exception:  # points is not a PointsService, or is not active
        return {}

    out: dict[str, Any] = {}
    for index, entry in enumerate(entries):
        key = entry.key or f"anonymous-{index}"
        try:
            value = entry.value() if callable(entry.value) else entry.value
        except Exception as exc:
            value = {"error": f"{type(exc).__name__}: {exc}"}
        out[str(key)] = _serialisable(value)
    return out


def _points_summary(ctx: Any) -> dict[str, int]:
    points = getattr(ctx, "points", None)
    if points is None:
        return {}
    try:
        return {name: points.count(name) for name in points.names()}
    except Exception:
        return {}


def describe(ctx: Any) -> dict:
    """A plain, JSON-serialisable snapshot of the running system.

    Read-only: nothing here restarts, unloads or reconfigures anything, so it is
    safe from a signal handler or an HTTP endpoint.

    :param ctx: any context. The snapshot covers the whole tree it belongs to.
    :returns: ``{"fibers": [...], "services": {...}, "points": {...},
        "diagnostics": {...}, "taken_at": float}``
    """
    services = _reachable_services(ctx)
    provided_by: dict[int, list[str]] = {}
    for name, uid in services.items():
        provided_by.setdefault(uid, []).append(name)

    fibers = []
    for fiber in _all_fibers(ctx):
        injects = sorted(fiber.inject or ())
        parent = getattr(fiber, "parent", None)
        fibers.append(
            {
                "uid": fiber.uid,
                "name": fiber.name,
                "state": _state_name(fiber),
                "parent": getattr(parent, "uid", None) if parent is not None else None,
                "provides": sorted(provided_by.get(fiber.uid, ())),
                "injects": injects,
                # The most useful line in the snapshot: a plugin that silently
                # never ran, and the name of what it is waiting for.
                "missing": [name for name in injects if name not in services],
                "effects": [e.get("label", "anonymous") for e in fiber.get_effects()],
                "error": _error_text(fiber),
            }
        )

    return {
        "taken_at": time.time(),
        "fibers": fibers,
        "services": services,
        "points": _points_summary(ctx),
        "diagnostics": _collect_diagnostics(ctx),
    }


def format_tree(snapshot: dict) -> str:
    """Render a snapshot as a readable tree.

    Takes a snapshot rather than a context, so one captured earlier — or read
    back out of a log — renders the same as a live one.
    """
    fibers = snapshot.get("fibers", [])
    services = snapshot.get("services", {})
    lines = [f"plugkit — {len(fibers)} fibers, {len(services)} services"]

    width = max((len(f["name"]) for f in fibers), default=0)
    for index, fiber in enumerate(fibers):
        elbow = "└─" if index == len(fibers) - 1 else "├─"
        note = _note(fiber)
        lines.append(
            f"{elbow} [{fiber['uid']}] {fiber['name']:<{width}}  "
            f"{fiber['state']:<9} {note}".rstrip()
        )

    diagnostics = snapshot.get("diagnostics") or {}
    if diagnostics:
        lines.append("")
        lines.append("diagnostics")
        for key in sorted(diagnostics):
            lines.append(f"  {key}: {diagnostics[key]}")
    return "\n".join(lines)


def _note(fiber: dict) -> str:
    if fiber["error"]:
        return fiber["error"]
    if fiber["missing"]:
        return "waiting for: " + ", ".join(fiber["missing"])
    if fiber["provides"]:
        return "provides " + ", ".join(fiber["provides"])
    if fiber["state"] == "PENDING":
        # injected services all exist, so it is mid-activation rather than stuck
        return "starting"
    return ""
