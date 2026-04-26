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

    Reads the gateway's "rest" surface if available, otherwise falls back
    to exposing all bus handlers directly.

    Args:
        app: A FastAPI application instance.
        kernel: A booted Kernel instance.
        prefix: URL prefix for all generated routes (default "/api").

    Returns:
        Number of routes added.
    """
    if not _HAS_FASTAPI:
        raise RuntimeError("FastAPI not installed")

    bus = kernel.bus
    gateway = kernel.registry.require_optional("IGateway")
    count = 0

    if gateway:
        surface = gateway.get_surface("rest")
        if surface and surface.entries:
            version_prefix = f"/{surface.version}" if surface.version else ""
            for group_name, entries in surface.groups().items():
                router = APIRouter(
                    prefix=f"{prefix}{version_prefix}/{group_name}",
                    tags=[group_name],
                )
                for entry in entries:
                    _add_route(router, entry.short_name, entry.runnable_name,
                               entry.description, bus)
                    count += 1
                app.include_router(router)
            log.info("mount_rest: %d routes from gateway surface", count)
            _add_kernel_routes(app, kernel, prefix)
            return count

    # No gateway or empty surface — fall back to bus handlers
    router = APIRouter(prefix=prefix, tags=["kernel"])
    for handler_name in bus.handlers:
        _add_route(router, handler_name, handler_name, f"Invoke {handler_name}", bus)
        count += 1
    app.include_router(router)
    log.info("mount_rest: %d routes from bus handlers (no gateway)", count)
    _add_kernel_routes(app, kernel, prefix)
    return count


def _add_route(router, short_name: str, runnable_name: str,
               description: str, bus) -> None:
    """Add a single POST route that invokes a bus handler."""
    def _make_handler(name=runnable_name, b=bus):
        async def _handler(request: Request) -> Any:
            body = await request.json() if await request.body() else {}
            try:
                result = await b.invoke(name, body)
                return JSONResponse({"ok": True, "data": result})
            except PermissionError as exc:
                return JSONResponse(
                    {"ok": False, "error": str(exc)}, status_code=403)
            except KeyError as exc:
                return JSONResponse(
                    {"ok": False, "error": str(exc)}, status_code=404)
            except Exception as exc:
                log.exception("Handler error for %s", name)
                return JSONResponse(
                    {"ok": False, "error": "Internal server error"}, status_code=500)
        return _handler

    router.add_api_route(
        f"/{short_name}",
        _make_handler(),
        methods=["POST"],
        name=short_name,
        summary=description or f"Invoke {short_name}",
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

@component("rest-transport", version="0.3", depends=["gateway"])
@provides("IRestAPI")
@requires(config="IConfig", gateway="IGateway")
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

        # Use the same mount function — DRY
        surface = rt.gateway.get_surface("rest")
        if surface and surface.entries:
            version_prefix = f"/{surface.version}" if surface.version else ""
            for group_name, entries in surface.groups().items():
                router = APIRouter(
                    prefix=f"/api{version_prefix}/{group_name}",
                    tags=[group_name],
                )
                for entry in entries:
                    _add_route(router, entry.short_name, entry.runnable_name,
                               entry.description, rt.bus)
                self.app.include_router(router)
            log.info("REST transport: %d routes in %d groups",
                     len(surface.entries), len(surface.groups()))
        else:
            log.warning("REST transport: no API surface from gateway")

        @self.app.get("/api/kernel/status", tags=["kernel"])
        async def kernel_status():
            return {"surfaces": {
                k: {"entries": len(v.entries), "groups": list(v.groups().keys())}
                for k, v in rt.gateway.surfaces().items()
            }}

        @self.app.get("/health")
        async def health():
            return {"status": "healthy"}

    def get_app(self):
        return self.app

    @lifecycle.deactivate
    def deactivate(self, rt):
        pass
