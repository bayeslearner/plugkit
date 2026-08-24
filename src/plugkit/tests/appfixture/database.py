"""A plugin as a module: `apply`, plus optional `name` and `inject`."""

name = "database"


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn


def apply(ctx, config=None):
    ctx.provide("database", Database((config or {}).get("dsn", "sqlite://")))
