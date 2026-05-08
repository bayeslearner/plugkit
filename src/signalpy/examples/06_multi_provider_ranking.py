"""Example 06 — Multi-Provider with Ranking

Multiple database adapters with different priorities. The highest-ranked
one gets injected. When a better one is hot-added, consumers switch
automatically via reactive propagation.
Domain: database / data layer.
Shows: service.ranking, @prop, @computed, reactive auto-switch on hot-add.

Run: PYTHONPATH=. python examples/06_multi_provider_ranking.py
"""
import asyncio
from typing import Protocol, runtime_checkable
from pydantic import BaseModel
from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle, computed, effect, prop


@runtime_checkable
class IDatabase(Protocol):
    def query(self, sql: str) -> list: ...
    @property
    def name(self) -> str: ...


@component("sqlite-db")
@provides(IDatabase)
@prop("_rank", "service.ranking", 10)  # low priority — fallback
class SQLiteDB:
    @lifecycle.activate
    def activate(self):
        print("  SQLite database ready (fallback)")
    @property
    def name(self): return "SQLite"
    def query(self, sql): return [{"source": "sqlite", "sql": sql}]


@component("postgres-db")
@provides(IDatabase)
@prop("_rank", "service.ranking", 5)  # medium priority
class PostgresDB:
    @lifecycle.activate
    def activate(self):
        print("  PostgreSQL database ready")
    @property
    def name(self): return "PostgreSQL"
    def query(self, sql): return [{"source": "postgres", "sql": sql}]


class QueryParams(BaseModel):
    sql: str = "SELECT 1"


@component("app")
@requires(db=IDatabase)
class App:
    @lifecycle.activate
    def activate(self):
        self.switch_log = []

    @computed
    def db_name(self):
        return self.rt.db.name

    @effect
    def on_db_switch(self):
        name = self.rt.db.name
        self.switch_log.append(name)
        print(f"  [effect] Using database: {name}")

    @runnable("query", params=QueryParams, description="Run a query")
    async def run_query(self, params):
        return {"db": self.db_name(), "results": self.rt.db.query(params.sql)}


async def main():
    kernel = Kernel()
    kernel.discover([SQLiteDB, PostgresDB, App])
    await kernel.boot()

    print()
    # Find and call the runnable schema directly
    r = await kernel.invoke("app.query", {"sql": "SELECT * FROM users"})
    print(f"  Query result: db={r['db']}, rows={len(r['results'])}")

    # Hot-add a higher-priority Redis cache (acts as a "database")
    print()
    @component("redis-db")
    @provides(IDatabase)
    @prop("_rank", "service.ranking", 1)  # highest priority
    class RedisDB:
        @lifecycle.activate
        def activate(self):
            print("  Redis database ready (high priority)")
        @property
        def name(self): return "Redis"
        def query(self, sql): return [{"source": "redis", "cached": True}]

    await kernel.hot_add(RedisDB)

    r = await kernel.invoke("app.query", {"sql": "SELECT * FROM users"})
    print(f"  Query result: db={r['db']}, rows={len(r['results'])}")

    app = kernel.lifecycle.get_instance("app").instance
    print(f"\n  Database switch history: {app.switch_log}")

    await kernel.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
