"""Example 10 — Multi-Tenant Isolation (L3 Targeted)

Same component factory, different instances per tenant. Each tenant
gets its own config, credentials, and storage scope — enforced
structurally by the kernel, not by application code.

Domain: SaaS / multi-tenant backend.
Shows: kernel.instantiate() with properties, L3 targeted trait,
       bus routing by target, structural scoping.

No kernel changes required.

Run: PYTHONPATH=src python -m signalpy.examples.multi_tenant
"""
import asyncio
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    computed, effect, prop,
)
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider
from signalpy.providers.credentials import CredentialProvider


# ── Tenant data service — one factory, many instances ───────────

class QueryParams(BaseModel):
    table: str = "users"
    limit: int = 10


@component("tenant-db", version="1.0")
@requires(config="IConfig", creds="ICredentials")
class TenantDatabase:
    """Database adapter scoped to a single tenant.

    Each instance gets:
    - Its own config scope (from properties)
    - Its own credential scope (structural — kernel enforces)
    - Bus routing by target property
    """

    @lifecycle.activate
    def activate(self):
        self._target = self.rt.properties.get("target", "unknown")
        self._db_url = self.rt.config.get(
            f"tenants.{self._target}.db_url",
            f"postgres://localhost/{self._target}"
        )
        self.query_log = []

    @computed
    def db_url(self):
        """Reactive: recomputes if tenant config changes."""
        target = self.rt.properties.get("target", "unknown")
        return self.rt.config.get(f"tenants.{target}.db_url", f"postgres://localhost/{target}")

    @runnable("query", params=QueryParams, description="Query tenant database")
    async def query(self, params):
        self.query_log.append(params.table)
        return {
            "tenant": self._target,
            "db_url": self.db_url(),
            "table": params.table,
            "rows": [{"id": 1, "tenant": self._target}],
        }

    @runnable("status", params=BaseModel, description="Tenant DB status")
    async def status(self, params):
        return {
            "tenant": self._target,
            "db_url": self.db_url(),
            "queries": len(self.query_log),
        }


# ── Main ────────────────────────────────────────────────────────

async def main():
    kernel = Kernel()
    kernel.discover([ConfigProvider, LoggingProvider, CredentialProvider, TenantDatabase])

    # Seed per-tenant config
    kernel.instantiate("config", properties={
        "defaults": {
            "tenants": {
                "acme": {"db_url": "postgres://db1.prod/acme", "plan": "enterprise"},
                "globex": {"db_url": "postgres://db2.prod/globex", "plan": "starter"},
                "initech": {"db_url": "postgres://db3.prod/initech", "plan": "pro"},
            }
        }
    })

    # Create per-tenant instances of the same factory
    kernel.instantiate("tenant-db", "tenant-db-acme", {"target": "acme"})
    kernel.instantiate("tenant-db", "tenant-db-globex", {"target": "globex"})
    kernel.instantiate("tenant-db", "tenant-db-initech", {"target": "initech"})

    await kernel.boot()

    # Direct instance calls — use runnable schemas
    print("  === Direct instance calls ===")
    r = await kernel.invoke("tenant-db-acme.query", {"table": "orders"})
    print(f"    Acme: {r['tenant']} → {r['db_url']}")

    r = await kernel.invoke("tenant-db-globex.query", {"table": "users"})
    print(f"    Globex: {r['tenant']} → {r['db_url']}")

    # Target-routed calls (factory name + target param) — bus handles routing
    print()
    print("  === Target-routed calls (factory + target param) ===")
    r = await kernel.invoke("tenant-db.query", {"table": "invoices", "target": "acme"})
    print(f"    Route to acme: {r['tenant']} → {r['db_url']}")

    r = await kernel.invoke("tenant-db.query", {"table": "invoices", "target": "initech"})
    print(f"    Route to initech: {r['tenant']} → {r['db_url']}")

    # Reactive config change — update a tenant's DB URL
    print()
    print("  === Migrate Globex to new DB ===")
    config = kernel.registry.require("IConfig")
    config.set("tenants.globex.db_url", "postgres://db-new.prod/globex-migrated")

    r = await kernel.invoke("tenant-db.query", {"table": "users", "target": "globex"})
    print(f"    Globex after migration: {r['db_url']}")

    # Status of all tenants — target-routed
    print()
    print("  === Tenant status ===")
    for tenant in ["acme", "globex", "initech"]:
        r = await kernel.invoke("tenant-db.status", {"target": tenant})
        print(f"    {r['tenant']}: {r['queries']} queries, db={r['db_url']}")

    await kernel.shutdown()
    print()
    print("  Multi-tenant demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
