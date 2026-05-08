"""Search V1 — keyword matching. Dropped into the plugins directory at boot."""
from pydantic import BaseModel
from signalpy.kernel import component, provides, requires, runnable, lifecycle


class SearchParams(BaseModel):
    query: str = ""


@component("search", version="1.0")
@requires(config="IConfig")
@provides("ISearch")
class SearchV1:
    """Version 1: simple keyword matching."""

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

    @runnable("search", params=SearchParams, description="Keyword search")
    async def search(self, params):
        self._query_count += 1
        results = [
            {"id": did, "text": txt}
            for did, txt in self._index.items()
            if params.query.lower() in txt.lower()
        ]
        return {
            "engine": "v1-keyword",
            "query": params.query,
            "results": results,
            "total_queries": self._query_count,
        }

    @runnable("status", params=BaseModel, description="Service status")
    async def status(self, params):
        return {"version": "1.0", "engine": "keyword", "docs": len(self._index), "queries": self._query_count}
