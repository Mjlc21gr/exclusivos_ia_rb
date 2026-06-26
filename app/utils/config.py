"""Configuracion centralizada por variables de entorno."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    """Configuracion global del sistema."""

    spreadsheet_id: str
    sheet_servicios: str
    sheet_turnos: str
    sheet_preasignaciones: str
    sheet_config: str
    sheet_datos_tecnicos: str
    endpoint_api_key: str
    average_speed_kmh: float
    buffer_minutes: int
    onsite_minutes: int
    max_radius_km: float
    pre_shift_travel_minutes: int
    horizon_days: int
    min_notice_minutes: int
    lock_timeout_seconds: int
    lock_retry_seconds: int
    lock_max_retries: int
    service_timeout_seconds: int
    sheets_http_timeout_seconds: int
    sheets_max_retries: int
    sheet_cache_ttl_seconds: int
    decision_engine: str
    ortools_time_limit_seconds: int
    ortools_local_search_seconds: int
    ortools_move_penalty_km: float
    ortools_first_solution_strategy: str
    ortools_local_search_metaheuristic: str
    gemini_service_account_file: str
    gemini_region: str
    gemini_model: str
    gemini_timeout_seconds: int
    datadog_enabled: bool
    allow_test_clock: bool
    datadog_api_key: str
    datadog_site: str
    datadog_service: str
    datadog_env: str
    datadog_version: str


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Carga configuracion una sola vez por proceso."""

    endpoint_api_key = os.getenv("ENDPOINT_API_KEY", "").strip()
    if not endpoint_api_key:
        endpoint_api_key = os.getenv("END_POINT_API_KEY", "").strip()

    lock_timeout_seconds_raw = os.getenv("LOCK_TIMEOUT_SECONDS", "").strip()
    if lock_timeout_seconds_raw:
        lock_timeout_seconds = int(lock_timeout_seconds_raw)
    else:
        legacy_lock_timeout_minutes = int(os.getenv("LOCK_TIMEOUT_MINUTES", "0") or "0")
        lock_timeout_seconds = legacy_lock_timeout_minutes * 60 if legacy_lock_timeout_minutes else 150

    return Settings(
        spreadsheet_id=os.getenv("SPREADSHEET_ID", ""),
        sheet_servicios=os.getenv("SHEET_SERVICIOS", "SERVICIOS"),
        sheet_turnos=os.getenv("SHEET_TURNOS", "TURNOS_TECNICOS"),
        sheet_preasignaciones=os.getenv("SHEET_PREASIGNACIONES", "PREASIGNACIONES"),
        sheet_config=os.getenv("SHEET_CONFIG", "CONFIG"),
        sheet_datos_tecnicos=os.getenv("SHEET_DATOS_TECNICOS", "DATOS_TECNICOS"),
        endpoint_api_key=endpoint_api_key,
        average_speed_kmh=float(os.getenv("AVERAGE_SPEED_KMH", "22")),
        buffer_minutes=int(os.getenv("BUFFER_SERVICIO_MINUTOS", "5")),
        onsite_minutes=int(os.getenv("ONSITE_MINUTES", "12")),
        max_radius_km=float(os.getenv("RADIO_MAX_KM", "25")),
        pre_shift_travel_minutes=int(os.getenv("PRE_SHIFT_TRAVEL_MINUTES", "40")),
        horizon_days=int(os.getenv("HORIZON_DAYS", "8")),
        min_notice_minutes=int(os.getenv("MIN_NOTICE_MINUTES", "150")),
        lock_timeout_seconds=lock_timeout_seconds,
        lock_retry_seconds=int(os.getenv("LOCK_RETRY_SECONDS", "5")),
        lock_max_retries=int(os.getenv("LOCK_MAX_RETRIES", "3")),
        service_timeout_seconds=int(os.getenv("SERVICE_TIMEOUT_SECONDS", "120")),
        sheets_http_timeout_seconds=int(os.getenv("SHEETS_HTTP_TIMEOUT_SECONDS", "12")),
        sheets_max_retries=int(os.getenv("SHEETS_MAX_RETRIES", "2")),
        sheet_cache_ttl_seconds=int(os.getenv("SHEET_CACHE_TTL_SECONDS", "300")),
        decision_engine=os.getenv("DECISION_ENGINE", "ortools").strip().lower(),
        ortools_time_limit_seconds=int(os.getenv("ORTOOLS_TIME_LIMIT_SECONDS", "8")),
        ortools_local_search_seconds=int(os.getenv("ORTOOLS_LOCAL_SEARCH_SECONDS", "7")),
        ortools_move_penalty_km=float(os.getenv("ORTOOLS_MOVE_PENALTY_KM", "2.0")),
        ortools_first_solution_strategy=os.getenv(
            "ORTOOLS_FIRST_SOLUTION_STRATEGY",
            "PARALLEL_CHEAPEST_INSERTION",
        ).strip().upper(),
        ortools_local_search_metaheuristic=os.getenv(
            "ORTOOLS_LOCAL_SEARCH_METAHEURISTIC",
            "GUIDED_LOCAL_SEARCH",
        ).strip().upper(),
        gemini_service_account_file=os.getenv("GEMINI_SERVICE_ACCOUNT_FILE", "").strip(),
        gemini_region=os.getenv("GEMINI_REGION", "us-central1").strip(),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
        gemini_timeout_seconds=int(os.getenv("GEMINI_TIMEOUT_SECONDS", "15")),
        datadog_enabled=os.getenv("DATADOG_ENABLED", "false").lower() == "true",
        allow_test_clock=os.getenv("ALLOW_TEST_CLOCK", "false").lower() == "true",
        datadog_api_key=os.getenv("DD_API_KEY", ""),
        datadog_site=os.getenv("DD_SITE", "datadoghq.com"),
        datadog_service=os.getenv("DD_SERVICE", "rve-microservice"),
        datadog_env=os.getenv("DD_ENV", "dev"),
        datadog_version=os.getenv("DD_VERSION", "latest"),
    )
