"""Console logger exporter — port of `@cordisjs/plugin-logger-console`."""

from __future__ import annotations

from typing import Any, Optional

from .logger import Logger
from .timer import format_ms, get_clock


def js_inspect(value: Any, exporter=None, message=None, seen: Optional[set] = None) -> str:
    """A minimal `node:util.inspect`-style formatter (compact, quoted keys
    omitted, single-quoted strings)."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(value, (int, float)):
        return repr(value)
    if seen is None:
        seen = set()
    if id(value) in seen:
        return "[Circular]"
    seen.add(id(value))
    try:
        if isinstance(value, dict):
            inner = ", ".join(f"{js_inspect(k, None, None, seen)}: {js_inspect(v, None, None, seen)}" for k, v in value.items())
            return "{ " + inner + " }"
        if isinstance(value, (list, tuple)):
            inner = ", ".join(js_inspect(item, None, None, seen) for item in value)
            return "[ " + inner + " ]"
        if isinstance(value, BaseException):
            return value.__class__.__name__ + ": " + str(value)
        return repr(value)
    finally:
        seen.discard(id(value))


class ConsoleExporter:
    """Node-style console exporter (the `index.ts` variant)."""

    name = "logger-console"

    def __init__(self, ctx, config: Optional[dict] = None):
        config = dict(config or {})
        self.ctx = ctx
        self.colors = config.get("colors", False)
        self.maxLength = config.get("maxLength")
        self.levels = config.get("levels")
        self.showDiff = config.get("showDiff", False)
        self.showTime = config.get("showTime", "yyyy-MM-dd hh:mm:ss ")
        self.label = config.get("label")
        self.formatters = {"o": js_inspect, "O": js_inspect}
        self.timestamp = get_clock().now()
        ctx.logger.exporter(self)

    def export(self, message):
        print(self.render(message))

    def render(self, message) -> str:
        label_config = self.label or {}
        prefix = f"[{message.type[0].upper()}]"
        space = " " * (label_config.get("margin", 1))
        indent = 3 + len(space)
        output = ""
        if self.showTime:
            indent += len(self.showTime)
            output += Logger.color(self, 8, _format_time(self.showTime, message.ts))
        code = Logger.code(message.name, self.colors)
        label = Logger.color(self, code, message.name, ";1")
        width = label_config.get("width", 0)
        pad_length = width + len(label) - len(message.name)
        if label_config.get("align") == "right":
            output += label.rjust(pad_length) + space + prefix + space
            indent += width + len(space)
        else:
            output += prefix + space + label.ljust(pad_length) + space
        output += Logger.format(self, message).replace("\n", "\n" + " " * indent)
        if self.showDiff and self.timestamp is not None:
            diff = message.ts - self.timestamp
            output += Logger.color(self, code, " +" + format_ms(diff))
        self.timestamp = message.ts
        return output


def _format_time(template: str, ts: int) -> str:
    """Minimal `yyyy-MM-dd hh:mm:ss` formatter (cosmokit `Time.template` subset)."""
    import datetime

    dt = datetime.datetime.fromtimestamp(ts / 1000)
    result = (
        template.replace("yyyy", f"{dt.year:04d}")
        .replace("MM", f"{dt.month:02d}")
        .replace("dd", f"{dt.day:02d}")
        .replace("hh", f"{dt.hour:02d}")
        .replace("mm", f"{dt.minute:02d}")
        .replace("ss", f"{dt.second:02d}")
    )
    return result
