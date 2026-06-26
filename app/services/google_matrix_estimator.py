"""Estimador local de costo de matrices Google sin llamadas externas."""

from __future__ import annotations

import contextvars
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from utils.time_utils import format_datetime, now_bogota


_CONTEXT: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "google_matrix_estimator_context",
    default={},
)
_RECORDS: list["MatrixEstimateRecord"] = []


@dataclass(frozen=True)
class MatrixEstimateRecord:
    """Registro de un solve que podria requerir matriz Google."""

    timestamp: str
    endpoint: str
    request_id: str
    autorizacion: str
    department: str
    services: int
    turns: int
    manual_blocks: int
    dynamic_services: int
    locked_services: int
    core_elements: int
    full_elements: int
    estimated_core_requests: int
    estimated_full_requests: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimation_enabled() -> bool:
    """Indica si el estimador debe registrar solves."""

    return os.getenv("ESTIMATE_GOOGLE_MATRIX_COST", "false").strip().lower() == "true"


def set_estimation_context(
    endpoint: str,
    request_id: str = "",
    autorizacion: str = "",
) -> contextvars.Token:
    """Fija contexto logico para los solves del request actual."""

    return _CONTEXT.set(
        {
            "endpoint": endpoint,
            "request_id": request_id,
            "autorizacion": autorizacion,
        }
    )


def reset_estimation_context(token: contextvars.Token) -> None:
    """Restaura el contexto previo."""

    _CONTEXT.reset(token)


def reset_estimates() -> None:
    """Limpia registros acumulados en el proceso."""

    _RECORDS.clear()


def get_estimates() -> list[dict[str, Any]]:
    """Devuelve una copia serializable de los registros."""

    return [record.to_dict() for record in _RECORDS]


def record_solve_estimate(
    *,
    services_count: int,
    turns_count: int,
    manual_blocks_count: int,
    dynamic_services_count: int,
    locked_services_count: int,
    department: str,
    max_elements_per_request: int = 625,
    timestamp: datetime | None = None,
) -> None:
    """Registra el tamano hipotetico de matriz para un solve."""

    if not estimation_enabled() or services_count <= 0 or turns_count <= 0:
        return

    core_elements = services_count * (services_count + turns_count)
    full_elements = core_elements + (turns_count * services_count) + (
        manual_blocks_count * (services_count + 2)
    )
    context = _CONTEXT.get({})
    chunk_size = max(1, max_elements_per_request)
    record = MatrixEstimateRecord(
        timestamp=format_datetime(timestamp or now_bogota()),
        endpoint=context.get("endpoint", ""),
        request_id=context.get("request_id", ""),
        autorizacion=context.get("autorizacion", ""),
        department=department,
        services=services_count,
        turns=turns_count,
        manual_blocks=manual_blocks_count,
        dynamic_services=dynamic_services_count,
        locked_services=locked_services_count,
        core_elements=core_elements,
        full_elements=full_elements,
        estimated_core_requests=math.ceil(core_elements / chunk_size),
        estimated_full_requests=math.ceil(full_elements / chunk_size),
    )
    _RECORDS.append(record)

