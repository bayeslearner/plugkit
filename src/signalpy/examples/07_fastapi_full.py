"""Example 07 — FastAPI Full Stack

Complete web application: multiple apps, gateway composition, REST endpoints,
reactive config, auth — all through FastAPI.
Domain: web application / API server.
Shows: Full platform boot, APIGateway, RESTTransport, multiple @api surfaces,
       HTTP request → bus → component → response pipeline.

Run: PYTHONPATH=. python examples/07_fastapi_full.py
     Then: curl http://localhost:8000/api/v1/tasks/list
"""
import asyncio
from pydantic import BaseModel
from signalpy.kernel import Kernel, component, provides, requires, runnable, lifecycle, computed


class TaskParams(BaseModel):
    title: str = ""
    status: str = "pending"

class TaskIdParams(BaseModel):
    id: int = 0


@component("task-service", version="1.0", rest={"prefix": "/tasks", "version": "v1"})
@provides("ITaskService")
@requires(config="IConfig", logger="ILogger")
class TaskService:
    @lifecycle.activate
    def activate(self):
        self._tasks = [
            {"id": 1, "title": "Build reactive kernel", "status": "done"},
            {"id": 2, "title": "Write examples", "status": "in_progress"},
            {"id": 3, "title": "Deploy to production", "status": "pending"},
        ]
        self.rt.logger.info("Task service ready", count=len(self._tasks))

    @computed
    def task_count(self):
        return len(self._tasks)

    @runnable("list", params=BaseModel, description="List all tasks")
    async def list_tasks(self, params):
        return {"tasks": self._tasks, "count": self.task_count()}

    @runnable("create", params=TaskParams, description="Create a task",
              requires_action="tasks.write")
    async def create_task(self, params):
        task = {"id": len(self._tasks) + 1, "title": params.title, "status": params.status}
        self._tasks.append(task)
        return {"created": task}

    @runnable("complete", params=TaskIdParams, description="Complete a task")
    async def complete_task(self, params):
        for t in self._tasks:
            if t["id"] == params.id:
                t["status"] = "done"
                return {"completed": t}
        return {"error": "Not found"}


async def main():
    from signalpy.providers.config import ConfigProvider
    from signalpy.providers.logging_provider import LoggingProvider
    from signalpy.providers.credentials import CredentialProvider
    from signalpy.providers.storage import StorageProvider
    from signalpy.providers.auth import AuthProvider
    from signalpy.providers.gateway import APIGateway
    from signalpy.adapters.rest import RESTTransport

    kernel = Kernel()
    kernel.discover([
        ConfigProvider, LoggingProvider, CredentialProvider,
        StorageProvider, AuthProvider, APIGateway, RESTTransport,
        TaskService,
    ])
    await kernel.boot()

    # Test via runnable schemas
    print("\n=== Runnable schema invocations ===")
    r = await kernel.invoke("task-service.list", {})
    print(f"  Tasks: {r['count']} total")

    r = await kernel.invoke("task-service.complete", {"id": 2})
    print(f"  Completed: {r}")

    # Test via HTTP
    print("\n=== HTTP requests ===")
    rest = kernel.registry.require("IRestAPI")
    from httpx import AsyncClient, ASGITransport
    async with AsyncClient(transport=ASGITransport(app=rest.app), base_url="http://test") as c:
        r = await c.post("/api/v1/tasks/list", json={})
        print(f"  GET /tasks/list: {r.json()['data']['count']} tasks")

        r = await c.post("/api/v1/tasks/complete", json={"id": 3})
        print(f"  POST /tasks/complete: {r.json()['data']}")

        r = await c.get("/health")
        print(f"  GET /health: {r.json()}")

    # Status
    print("\n=== Kernel status ===")
    for comp in kernel.status()["components"]:
        if comp["runnables"]:
            print(f"  {comp['name']:20s} runnables={comp['runnables']}")

    await kernel.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
