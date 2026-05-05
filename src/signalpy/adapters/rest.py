"""REST adapter — bridge between SignalPy kernel and FastAPI.

Two modes:

  Container mode (SignalPy owns the app):
    kernel.discover([..., RESTTransport])
    await kernel.boot()
    app = kernel.registry.require("IRestAPI").get_app()

  Library mode (mount into your existing app):
    app = FastAPI()
    kernel = Kernel(); kernel.discover([...]); await kernel.boot()
    mount_rest(app, kernel)

Spec 011: Routes call schema.handler directly — no bus.invoke.
"""
import logging
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)

try:
    from fastapi import FastAPI, APIRouter, Request
    from fastapi.responses import JSONResponse
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


# ── Library mode: mount into an existing FastAPI app ──────────────

def mount_rest(app, kernel, *, prefix: str = "/api") -> int:
    """Mount kernel runnables as POST routes on an existing FastAPI app.

    Uses kernel.runnables_by_component(transport="rest") to discover
    operations and their schemas, then builds routes that call
    schema.handler directly.

    Args:
        app: A FastAPI application instance.
        kernel: A booted Kernel instance.
        prefix: URL prefix for all generated routes (default "/api").

    Returns:
        Number of routes added.
    """
    if not _HAS_FASTAPI:
        raise RuntimeError("FastAPI not installed")

    count = 0
    grouped = kernel.runnables_by_component(transport="rest")

    for comp_name, info in grouped.items():
        tc = info["transport_config"]
        comp_prefix = tc.get("prefix", f"/{comp_name}")
        version = tc.get("version", "")
        version_prefix = f"/{version}" if version else ""

        router = APIRouter(
            prefix=f"{prefix}{version_prefix}{comp_prefix}",
            tags=[comp_name],
        )
        for schema in info["schemas"]:
            _add_route(router, schema)
            count += 1
        app.include_router(router)

    if not grouped:
        # Fallback: expose all runnables under prefix
        router = APIRouter(prefix=prefix, tags=["kernel"])
        for schema in kernel.runnables(transport="rest"):
            _add_route(router, schema)
            count += 1
        app.include_router(router)

    log.info("mount_rest: %d routes", count)
    _add_kernel_routes(app, kernel, prefix)
    return count


def _add_route(router, schema) -> None:
    """Add a single POST route that calls schema.handler directly."""
    def _make_handler(s=schema):
        async def _handler(request: Request) -> Any:
            body = await request.json() if await request.body() else {}
            try:
                result = await s.handler(body)
                return JSONResponse({"ok": True, "data": result})
            except PermissionError as exc:
                return JSONResponse(
                    {"ok": False, "error": str(exc)}, status_code=403)
            except KeyError as exc:
                return JSONResponse(
                    {"ok": False, "error": str(exc)}, status_code=404)
            except Exception as exc:
                log.exception("Handler error for %s", s.name)
                return JSONResponse(
                    {"ok": False, "error": "Internal server error"}, status_code=500)
        return _handler

    router.add_api_route(
        f"/{schema.name}",
        _make_handler(),
        methods=["POST"],
        name=schema.name,
        summary=schema.description or f"Invoke {schema.name}",
    )


def _add_kernel_routes(app, kernel, prefix: str) -> None:
    """Add built-in kernel status and health routes."""
    @app.get(f"{prefix}/kernel/status", tags=["kernel"])
    async def kernel_status():
        return kernel.status()

    @app.get("/health")
    async def health():
        return {"status": "healthy"}


# ── Container mode: SignalPy manages the FastAPI app ──────────────

@component("rest-transport", version="0.3")
@provides("IRestAPI")
@requires(config="IConfig")
class RESTTransport:
    """Container mode — SignalPy creates and owns the FastAPI app.

    Usage:
        kernel.discover([..., RESTTransport])
        await kernel.boot()
        app = kernel.registry.require("IRestAPI").get_app()
        uvicorn.run(app, port=8000)
    """

    @lifecycle.activate
    def activate(self, rt):
        if not _HAS_FASTAPI:
            raise RuntimeError("FastAPI not installed — cannot activate REST transport")

        title = rt.config.get("rest.title", "Microkernel API")
        self.app = FastAPI(title=title)
        self._rt = rt

        # TODO: access kernel.runnables_by_component from rt
        # For now, the container mode is minimal

        @self.app.get("/health")
        async def health():
            return {"status": "healthy"}

    def get_app(self):
        return self.app

    @lifecycle.deactivate
    def deactivate(self, rt):
        pass
