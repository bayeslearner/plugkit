"""LoggingProvider — structured logging as a component.

Provides: ILogger
Requires: nothing (leaf provider)

Every component gets a logger pre-bound with its identity:
  {component: "splunk", factory: "splunk-app", timestamp: ...}
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from reactpy.kernel import component, provides, lifecycle


class ComponentLogger:
    """Structured logger scoped to a component."""

    def __init__(self, component_name: str, factory_name: str, level: int = logging.INFO):
        self._component = component_name
        self._factory = factory_name
        self._level = level
        self._logger = logging.getLogger(f"kernel.{component_name}")

    def _log(self, level: int, msg: str, **kwargs: Any) -> None:
        if level < self._level:
            return
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": logging.getLevelName(level),
            "component": self._component,
            "msg": msg,
            **kwargs,
        }
        self._logger.log(level, json.dumps(entry, default=str))

    def info(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, **kwargs)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, **kwargs)


@component("logging", version="0.1")
@provides("ILogger")
class LoggingProvider:
    """Provides scoped loggers to components.

    The kernel calls get_logger(component_name, factory_name)
    during runtime building to scope the logger.
    """

    @lifecycle.activate
    def activate(self, rt):
        level_name = rt.properties.get("log_level", "INFO")
        self._level = getattr(logging, level_name.upper(), logging.INFO)

        # Configure root handler for structured output
        root = logging.getLogger("kernel")
        if not root.handlers:
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter("%(message)s"))
            root.addHandler(handler)
        root.setLevel(self._level)

    def get_logger(self, component_name: str, factory_name: str = "") -> ComponentLogger:
        """Create a scoped logger for a component."""
        return ComponentLogger(component_name, factory_name, self._level)

    # ILogger interface (for direct use as a generic logger)
    def info(self, msg: str, **kwargs: Any) -> None:
        ComponentLogger("kernel", "kernel", self._level).info(msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        ComponentLogger("kernel", "kernel", self._level).warning(msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        ComponentLogger("kernel", "kernel", self._level).error(msg, **kwargs)

    def debug(self, msg: str, **kwargs: Any) -> None:
        ComponentLogger("kernel", "kernel", self._level).debug(msg, **kwargs)
