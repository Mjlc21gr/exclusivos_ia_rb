"""Endpoints HTTP del sistema RVE."""

import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from api.schemas import (
    CancelacionResponse,
    CompletarResponse,
    DebugNowRequest,
    DecisionResponse,
    RoutingModeRequest,
    ServicioRequest,
)
from services.dispatch_service import DispatchRetryRequiredError, DispatchService
from services.google_matrix_estimator import get_estimates, reset_estimates
from utils.config import get_settings
from utils.time_utils import format_datetime, get_simulated_now, now_bogota, set_simulated_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rve", tags=["RVE"])
dispatch_service = DispatchService()


async def verify_api_key(x_api_key: str = Header(default="", alias="X-Api-Key")) -> str:
    """Valida la API Key si esta configurada."""

    settings = get_settings()
    if not settings.endpoint_api_key:
        logger.warning("ENDPOINT_API_KEY no configurada; se omite validacion")
        return x_api_key
    if x_api_key != settings.endpoint_api_key:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return x_api_key


def verify_test_clock_enabled() -> None:
    """Habilita endpoints de depuracion solo en modo local controlado."""

    settings = get_settings()
    if not settings.allow_test_clock:
        raise HTTPException(status_code=404, detail="Not found")


@router.post("/servicio", response_model=DecisionResponse, dependencies=[Depends(verify_api_key)])
async def evaluar_nuevo_servicio(servicio: ServicioRequest, request: Request) -> DecisionResponse:
    """Recibe un servicio y devuelve ACEPTAR o RECHAZAR."""

    request_id = getattr(request.state, "request_id", uuid4().hex[:8])
    logger.info(
        "endpoint.servicio.received request_id=%s autorizacion=%s fecha_servicio=%s",
        request_id,
        servicio.autorizacion,
        servicio.fecha_servicio,
    )
    try:
        outcome = await run_in_threadpool(
            dispatch_service.procesar_nuevo_servicio,
            servicio.model_dump(),
            request_id,
        )
    except DispatchRetryRequiredError as exc:
        logger.warning(
            "endpoint.servicio.retry_required request_id=%s autorizacion=%s detail=%s",
            request_id,
            servicio.autorizacion,
            str(exc),
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(
            "endpoint.servicio.unhandled request_id=%s autorizacion=%s error_type=%s error=%s",
            request_id,
            servicio.autorizacion,
            type(exc).__name__,
            str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail="No fue posible confirmar el estado del servicio en Google Sheets. Reintente con la misma autorizacion.",
        )
    logger.info(
        "endpoint.servicio.response request_id=%s autorizacion=%s decision=%s razon=%s",
        request_id,
        outcome.autorizacion,
        outcome.decision,
        outcome.razon,
    )
    return DecisionResponse(
        autorizacion=outcome.autorizacion,
        decision=outcome.decision,
        id_turno=outcome.id_turno,
        cedula_conductor=outcome.cedula_conductor,
        nombre_conductor=outcome.nombre_conductor,
        razon=outcome.razon,
        timestamp=format_datetime(now_bogota()),
    )


@router.post("/cancelacion", response_model=CancelacionResponse, dependencies=[Depends(verify_api_key)])
async def cancelar_servicio(
    autorizacion: str = Query(..., description="Numero de autorizacion del servicio"),
    request: Request = None,
) -> CancelacionResponse:
    """Cancela un servicio y libera su slot."""

    request_id = getattr(request.state, "request_id", uuid4().hex[:8]) if request else uuid4().hex[:8]
    logger.info(
        "endpoint.cancelacion.received request_id=%s autorizacion=%s",
        request_id,
        autorizacion,
    )
    result = await run_in_threadpool(dispatch_service.cancelar_servicio, autorizacion, request_id)
    return CancelacionResponse(
        autorizacion=autorizacion,
        estado=result["estado"],
        resultado=result["resultado"],
    )


@router.post("/validar", dependencies=[Depends(verify_api_key)])
async def bloquear_asignaciones(request: Request):
    """Congela las preasignaciones dentro de la ventana de 1 hora."""

    request_id = getattr(request.state, "request_id", uuid4().hex[:8])
    logger.info("endpoint.validar.received request_id=%s", request_id)
    return await run_in_threadpool(dispatch_service.bloquear_servicios_proximos, request_id)


@router.post("/config/enrutamiento", dependencies=[Depends(verify_api_key)])
async def configurar_tipo_enrutamiento(payload: RoutingModeRequest, request: Request):
    """Cambia el modo de enrutamiento entre AUTOMATICO y MANUAL."""

    request_id = getattr(request.state, "request_id", uuid4().hex[:8])
    logger.info(
        "endpoint.config.enrutamiento.received request_id=%s tipo=%s",
        request_id,
        payload.tipo_enrutamiento,
    )
    return await run_in_threadpool(
        dispatch_service.configurar_tipo_enrutamiento,
        payload.tipo_enrutamiento,
        request_id,
    )


@router.post("/completar", response_model=CompletarResponse, dependencies=[Depends(verify_api_key)])
async def completar_servicio(
    autorizacion: str = Query(..., description="Numero de autorizacion del servicio"),
    request: Request = None,
) -> CompletarResponse:
    """Marca un servicio como completado."""

    request_id = getattr(request.state, "request_id", uuid4().hex[:8]) if request else uuid4().hex[:8]
    logger.info(
        "endpoint.completar.received request_id=%s autorizacion=%s",
        request_id,
        autorizacion,
    )
    result = await run_in_threadpool(dispatch_service.completar_servicio, autorizacion, request_id)
    return CompletarResponse(
        autorizacion=autorizacion,
        estado=result["estado"],
        resultado=result["resultado"],
    )


@router.post(
    "/_debug/now",
    dependencies=[Depends(verify_api_key), Depends(verify_test_clock_enabled)],
)
async def fijar_hora_simulada(payload: DebugNowRequest):
    """Fija la hora simulada del proceso para pruebas locales."""

    value = set_simulated_now(payload.now_value)
    return {
        "resultado": "OK",
        "hora_simulada": format_datetime(value) if value else "",
    }


@router.post(
    "/_debug/reset-lock",
    dependencies=[Depends(verify_api_key), Depends(verify_test_clock_enabled)],
)
async def debug_reset_lock():
    """Limpia el lock de CONFIG desde la misma instancia en pruebas locales."""

    repository = dispatch_service.repository
    worksheet = repository.worksheet(repository.settings.sheet_config)
    header_map = repository._header_map(worksheet)
    repository._update_row(
        worksheet,
        2,
        header_map,
        {
            "LOCK_OWNER": "",
            "LOCK_EXPIRES_AT": "",
            "LOCK_UPDATED_AT": format_datetime(now_bogota()),
        },
    )
    return {"resultado": "OK", "hora_simulada": format_datetime(get_simulated_now())}


@router.get(
    "/_debug/config",
    dependencies=[Depends(verify_api_key), Depends(verify_test_clock_enabled)],
)
async def debug_config():
    """Devuelve configuracion efectiva del proceso local."""

    settings = get_settings()
    return {
        "resultado": "OK",
        "spreadsheet_id": settings.spreadsheet_id,
        "sheet_turnos": settings.sheet_turnos,
        "sheet_servicios": settings.sheet_servicios,
        "sheet_preasignaciones": settings.sheet_preasignaciones,
        "sheet_config": settings.sheet_config,
        "allow_test_clock": settings.allow_test_clock,
    }


@router.get(
    "/_debug/google-matrix-estimates",
    dependencies=[Depends(verify_api_key), Depends(verify_test_clock_enabled)],
)
async def debug_google_matrix_estimates():
    """Devuelve registros acumulados del estimador local de matrices."""

    return {"resultado": "OK", "records": get_estimates()}


@router.post(
    "/_debug/google-matrix-estimates/reset",
    dependencies=[Depends(verify_api_key), Depends(verify_test_clock_enabled)],
)
async def debug_google_matrix_estimates_reset():
    """Limpia registros acumulados del estimador local de matrices."""

    reset_estimates()
    return {"resultado": "OK"}


@router.get(
    "/_debug/snapshot/{autorizacion}",
    dependencies=[Depends(verify_api_key), Depends(verify_test_clock_enabled)],
)
async def debug_snapshot_servicio(autorizacion: str):
    """Devuelve el estado actual del servicio y sus preasignaciones."""

    repository = dispatch_service.repository
    servicio = repository.get_servicio(autorizacion)
    preasignaciones = [
        item for item in repository.list_preasignaciones() if item.autorizacion == autorizacion
    ]
    return {
        "autorizacion": autorizacion,
        "servicio_estado": servicio.estado_operacion if servicio else "NO_ENCONTRADO",
        "id_turno": servicio.id_turno if servicio else "",
        "cedula_conductor": servicio.cedula_conductor if servicio else "",
        "nombre_conductor": servicio.nombre_conductor if servicio else "",
        "preasignaciones": [
            {
                "id_preasignacion": item.id_preasignacion,
                "id_turno": item.id_turno,
                "nombre_tecnico_preasignacion": item.nombre_tecnico_preasignacion,
                "estado_preasignacion": item.estado_preasignacion,
                "orden_en_ruta": item.orden_en_ruta,
            }
            for item in preasignaciones
        ],
    }
