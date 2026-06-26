"""Aplicacion FastAPI principal."""

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from gspread.exceptions import APIError

from api.routes import router
from utils.monitoring import get_metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)
metrics = get_metrics()

app = FastAPI(
    title="RVE - Red de Vehiculos Exclusivos",
    version="3.0.0",
    description="Sistema de dispatching para asignacion dinamica de servicios",
)

app.include_router(router)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Loguea inicio y fin de cada request con un identificador unico."""

    request_id = uuid.uuid4().hex[:8]
    request.state.request_id = request_id
    start_time = time.monotonic()
    logger.info(
        "request.start request_id=%s method=%s path=%s",
        request_id,
        request.method,
        request.url.path,
    )
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        logger.exception(
            "request.error request_id=%s method=%s path=%s duration_ms=%s",
            request_id,
            request.method,
            request.url.path,
            elapsed_ms,
        )
        raise

    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    logger.info(
        "request.end request_id=%s method=%s path=%s status=%s duration_ms=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    response.headers["X-Request-Id"] = request_id
    return response


@app.get("/health")
async def health_check():
    """Health check para despliegue."""

    return {"status": "healthy", "service": "rve-microservice"}


@app.get("/lookup")
async def lookup_legacy():
    """Compatibilidad hacia atras con el endpoint legado."""

    return {
        "message": "Use /rve/servicio endpoint for service evaluation",
        "version": "3.0",
    }


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Control global de errores inesperados."""

    if isinstance(exc, APIError):
        logger.exception("Google Sheets API error: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"error": "Google Sheets temporalmente no disponible"},
        )
    logger.exception("Unhandled error: %s", exc)
    metrics.increment("rve.errors", tags=["error:global"])
    return JSONResponse(status_code=500, content={"error": "Internal server error"})
