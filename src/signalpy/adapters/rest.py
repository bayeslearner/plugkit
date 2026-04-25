"""REST Transport — renders the gateway's API surface as FastAPI routes.

Consumes: IGateway (the composed API surface)
Provides: IRestAPI (the FastAPI app instance)

The REST transport does NOT decide what to expose — the gateway does.
This adapter just renders the gateway's "rest" surface as HTTP routes.
"""
import logging
from typing import Any

from signalpy.kernel import component, provides, requires, lifecycle

log = logging.getLogger(__name__)

# Lazy-check for FastAPI at import time — but don't crash if missing
try:
    from fastapi import FastAPI, APIRouter, Request
    from fastapi.responses import JSONResponse
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


@component("rest-transport", version="0.2", depends=["gateway"])
@provides("IRestAPI")
@requires(config="IConfig", gateway="IGateway")
class RESTTransport:
    """Renders the gateway's REST surface as FastAPI routes.

    On activation:
      1. Gets the "rest" APISurface from the gateway
      2. Generates routes grouped by API group
      3. Applies auth/versioning from the surface policy
    """

    @lifecycle.activate
    def activate(self, rt):
        if not _HAS_FASTAPI:
            raise RuntimeError("FastAPI not installed — cannot activate REST transport")

        title = rt.config.get("rest.title", "Microkernel API")
        self._rt = rt

        self.app = FastAPI(title=title)

        # ── Get composed surface from gateway ───────────────

        surface = rt.gateway.get_surface("rest")

        if surface and surface.entries:
            version_prefix = f"/{surface.version}" if surface.version else ""

            # Group by API group
            for group_name, entries in surface.groups().items():
                group_router = APIRouter(
                    prefix=f"/api{version_prefix}/{group_name}",
                    tags=[group_name],
                )

                for entry in entries:

                    def _make_handler(name=entry.runnable_name, bus=rt.bus):
                        async def _handler(request: Request) -> Any:
                            body = await request.json() if await request.body() else {}
                            try:
                                result = await bus.invoke(name, body)
                                return JSONResponse({"ok": True, "data": result})
                            except PermissionError as exc:
                                return JSONResponse(
                                    {"ok": False, "error": str(exc)}, status_code=403,
                                )
                            except KeyError as exc:
                                return JSONResponse(
                                    {"ok": False, "error": str(exc)}, status_code=404,
                                )
                            except Exception as exc:
                                log.exception("Handler error for %s", name)
                                return JSONResponse(
                                    {"ok": False, "error": "Internal server error"},
                                    status_code=500,
                                )
                        return _handler

                    group_router.add_api_route(
                        f"/{entry.short_name}",
                        _make_handler(),
                        methods=["POST"],
                        name=f"{group_name}_{entry.short_name}",
                        summary=entry.description or f"Invoke {entry.short_name}",
                    )

                self.app.include_router(group_router)

            log.info("REST transport: %d routes in %d groups",
                     len(surface.entries), len(surface.groups()))
        else:
            log.warning("REST transport: no API surface from gateway")

        # ── Built-in routes ─────────────────────────────────

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
