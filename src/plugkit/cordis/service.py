"""Service — the base class for provided services."""

from __future__ import annotations

import copy
from typing import Any, Optional

from .utils import symbols


class Service:
    """Base class for cordis services.

    Subclasses override `__cordis_init__` (JS `[Service.init]`),
    `__cordis_invoke__` (JS `[Service.invoke]`), `__cordis_check__`
    (JS `[Service.check]`) and `__cordis_extend__` (JS `[Service.extend]`).
    """

    init = symbols.init
    check = symbols.check
    config = symbols.config
    invoke = symbols.invoke
    extend = symbols.extend
    tracker = symbols.tracker
    resolveConfig = symbols.resolveConfig

    def __init__(self, ctx, name: Optional[str] = None):
        if name is None:
            name = getattr(type(self), "provide", None)
        object.__setattr__(self, symbols.tracker, {"associate": name, "property": "ctx"})
        self.ctx = ctx
        self.name = name
        ctx.reflect.provide(name, self, getattr(type(self), symbols.check, None))

    def _filter(self, ctx) -> bool:
        return ctx._effective_isolate().get(self.name) is self.ctx._effective_isolate().get(self.name)

    def __cordis_resolve_config__(self, base=None, head=None):
        name = self.name
        configs = []

        chain = []
        obj = self.ctx
        while obj is not None:
            chain.append(obj)
            obj = obj.__dict__.get("_parent")
        for obj in reversed(chain):
            intercept = obj.__dict__.get("_intercept")
            if intercept is not None and name in intercept:
                configs.append(intercept[name])

        if base is not None:
            configs.insert(0, base)
        if head is not None:
            configs.append(head)

        config_cls = getattr(self, "Config", None)
        merge = getattr(config_cls, "merge", None) if config_cls is not None else None
        if merge is not None:
            return merge(*configs)
        merged = {}
        for config in configs:
            if config:
                merged.update(config)
        return merged

    def __cordis_extend__(self, props=None):
        value = self._cordis_value if hasattr(self, "_cordis_value") else self
        extended = copy.copy(value)
        if props:
            extended.__dict__.update(props)
        if getattr(extended, symbols.invoke, None) is not None:
            from .traceable import _ServiceCallable

            return _ServiceCallable(self._cordis_ctx, extended, self._cordis_tracker)
        return extended
