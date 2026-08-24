"""How to get a statically typed `ctx` — checked by `tests/test_typing.py`.

`Context.__getattr__` returns `Any`, because which services exist depends on
which plugins are mounted, and no type system knows that at import time. But you
do not need the *Context* to be typed. You need *your plugin's parameter* to be
typed, and that is a plain Protocol.

Verified against pyright 1.1.x on Python 3.13:

    approach                                        result
    pass a raw Context where a Protocol is wanted   REJECTED - __getattr__ does
                                                    not satisfy protocol members
    annotate your own parameter with a Protocol     WORKS - full completion,
                                                    typos caught
    token lookup: get[T](token: type[T]) -> T       WORKS
    cast(MyDeps, ctx) once at the top               WORKS

The second is the one to use: no runtime cost, no cast, and — with
`typing.get_protocol_members` (Python 3.13+) — the same Protocol also produces
the runtime `inject` list, so there is one source of truth instead of a
declaration the checker knows about and a string list it does not.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..binding import plugin, provide


# ── what your plugin needs, declared once ─────────────────────────────────


class Database(Protocol):
    def query(self, sql: str) -> list[dict]: ...


class Tools(Protocol):
    def register(self, tool: Any) -> Any: ...
    def guard(self, guard: Any) -> Any: ...


class ReportDeps(Protocol):
    """The services this plugin needs. Both the types and the inject list."""

    database: Database
    tools: Tools

    # methods you call on ctx itself, so the checker knows about them too
    def on(self, event: str, listener: Any) -> Any: ...
    def effect(self, execute: Any, label: str = ...) -> Any: ...


# ── the plugin, fully typed ───────────────────────────────────────────────


@plugin
def report_plugin(ctx: ReportDeps, config: dict | None = None) -> None:
    rows = ctx.database.query("SELECT 1")   # typed: list[dict]
    ctx.on("ready", lambda: print(len(rows)))
    # ctx.database.qeury(...)  <- pyright: Cannot access attribute "qeury"
    # ctx.databse              <- pyright: Cannot access attribute "databse"

# `inject` is derived from ReportDeps — no second list to keep in sync.


# ── the same thing for a POPO binding ─────────────────────────────────────


class Reporter:
    """A component. Imports nothing from the kernel; typed against Protocols."""

    def __init__(self, database: Database, prefix: str = "report"):
        self.database = database
        self.prefix = prefix

    def run(self) -> str:
        return f"{self.prefix}: {len(self.database.query('SELECT 1'))} rows"


class ReporterDeps(Protocol):
    database: Database


reporter = provide(
    Reporter,
    "reporter",                             # the service name — always explicit
    needs=ReporterDeps,                     # -> inject = ["database"]
    config={"prefix": ("report.prefix", "report")},
)
