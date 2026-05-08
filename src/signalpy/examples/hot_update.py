"""Example 15 — Hot Code Update

Reload a component with a new version of its class, preserving state.
Running instances are snapshotted, torn down, replaced, re-activated,
and state-restored — without dropping bus handlers or losing data.

Domain: zero-downtime deployments / blue-green upgrades.
Shows: kernel.hot_update(), @lifecycle.snapshot, @lifecycle.restore,
       state preservation across code changes.

This is the ONE pattern that required a kernel change — adding the
hot_update() method and the snapshot/restore lifecycle hooks.

Run: PYTHONPATH=src python -m signalpy.examples.hot_update
"""
import asyncio
from pydantic import BaseModel
from signalpy.kernel import (
    Kernel, component, provides, requires, runnable, lifecycle, computed, effect,
)
from signalpy.providers.config import ConfigProvider
from signalpy.providers.logging_provider import LoggingProvider


# ══════════════════════════════════════════════════════════════════
# Version 1 of the search service
# ══════════════════════════════════════════════════════════════════

class SearchParams(BaseModel):
    query: str = ""


@component("search", version="1.0")
@requires(config="IConfig")
@provides("ISearch")
class SearchV1:
    """Version 1: keyword matching."""

    @lifecycle.activate
    def activate(self):
        self._index = {}
        self._query_count = 0

    @lifecycle.snapshot
    def snapshot(self):
        """Preserve the index and query count across upgrades."""
        return {"index": dict(self._index), "query_count": self._query_count}

    @lifecycle.restore
    def restore(self, state):
        """Restore preserved state after upgrade."""
        self._index = state.get("index", {})
        self._query_count = state.get("query_count", 0)

    @runnable("index_doc", params=BaseModel, description="Add a document to the index")
    async def index_doc(self, params):
        doc_id = params.get("id", "") if isinstance(params, dict) else ""
        text = params.get("text", "") if isinstance(params, dict) else ""
        self._index[doc_id] = text
        return {"indexed": doc_id}

    @runnable("search", params=SearchParams, description="Search the index")
    async def search(self, params):
        self._query_count += 1
        results = [
            {"id": doc_id, "text": text}
            for doc_id, text in self._index.items()
            if params.query.lower() in text.lower()
        ]
        return {
            "engine": "v1-keyword",
            "query": params.query,
            "results": results,
            "total_queries": self._query_count,
        }

    @runnable("status", params=BaseModel, description="Service status")
    async def status(self, params):
        return {
            "version": "1.0",
            "engine": "keyword",
            "docs": len(self._index),
            "queries": self._query_count,
        }


# ══════════════════════════════════════════════════════════════════
# Version 2 — same component name, improved search algorithm
# ══════════════════════════════════════════════════════════════════

@component("search", version="2.0")
@requires(config="IConfig")
@provides("ISearch")
class SearchV2:
    """Version 2: adds relevance scoring and prefix matching."""

    @lifecycle.activate
    def activate(self):
        self._index = {}
        self._query_count = 0

    @lifecycle.snapshot
    def snapshot(self):
        return {"index": dict(self._index), "query_count": self._query_count}

    @lifecycle.restore
    def restore(self, state):
        self._index = state.get("index", {})
        self._query_count = state.get("query_count", 0)

    @runnable("index_doc", params=BaseModel, description="Add a document to the index")
    async def index_doc(self, params):
        doc_id = params.get("id", "") if isinstance(params, dict) else ""
        text = params.get("text", "") if isinstance(params, dict) else ""
        self._index[doc_id] = text
        return {"indexed": doc_id}

    @runnable("search", params=SearchParams, description="Search the index (v2)")
    async def search(self, params):
        self._query_count += 1
        query = params.query.lower()
        results = []
        for doc_id, text in self._index.items():
            text_lower = text.lower()
            if query in text_lower:
                # v2: relevance scoring (word position + exact match bonus)
                score = 1.0
                if text_lower.startswith(query):
                    score += 0.5  # prefix bonus
                if query == text_lower:
                    score += 1.0  # exact match bonus
                results.append({"id": doc_id, "text": text, "score": score})
        results.sort(key=lambda r: r["score"], reverse=True)
        return {
            "engine": "v2-scored",
            "query": params.query,
            "results": results,
            "total_queries": self._query_count,
        }

    @runnable("status", params=BaseModel, description="Service status")
    async def status(self, params):
        return {
            "version": "2.0",
            "engine": "scored",
            "docs": len(self._index),
            "queries": self._query_count,
        }


# ══════════════════════════════════════════════════════════════════
# Demo
# ══════════════════════════════════════════════════════════════════

async def main():
    kernel = Kernel()
    kernel.discover([ConfigProvider, LoggingProvider, SearchV1])
    kernel.instantiate("config", properties={"defaults": {}})
    await kernel.boot()

    print("  === V1 running ===")
    # Find and call runnable schemas directly
    status = await kernel.invoke("search.status", {})
    print(f"    Status: {status}")

    # Index some documents
    await kernel.invoke("search.index_doc", {"id": "1", "text": "Python programming language"})
    await kernel.invoke("search.index_doc", {"id": "2", "text": "Python snake species"})
    await kernel.invoke("search.index_doc", {"id": "3", "text": "JavaScript framework"})

    # Search with v1
    r = await kernel.invoke("search.search", {"query": "python"})
    print(f"    V1 search 'python': {len(r['results'])} results, engine={r['engine']}")
    print(f"    Queries so far: {r['total_queries']}")

    # ── Hot update to V2 ──────────────────────────────────────
    print()
    print("  === Hot update: V1 → V2 ===")
    new_instances = await kernel.hot_update(SearchV2)
    print(f"    Updated {len(new_instances)} instance(s)")

    # Re-fetch schemas after hot update (handlers changed)
    # State should be preserved
    status = await kernel.invoke("search.status", {})
    print(f"    Status after update: {status}")
    assert status["version"] == "2.0"
    assert status["docs"] == 3        # index preserved
    assert status["queries"] == 1     # query count preserved

    # Search with v2 — now has relevance scoring
    r = await kernel.invoke("search.search", {"query": "python"})
    print(f"    V2 search 'python': {len(r['results'])} results, engine={r['engine']}")
    for hit in r["results"]:
        print(f"      {hit['id']}: {hit['text']} (score={hit.get('score', 'n/a')})")
    print(f"    Queries after update: {r['total_queries']}")
    assert r["total_queries"] == 2  # count continued from v1

    await kernel.shutdown()
    print()
    print("  Hot update demo complete.")


if __name__ == "__main__":
    asyncio.run(main())
