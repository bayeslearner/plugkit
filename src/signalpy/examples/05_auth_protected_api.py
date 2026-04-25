"""Example 05 — Auth-Protected API

A document service with public (list) and protected (create, delete)
operations. Auth enforcement at bus level — same check for REST, MCP, CLI.
Domain: document management / CMS.
Shows: requires_action, requires_role, @api, bus-level auth, FastAPI integration.

Run: PYTHONPATH=. python examples/05_auth_protected_api.py
"""
import asyncio
from pydantic import BaseModel
from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle, api
from signalpy.providers.config import ConfigProvider
from signalpy.providers.auth import AuthProvider


class ListParams(BaseModel):
    pass

class CreateParams(BaseModel):
    title: str
    content: str = ""

class DeleteParams(BaseModel):
    id: str


@component("doc-service")
@provides("IDocService")
@requires(config="IConfig")
@api("rest", prefix="/docs", version="v1")
class DocumentService:
    @lifecycle.activate
    def activate(self):
        self._docs = {
            "doc-1": {"title": "Getting Started", "content": "Welcome..."},
            "doc-2": {"title": "API Reference", "content": "Endpoints..."},
        }

    @runnable("list", params=ListParams, description="List all documents")
    async def list_docs(self, params):
        """Public — no auth required."""
        return {"docs": list(self._docs.values())}

    @runnable("create", params=CreateParams, description="Create document",
              requires_action="docs.write")
    async def create_doc(self, params):
        """Protected — must be authorized for 'docs.write'."""
        doc_id = f"doc-{len(self._docs) + 1}"
        self._docs[doc_id] = {"title": params.title, "content": params.content}
        return {"id": doc_id, "created": True}

    @runnable("delete", params=DeleteParams, description="Delete document",
              requires_role="admin")
    async def delete_doc(self, params):
        """Protected — must have 'admin' role."""
        if params.id in self._docs:
            del self._docs[params.id]
            return {"deleted": True}
        return {"error": "Not found"}


async def main():
    kernel = Kernel()
    kernel.discover([ConfigProvider, AuthProvider, DocumentService])
    await kernel.boot()

    # Configure auth
    auth = kernel.lifecycle.get_instance("auth").instance
    auth._enabled = True
    auth._tokens = {
        "admin-token": {"identity": "admin", "roles": ["admin"]},
        "writer-token": {"identity": "writer", "roles": ["writer"]},
        "reader-token": {"identity": "reader", "roles": ["reader"]},
    }
    auth._policies = {
        "admin": ["*"],
        "writer": ["docs.write", "docs.read"],
        "reader": ["docs.read"],
    }

    print("=== Document Service ===\n")

    # Public: list (no auth needed)
    r = await kernel.bus.invoke("doc-service.list", {})
    print(f"  List (public): {len(r['docs'])} docs")

    # Protected: create with writer token
    r = await kernel.bus.invoke("doc-service.create", {
        "title": "New Doc",
        "__auth_token__": "writer-token",
    })
    print(f"  Create (writer): {r}")

    # Protected: create without token → PermissionError
    try:
        await kernel.bus.invoke("doc-service.create", {"title": "Fail"})
    except PermissionError as e:
        print(f"  Create (no auth): PermissionError — {e}")

    # Protected: create with reader token → PermissionError
    try:
        await kernel.bus.invoke("doc-service.create", {
            "title": "Fail", "__auth_token__": "reader-token",
        })
    except PermissionError as e:
        print(f"  Create (reader): PermissionError — {e}")

    # Protected: delete requires admin role
    r = await kernel.bus.invoke("doc-service.delete", {
        "id": "doc-1", "__auth_token__": "admin-token",
    })
    print(f"  Delete (admin): {r}")

    try:
        await kernel.bus.invoke("doc-service.delete", {
            "id": "doc-2", "__auth_token__": "writer-token",
        })
    except PermissionError as e:
        print(f"  Delete (writer): PermissionError — {e}")

    await kernel.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
