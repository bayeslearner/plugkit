"""Search V2 — relevance scoring. Dropped into plugins/ to trigger hot_update."""
from pydantic import BaseModel
from signalpy.kernel import component, provides, requires, runnable, lifecycle


class SearchParams(BaseModel):
    query: str = ""


@component("search", version="2.0")
@requires(config="IConfig")
@provides("ISearch")
class SearchV2:
    """Version 2: relevance scoring with prefix and exact match bonuses."""

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

    @runnable("index_doc", params=BaseModel, description="Index a document")
    async def index_doc(self, params):
        doc_id = params.get("id", "") if isinstance(params, dict) else ""
        text = params.get("text", "") if isinstance(params, dict) else ""
        self._index[doc_id] = text
        return {"indexed": doc_id, "total": len(self._index)}

    @runnable("search", params=SearchParams, description="Scored search")
    async def search(self, params):
        self._query_count += 1
        query = params.query.lower()
        results = []
        for did, txt in self._index.items():
            txt_lower = txt.lower()
            if query in txt_lower:
                score = 1.0
                if txt_lower.startswith(query):
                    score += 0.5
                if query == txt_lower:
                    score += 1.0
                results.append({"id": did, "text": txt, "score": score})
        results.sort(key=lambda r: r["score"], reverse=True)
        return {
            "engine": "v2-scored",
            "query": params.query,
            "results": results,
            "total_queries": self._query_count,
        }

    @runnable("status", params=BaseModel, description="Service status")
    async def status(self, params):
        return {"version": "2.0", "engine": "scored", "docs": len(self._index), "queries": self._query_count}
