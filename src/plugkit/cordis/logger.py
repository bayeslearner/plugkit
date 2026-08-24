"""Logger service and exporters."""

from __future__ import annotations

import datetime
import json
import re
import traceback
from typing import Any, Optional

from .utils import hyphenate, symbols

LoggerLevel = {"ERROR": 0, "WARN": 1, "INFO": 2, "DEBUG": 3}


def _now_ms() -> int:
    """Message timestamp — uses the timer clock so fake clocks in tests
    control it (JS `Date.now()` is likewise patchable)."""
    try:
        from .timer import get_clock

        return int(get_clock().now())
    except Exception:  # pragma: no cover
        return int(datetime.datetime.now().timestamp() * 1000)


def _get(exporter, key, default=None):
    """Exporter configs may be plain dicts (JS-style) or objects."""
    if isinstance(exporter, dict):
        return exporter.get(key, default)
    return getattr(exporter, key, default)

c16 = [6, 2, 3, 4, 5, 1]
c256 = [
    20, 21, 26, 27, 32, 33, 38, 39, 40, 41, 42, 43, 44, 45, 56, 57, 62,
    63, 68, 69, 74, 75, 76, 77, 78, 79, 80, 81, 92, 93, 98, 99, 112, 113,
    129, 134, 135, 148, 149, 160, 161, 162, 163, 164, 165, 166, 167, 168,
    169, 170, 171, 172, 173, 178, 179, 184, 185, 196, 197, 198, 199, 200,
    201, 202, 203, 204, 205, 206, 207, 208, 209, 214, 215, 220, 221,
]


class Message:
    __slots__ = ("sn", "ts", "type", "level", "name", "args", "fiber")

    def __init__(self, sn: int, ts: int, type_: str, level: int, name: str, args: list, fiber=None):
        self.sn = sn
        self.ts = ts
        self.type = type_
        self.level = level
        self.name = name
        self.args = args
        self.fiber = fiber


default_formatters = {
    "s": lambda value, exporter, message: str(value),
    "d": lambda value, exporter, message: int(value),
    "i": lambda value, exporter, message: int(value),
    "f": lambda value, exporter, message: float(value),
    "o": lambda value, exporter, message: json.dumps(value, default=repr),
    "O": lambda value, exporter, message: json.dumps(value, default=repr),
    "c": lambda value, exporter, message: "",
    "C": lambda value, exporter, message: Logger.color(exporter, Logger.code(message.name, exporter.colors), value),
}


def _format_time(template: str, ts: Optional[int] = None) -> str:
    """Minimal `yyyy-MM-dd hh:mm:ss` formatter (Time.template subset)."""
    dt = datetime.datetime.fromtimestamp((ts if ts is not None else datetime.datetime.now().timestamp()) / 1000)
    result = template.replace("yyyy", f"{dt.year:04d}").replace("MM", f"{dt.month:02d}")
    result = result.replace("dd", f"{dt.day:02d}").replace("hh", f"{dt.hour:02d}")
    result = result.replace("mm", f"{dt.minute:02d}").replace("ss", f"{dt.second:02d}")
    return result


def _format_diff(ms: int) -> str:
    sign = "+" if ms >= 0 else "-"
    ms = abs(ms)
    total_seconds = ms // 1000
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return sign + " ".join(parts) if parts else sign + "0s"


class Logger:
    @staticmethod
    def color(exporter, code, value, decoration: str = ""):
        if not _get(exporter, "colors"):
            return "" + str(value)
        color_code = f"3{code}" if code < 8 else f"38;5;{code}"
        bold = decoration if exporter.colors >= 2 else ""
        return f"\u001b[{color_code}{bold}m{value}\u001b[0m"

    @staticmethod
    def code(name: str, level=None):
        hash_value = 0
        for char in name:
            hash_value = ((hash_value << 3) - hash_value) + ord(char) + 13
            hash_value &= 0xFFFFFFFF
            if hash_value >= 2**31:
                hash_value -= 2**32
        if not level:
            colors = []
        elif level >= 2:
            colors = c256
        else:
            colors = c16
        return colors[abs(hash_value) % len(colors)] if colors else 0

    @staticmethod
    def format(exporter, message: Message) -> str:
        args = list(message.args)
        if args and isinstance(args[0], BaseException):
            tb = args[0].__traceback__
            if tb is not None:
                args[0] = "".join(traceback.format_exception(type(args[0]), args[0], tb))
            else:
                # JS: `error.stack || error.message`
                args[0] = str(args[0])
            args.insert(0, "%s")
        elif args and not isinstance(args[0], str):
            args.insert(0, "%o")

        format_str = args.pop(0)

        formatters = _get(exporter, "formatters") or {}

        def replace(match):
            char = match.group(1)
            if char == "%":
                return "%"
            formatter = formatters.get(char) or default_formatters.get(char)
            if formatter is not None:
                value = args.pop(0) if args else None
                return str(formatter(value, exporter, message))
            return match.group(0)

        format_str = re.sub(r"%([a-zA-Z%])", replace, format_str)

        o_formatter = formatters.get("o") or default_formatters["o"]
        for arg in args:
            if isinstance(arg, (dict, list, tuple, set)) or (arg is not None and hasattr(arg, "__dict__")):
                arg = o_formatter(arg, exporter, message)
            format_str += " " + str(arg)

        max_length = _get(exporter, "maxLength") or 10240
        return "\n".join(
            line[:max_length] + ("..." if len(line) > max_length else "")
            for line in format_str.split("\n")
        )

    def __init__(self, options: dict, service: "LoggerService"):
        self.name = options.get("name")
        self.level = options.get("level")
        self.meta = options.get("meta")
        self._service = service
        self.error = self._method("error", LoggerLevel["ERROR"])
        self.info = self._method("info", LoggerLevel["INFO"])
        self.warn = self._method("warn", LoggerLevel["WARN"])
        self.debug = self._method("debug", LoggerLevel["DEBUG"])

    def _method(self, type_: str, level: int):
        def method(*args):
            if len(args) == 1 and isinstance(args[0], BaseException):
                cause = args[0].__cause__
                errors = getattr(args[0], "errors", None)
                if cause is not None:
                    method(cause)
                elif isinstance(args[0], ExceptionGroup):
                    for error in args[0].exceptions:
                        method(error)
                    return
                elif errors is not None:
                    for error in errors:
                        method(error)
                    return
            sn = self._service._sn_message = self._service._sn_message + 1
            ts = _now_ms()
            for exporter in list(self._service.exporters.values()):
                levels = _get(exporter, "levels")
                if levels:
                    target_level = levels.get(self.name)
                    if target_level is None:
                        target_level = levels.get("default")
                else:
                    target_level = None
                if target_level is None:
                    target_level = self.level if self.level is not None else LoggerLevel["INFO"]
                if target_level < level:
                    continue
                message = Message(sn, ts, type_, level, self.name, list(args), None)
                if self.meta:
                    for key, value in self.meta.items():
                        setattr(message, key, value)
                export = _get(exporter, "export")
                export(message)

        return method


class LoggerService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.__cordis_tracker__ = {"property": "ctx", "noShadow": True}
        self._buffer_size = 1000
        self.buffer: list = []
        self._sn_message = 0
        self._sn_exporter = 0
        self.exporters: dict[int, Any] = {}

        self_ = self

        class BufferExporter:
            colors = 3
            maxLength = None
            levels = None
            formatters = None

            def export(self, message):
                self_.buffer.append(message)
                overflow = len(self_.buffer) - self_._buffer_size
                if overflow == 1:
                    self_.buffer.pop(0)
                elif overflow > 1:
                    del self_.buffer[0:overflow]

        self.exporter(BufferExporter())

    def __call__(self, *args, **kwargs):
        return self.__cordis_invoke__(*args, **kwargs)

    def __cordis_invoke__(self, name: Optional[str] = None) -> Logger:
        config = self._resolve_config()
        caller = getattr(self, symbols.caller, None) or self.ctx
        fiber = caller.fiber
        if name is None:
            name = config.get("name")
        if name is None:
            name = hyphenate(fiber.name)
        return Logger({"name": name, "level": config.get("level"), "meta": {"fiber": fiber}}, self)

    def _resolve_config(self) -> dict:
        configs = []
        chain = []
        obj = self.ctx
        while obj is not None:
            chain.append(obj)
            obj = obj.__dict__.get("_parent")
        for obj in reversed(chain):
            intercept = obj.__dict__.get("_intercept")
            if intercept is not None and "logger" in intercept:
                configs.append(intercept["logger"])
        merged = {}
        for config in configs:
            if config:
                merged.update(config)
        return merged

    def exporter(self, exporter):
        self_ = self

        def effect():
            self_._sn_exporter += 1
            id_ = self_._sn_exporter
            self_.exporters[id_] = exporter

            def disposer():
                del self_.exporters[id_]

            return disposer

        return self.ctx.effect(effect, "ctx.logger.exporter()")

    @property
    def bufferSize(self) -> int:
        return self._buffer_size

    @bufferSize.setter
    def bufferSize(self, value: int) -> None:
        self._buffer_size = value

    def error(self, *args):
        return self.__cordis_invoke__().error(*args)

    def info(self, *args):
        return self.__cordis_invoke__().info(*args)

    def warn(self, *args):
        return self.__cordis_invoke__().warn(*args)

    def debug(self, *args):
        return self.__cordis_invoke__().debug(*args)
