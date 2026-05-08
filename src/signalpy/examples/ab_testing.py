"""Example 09 — A/B Testing (Dynamic Service Selection)

Two search backends (v1 and v2) provide the same contract.
An A/B router reads the split percentage from config and routes
each request to v1 or v2. Change the split at runtime — the
@computed recomputes immediately.

Domain: experimentation / gradual rollout.
Shows: multiple providers of same contract, map injection,
       @computed for config-driven routing, runtime split changes.

No kernel changes required.

Run: PYTHONPATH=src python -m signalpy.examples.ab_testing
"""
import asyncio
import hashlib
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle,
    computed, effect, prop,
)
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider


# ── Search contract — two versions ──────────────────────────────

class SearchParams(BaseModel):
    query: str = ""
    user_id: str = "anonymous"


@component("search-v1", version="1.0")
@provides("ISearch")
@prop("_version", "version", "v1")
class SearchV1:
    """Legacy search — keyword matching."""

    @lifecycle.activate
    def activate(self):
        self._call_count = 0

    @runnable("search", params=SearchParams, description="Search v1")
    async def search(self, params):
        self._call_count += 1
        return {"engine": "v1-keyword", "query": params.query, "results": ["legacy-result"]}


@component("search-v2", version="2.0")
@provides("ISearch")
@prop("_version", "version", "v2")
class SearchV2:
    """New search — semantic matching."""

    @lifecycle.activate
    def activate(self):
        self._call_count = 0

    @runnable("search", params=SearchParams, description="Search v2")
    async def search(self, params):
        self._call_count += 1
        return {"engine": "v2-semantic", "query": params.query, "results": ["semantic-result"]}


# ── A/B Router — reads split from config, routes by user_id ────

@component("search-router", version="1.0")
@provides("ISearchRouter")
@requires(config="IConfig")
@requires(backends="ISearch", key="version")
class ABRouter:
    """Routes search requests between v1 and v2 based on config split.

    Uses deterministic hashing on user_id so the same user always
    gets the same variant (consistent bucketing).
    """

    @lifecycle.activate
    def activate(self):
        self.route_log = []

    @computed
    def v2_percentage(self):
        """Reactive: recomputes when config changes."""
        return self.rt.config.get("ab.search.v2_percent", 0)

    def _pick_variant(self, user_id: str) -> str:
        """Deterministic: hash user_id to a 0-99 bucket."""
        bucket = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % 100
        return "v2" if bucket < self.v2_percentage() else "v1"

    @runnable("search", params=SearchParams, description="A/B routed search")
    async def search(self, params):
        variant = self._pick_variant(params.user_id)
        backends = self.rt.backends
        backend = backends.get(variant)
        if backend is None:
            return {"error": f"No backend for variant {variant}"}
        result = await backend.search(params)
        self.route_log.append(variant)
        result["variant"] = variant
        return result


# ── Main ────────────────────────────────────────────────────────

async def main():
    kernel = Kernel()
    kernel.discover([
        ConfigProvider, LoggingProvider,
        SearchV1, SearchV2, ABRouter,
    ])
    kernel.instantiate("config", properties={
        "defaults": {"ab": {"search": {"v2_percent": 0}}}
    })
    await kernel.boot()

    router = kernel.lifecycle.get_instance("search-router").instance
    v1 = kernel.lifecycle.get_instance("search-v1").instance
    v2 = kernel.lifecycle.get_instance("search-v2").instance
    config = kernel.registry.require("IConfig")

    # Find the runnable schema directly
    # Phase 1: 0% v2 — all traffic goes to v1
    print("  === Phase 1: 0% v2 ===")
    for uid in ["alice", "bob", "charlie", "dave", "eve"]:
        r = await kernel.invoke("search-router.search", {"query": "test", "user_id": uid})
        print(f"    {uid}: {r['variant']} ({r['engine']})")

    print(f"  v1 calls: {v1._call_count}, v2 calls: {v2._call_count}")
    assert v2._call_count == 0

    # Phase 2: 50% v2 — change split at runtime
    print()
    print("  === Phase 2: 50% v2 (config.set) ===")
    config.set("ab.search.v2_percent", 50)

    v1._call_count = 0
    v2._call_count = 0
    for uid in ["alice", "bob", "charlie", "dave", "eve"]:
        r = await kernel.invoke("search-router.search", {"query": "test", "user_id": uid})
        print(f"    {uid}: {r['variant']} ({r['engine']})")

    print(f"  v1 calls: {v1._call_count}, v2 calls: {v2._call_count}")
    assert v1._call_count + v2._call_count == 5
    assert v2._call_count > 0  # some traffic went to v2

    # Phase 3: 100% v2 — full rollout
    print()
    print("  === Phase 3: 100% v2 ===")
    config.set("ab.search.v2_percent", 100)

    v1._call_count = 0
    v2._call_count = 0
    for uid in ["alice", "bob", "charlie", "dave", "eve"]:
        r = await kernel.invoke("search-router.search", {"query": "test", "user_id": uid})
        print(f"    {uid}: {r['variant']} ({r['engine']})")

    print(f"  v1 calls: {v1._call_count}, v2 calls: {v2._call_count}")
    assert v1._call_count == 0  # all traffic on v2

    await kernel.shutdown()
    print()
    print("  A/B testing demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
