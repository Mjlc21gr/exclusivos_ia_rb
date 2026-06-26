"""Utilidades de tiempo para America/Bogota."""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

BOGOTA_TZ = ZoneInfo("America/Bogota")
SUPPORTED_FORMATS = ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S")
_SIMULATED_NOW: datetime | None = None


def now_bogota() -> datetime:
    """Retorna la hora actual en Bogota.

    ``RVE_FIXED_NOW`` permite simular escenarios locales sin esperar dias reales.
    """

    if _SIMULATED_NOW is not None:
        return _SIMULATED_NOW
    override = os.getenv("RVE_FIXED_NOW", "").strip()
    if override:
        parsed = parse_datetime(override)
        if parsed:
            return parsed
    return datetime.now(BOGOTA_TZ)


def set_simulated_now(value: str | datetime | None) -> datetime | None:
    """Define una hora simulada en memoria para pruebas locales."""

    global _SIMULATED_NOW
    if value in {None, ""}:
        _SIMULATED_NOW = None
        return None
    parsed = parse_datetime(value)
    _SIMULATED_NOW = parsed
    return _SIMULATED_NOW


def get_simulated_now() -> datetime | None:
    """Devuelve la hora simulada activa si existe."""

    return _SIMULATED_NOW


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parsea un datetime soportando formatos de Sheets y API."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=BOGOTA_TZ)
    for fmt in SUPPORTED_FORMATS:
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=BOGOTA_TZ)
        except ValueError:
            continue
    return None


def format_datetime(value: datetime | None) -> str:
    """Formatea datetimes al formato consistente de la API."""

    if value is None:
        return ""
    local_value = value if value.tzinfo else value.replace(tzinfo=BOGOTA_TZ)
    return local_value.astimezone(BOGOTA_TZ).strftime("%Y-%m-%d %H:%M:%S")
