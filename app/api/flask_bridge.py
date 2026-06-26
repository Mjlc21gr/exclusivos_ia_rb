"""Puente Flask para exponer los endpoints RVE desde la app raiz."""

from __future__ import annotations

import html as html_lib
import logging
import os
import uuid

from flask import Blueprint, Response, jsonify, make_response, request
from pydantic import ValidationError

from api.schemas import DebugNowRequest, RoutingModeRequest, ServicioRequest
from services.dispatch_service import DispatchRetryRequiredError, DispatchService
from services.turnos_dashboard import (
    build_turnos_dashboard_data,
    parse_dashboard_date,
    render_turnos_dashboard_html,
)
from utils.config import get_settings
from utils.time_utils import format_datetime, get_simulated_now, now_bogota, set_simulated_now

logger = logging.getLogger(__name__)
dispatch_service = DispatchService()
rve_blueprint = Blueprint("rve_bridge", __name__)
VISUAL_ACCESS_KEY = get_settings().endpoint_api_key or os.getenv("VISUAL_ACCESS_KEY", "")


def _settings():
    return get_settings()


def _require_api_key():
    settings = _settings()
    if not settings.endpoint_api_key:
        return None

    provided_key = request.headers.get("X-Api-Key", "")
    if provided_key != settings.endpoint_api_key:
        return jsonify({"detail": "Invalid API Key"}), 401
    return None


def _require_test_clock():
    if not _settings().allow_test_clock:
        return jsonify({"detail": "Not found"}), 404
    return None


def _json_or_error():
    data = request.get_json(silent=True)
    if data is None:
        return None, (jsonify({"detail": "Invalid or missing JSON body"}), 400)
    return data, None


def _validation_error(exc: ValidationError):
    return jsonify({"detail": exc.errors()}), 422


def _internal_error(exc: Exception):
    logger.exception("Unhandled RVE error", exc_info=exc)
    return jsonify({"error": "Internal server error"}), 500


def _visual_access_granted() -> bool:
    return (
        request.args.get("clave", "") == VISUAL_ACCESS_KEY
        or request.cookies.get("rve_visual_access", "") == VISUAL_ACCESS_KEY
    )


def _visual_access_page():
    hidden_fields = "\n".join(
        f'<input type="hidden" name="{html_lib.escape(key)}" value="{html_lib.escape(value)}">'
        for key, value in request.args.items()
        if key != "clave"
    )
    page = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RVE - Acceso visual</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: #f6f8fb; color: #172033; }
    form { width: min(360px, calc(100vw - 32px)); background: white; border: 1px solid #d9e0ea; border-radius: 8px; padding: 20px; display: grid; gap: 12px; }
    h1 { margin: 0; font-size: 18px; }
    label { display: grid; gap: 6px; font-size: 13px; color: #617089; }
    input { height: 38px; border: 1px solid #d9e0ea; border-radius: 6px; padding: 0 10px; font-size: 15px; }
    button { height: 38px; border: 0; border-radius: 6px; background: #172033; color: white; font-weight: 650; cursor: pointer; }
  </style>
</head>
<body>
  <form method="get">
    <h1>Acceso visual RVE</h1>
    __HIDDEN_FIELDS__
    <label>Clave<input name="clave" type="password" autofocus></label>
    <button type="submit">Entrar</button>
  </form>
</body>
</html>""".replace("__HIDDEN_FIELDS__", hidden_fields)
    return Response(
        page,
        mimetype="text/html",
        status=401,
    )


@rve_blueprint.route("/rve/servicio", methods=["POST"])
def rve_servicio():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    data, error_response = _json_or_error()
    if error_response:
        return error_response

    try:
        servicio = ServicioRequest.model_validate(data)
    except ValidationError as exc:
        return _validation_error(exc)

    request_id = uuid.uuid4().hex[:8]
    logger.info(
        "endpoint.servicio.received request_id=%s autorizacion=%s fecha_servicio=%s",
        request_id,
        servicio.autorizacion,
        servicio.fecha_servicio,
    )
    try:
        outcome = dispatch_service.procesar_nuevo_servicio(servicio.model_dump(), request_id)
    except DispatchRetryRequiredError as exc:
        logger.warning(
            "endpoint.servicio.retry_required request_id=%s autorizacion=%s error=%s",
            request_id,
            servicio.autorizacion,
            str(exc),
        )
        return jsonify({"detail": str(exc)}), 503
    except Exception as exc:
        logger.exception(
            "endpoint.servicio.unhandled request_id=%s autorizacion=%s error_type=%s error=%s",
            request_id,
            servicio.autorizacion,
            type(exc).__name__,
            str(exc),
        )
        return jsonify(
            {
                "detail": "No fue posible confirmar el estado del servicio en Google Sheets. Reintente con la misma autorizacion."
            }
        ), 503

    logger.info(
        "endpoint.servicio.response request_id=%s autorizacion=%s decision=%s razon=%s",
        request_id,
        outcome.autorizacion,
        outcome.decision,
        outcome.razon,
    )
    return jsonify(
        {
            "autorizacion": outcome.autorizacion,
            "decision": outcome.decision,
            "id_turno": outcome.id_turno,
            "cedula_conductor": outcome.cedula_conductor,
            "nombre_conductor": outcome.nombre_conductor,
            "razon": outcome.razon,
            "timestamp": format_datetime(now_bogota()),
        }
    ), 200


@rve_blueprint.route("/rve/cancelacion", methods=["POST"])
def rve_cancelacion():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    autorizacion = request.args.get("autorizacion", "").strip()
    if not autorizacion:
        return jsonify({"detail": "Missing required query parameter: autorizacion"}), 400

    try:
        result = dispatch_service.cancelar_servicio(autorizacion, uuid.uuid4().hex[:8])
        return jsonify(
            {
                "autorizacion": autorizacion,
                "estado": result["estado"],
                "resultado": result["resultado"],
            }
        ), 200
    except Exception as exc:
        return _internal_error(exc)


@rve_blueprint.route("/rve/validar", methods=["POST"])
def rve_validar():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    try:
        return jsonify(dispatch_service.bloquear_servicios_proximos(uuid.uuid4().hex[:8])), 200
    except Exception as exc:
        return _internal_error(exc)


@rve_blueprint.route("/rve/config/enrutamiento", methods=["POST"])
def rve_config_enrutamiento():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    data, error_response = _json_or_error()
    if error_response:
        return error_response

    try:
        payload = RoutingModeRequest.model_validate(data)
    except ValidationError as exc:
        return _validation_error(exc)

    try:
        result = dispatch_service.configurar_tipo_enrutamiento(
            payload.tipo_enrutamiento,
            uuid.uuid4().hex[:8],
        )
        return jsonify(result), 200
    except Exception as exc:
        return _internal_error(exc)


@rve_blueprint.route("/rve/visual/turnos", methods=["GET"])
def rve_visual_turnos():
    """Vista Gantt/mapa de turnos y asignaciones actuales."""

    if not _visual_access_granted():
        return _visual_access_page()

    try:
        snapshot = dispatch_service.repository.load_dispatch_snapshot()
        snapshot.turnos = dispatch_service.repository.list_turnos(include_expired=True)
        data = build_turnos_dashboard_data(
            snapshot=snapshot,
            target_date=parse_dashboard_date(request.args.get("fecha")),
            departamento=request.args.get("departamento", ""),
            ciudad=request.args.get("ciudad", ""),
            servicio_filter=request.args.get("servicio", ""),
            tipo_servicio_filter=request.args.get("tipo_servicio", ""),
        )
        response = make_response(render_turnos_dashboard_html(data))
        response.mimetype = "text/html"
        if request.args.get("clave", "") == VISUAL_ACCESS_KEY:
            response.set_cookie("rve_visual_access", VISUAL_ACCESS_KEY, httponly=True, samesite="Lax")
        return response
    except Exception as exc:
        return _internal_error(exc)


@rve_blueprint.route("/rve/visual/turnos/data", methods=["GET"])
def rve_visual_turnos_data():
    """JSON de soporte para la vista de turnos."""

    if not _visual_access_granted():
        return jsonify({"detail": "Invalid visual access key"}), 401

    try:
        snapshot = dispatch_service.repository.load_dispatch_snapshot()
        snapshot.turnos = dispatch_service.repository.list_turnos(include_expired=True)
        data = build_turnos_dashboard_data(
            snapshot=snapshot,
            target_date=parse_dashboard_date(request.args.get("fecha")),
            departamento=request.args.get("departamento", ""),
            ciudad=request.args.get("ciudad", ""),
            servicio_filter=request.args.get("servicio", ""),
            tipo_servicio_filter=request.args.get("tipo_servicio", ""),
        )
        return jsonify(data), 200
    except Exception as exc:
        return _internal_error(exc)


@rve_blueprint.route("/rve/completar", methods=["POST"])
def rve_completar():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    autorizacion = request.args.get("autorizacion", "").strip()
    if not autorizacion:
        return jsonify({"detail": "Missing required query parameter: autorizacion"}), 400

    try:
        result = dispatch_service.completar_servicio(autorizacion, uuid.uuid4().hex[:8])
        return jsonify(
            {
                "autorizacion": autorizacion,
                "estado": result["estado"],
                "resultado": result["resultado"],
            }
        ), 200
    except Exception as exc:
        return _internal_error(exc)


@rve_blueprint.route("/rve/_debug/now", methods=["POST"])
def rve_debug_now():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    clock_error = _require_test_clock()
    if clock_error:
        return clock_error

    data, error_response = _json_or_error()
    if error_response:
        return error_response

    try:
        payload = DebugNowRequest.model_validate(data)
    except ValidationError as exc:
        return _validation_error(exc)

    value = set_simulated_now(payload.now_value)
    return jsonify(
        {
            "resultado": "OK",
            "hora_simulada": format_datetime(value) if value else "",
        }
    ), 200


@rve_blueprint.route("/rve/_debug/reset-lock", methods=["POST"])
def rve_debug_reset_lock():
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    clock_error = _require_test_clock()
    if clock_error:
        return clock_error

    try:
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
        return jsonify(
            {
                "resultado": "OK",
                "hora_simulada": format_datetime(get_simulated_now()),
            }
        ), 200
    except Exception as exc:
        return _internal_error(exc)


@rve_blueprint.route("/rve/_debug/snapshot/<autorizacion>", methods=["GET"])
def rve_debug_snapshot(autorizacion: str):
    auth_error = _require_api_key()
    if auth_error:
        return auth_error

    clock_error = _require_test_clock()
    if clock_error:
        return clock_error

    try:
        repository = dispatch_service.repository
        servicio = repository.get_servicio(autorizacion)
        preasignaciones = [
            item for item in repository.list_preasignaciones() if item.autorizacion == autorizacion
        ]
        return jsonify(
            {
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
        ), 200
    except Exception as exc:
        return _internal_error(exc)
