"""Repositorio de Google Sheets para el sistema RVE."""

from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import google.auth.transport.requests as google_auth_requests
import gspread
from gspread.exceptions import APIError
from google.auth import default as google_auth_default
from google.auth.exceptions import RefreshError
from google.oauth2 import service_account

from or_engine.models import PreasignacionPlan, ServicioPlan, TurnoPlan
from utils.config import get_settings
from utils.constants import (
    ANALISIS_DATOS_ERRONEOS,
    ANALISIS_FUERA_RADIO_MAXIMO,
    ANALISIS_INSERCION_NO_FACTIBLE,
    ANALISIS_NO_LLEGA_TIEMPO,
    ANALISIS_OTRO_RECHAZO,
    ANALISIS_SATURACION_TURNO,
    ANALISIS_SERVICIO_EMERGENCIA,
    ANALISIS_SERVICIO_FUERA_LIMITES_TURNO,
    ANALISIS_SIN_DISPONIBILIDAD_TURNOS,
    ESTADOS_MANUALES,
    ESTADOS_TERMINALES,
    ESTADO_ASIGNADO_FINAL,
    ESTADO_CANCELADO,
    ESTADO_COMPLETADO,
    ESTADO_MANUAL,
    ESTADO_PREASIGNADO,
    ESTADO_RECHAZADO_RVE,
    ESTADO_RECIBIDO,
    ESTADO_URGENTE_GESTIONAR_MANUAL,
    NOMBRE_CONDUCTOR_PENDIENTE,
    PREASIGNACION_ACTIVA,
    PREASIGNACION_CANCELADA,
    PREASIGNACION_CONGELADA,
    SERVICIOS_ANALISIS_COLUMN,
    SERVICIOS_ANALISIS_INDEX,
    TIPO_ENRUTAMIENTO_AUTOMATICO,
    TIPOS_ENRUTAMIENTO,
)
from utils.parsing import format_coordinate_for_sheet, normalize_header, parse_coordinate
from utils.time_utils import format_datetime, now_bogota, parse_datetime

logger = logging.getLogger(__name__)
DEFAULT_APP_SERVICE_ACCOUNT = "service-account.json"


@dataclass
class DispatchSnapshot:
    """Snapshot consistente de las hojas usadas por el dispatch."""

    servicios: List[ServicioPlan]
    preasignaciones: List[PreasignacionPlan]
    turnos: List[TurnoPlan]
    servicios_by_auth: Dict[str, ServicioPlan]
    turnos_by_id: Dict[str, TurnoPlan]
    preasignacion_vigente_by_auth: Dict[str, PreasignacionPlan]
    next_servicio_row_index: int


class GoogleSheetsRepository:
    """Encapsula lectura y escritura sobre Google Sheets."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._client: Optional[gspread.Client] = None
        self._spreadsheet = None
        self._worksheet_cache: Dict[str, object] = {}
        self._row_cache: Dict[str, List[dict]] = {}
        self._row_cache_at: Dict[str, float] = {}
        self._header_cache: Dict[str, List[str]] = {}
        self._header_cache_at: Dict[str, float] = {}
        self._request_deadline: ContextVar[float | None] = ContextVar(
            "google_sheets_request_deadline",
            default=None,
        )

    def get_client(self) -> gspread.Client:
        """Crea el cliente de gspread una sola vez."""

        if self._client is not None:
            return self._client

        service_account_file = self._resolve_service_account_file()
        if service_account_file:
            logger.info("sheets.auth.service_account_file path=%s", service_account_file)
            credentials = service_account.Credentials.from_service_account_file(
                service_account_file,
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
        else:
            logger.info("sheets.auth.application_default_credentials")
            credentials, _ = google_auth_default(
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )

        session = google_auth_requests.AuthorizedSession(credentials)
        original_request = session.request

        def request_with_timeout(method, url, **kwargs):
            timeout = kwargs.get("timeout")
            if timeout is None:
                kwargs["timeout"] = self._resolve_http_timeout_seconds()
            return original_request(method, url, **kwargs)

        session.request = request_with_timeout
        self._client = gspread.Client(auth=credentials, session=session)
        return self._client

    def push_request_deadline(self, deadline_at: float) -> Token:
        """Asocia un deadline monotonic al contexto del request actual."""

        return self._request_deadline.set(deadline_at)

    def pop_request_deadline(self, token: Token) -> None:
        """Restaura el deadline previo del contexto actual."""

        self._request_deadline.reset(token)

    def _resolve_service_account_file(self) -> Optional[str]:
        """Resuelve el JSON de credenciales incluido o configurado para Sheets."""

        candidates = []
        configured_path = os.getenv("SERVICE_ACCOUNT_FILE", "").strip()
        if configured_path:
            candidates.append(Path(configured_path))

        current_file = Path(__file__).resolve()
        candidates.extend(
            [
                current_file.parents[1] / DEFAULT_APP_SERVICE_ACCOUNT,
            ]
        )

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)
        return None

    def spreadsheet(self):
        """Obtiene la planilla configurada."""

        if not self.settings.spreadsheet_id:
            raise RuntimeError("SPREADSHEET_ID not configured")
        if self._spreadsheet is None:
            logger.info("sheets.spreadsheet.open key=%s", self.settings.spreadsheet_id)
            self._spreadsheet = self._call_with_retry(
                self.get_client().open_by_key,
                self.settings.spreadsheet_id,
            )
        return self._spreadsheet

    def worksheet(self, name: str):
        """Obtiene una hoja por nombre."""

        if name not in self._worksheet_cache:
            logger.info("sheets.worksheet.open name=%s", name)
            self._worksheet_cache[name] = self._call_with_retry(self.spreadsheet().worksheet, name)
        return self._worksheet_cache[name]

    def ensure_config_schema(self) -> None:
        """Garantiza que CONFIG tenga lock y tipo de enrutamiento."""

        try:
            worksheet = self.worksheet(self.settings.sheet_config)
        except gspread.WorksheetNotFound:  # type: ignore[name-defined]
            spreadsheet = self.spreadsheet()
            worksheet = spreadsheet.add_worksheet(title=self.settings.sheet_config, rows=10, cols=5)
            self._worksheet_cache[self.settings.sheet_config] = worksheet

        values = self._call_with_retry(worksheet.get, "A1:D2")
        headers = values[0] if values else []
        data_row = values[1] if len(values) > 1 else []
        expected_headers = ["LOCK_OWNER", "LOCK_EXPIRES_AT", "LOCK_UPDATED_AT", "TIPO_ENRUTAMIENTO"]
        normalized_mode = (
            str(data_row[3]).strip().upper()
            if len(data_row) > 3
            else ""
        )
        mode_value = normalized_mode if normalized_mode in TIPOS_ENRUTAMIENTO else TIPO_ENRUTAMIENTO_AUTOMATICO

        updates = []
        for index, header in enumerate(expected_headers, start=1):
            current = normalize_header(headers[index - 1]) if len(headers) >= index else ""
            if current != header:
                updates.append(
                    {
                        "range": gspread.utils.rowcol_to_a1(1, index),
                        "values": [[header]],
                    }
                )
        if normalized_mode != mode_value:
            updates.append({"range": "D2", "values": [[mode_value]]})

        if updates:
            logger.warning("sheets.config.schema_update cells=%s", len(updates))
            self._call_with_retry(worksheet.batch_update, updates)
            self._header_cache[self.settings.sheet_config] = expected_headers
            self._invalidate_sheet_rows_cache(self.settings.sheet_config)

    def get_routing_mode(self) -> str:
        """Lee el tipo de enrutamiento actual desde CONFIG."""

        self.ensure_config_schema()
        worksheet = self.worksheet(self.settings.sheet_config)
        values = self._call_with_retry(worksheet.get, "D2")
        value = values[0][0] if values and values[0] else ""
        normalized = str(value or "").strip().upper()
        if normalized not in TIPOS_ENRUTAMIENTO:
            return TIPO_ENRUTAMIENTO_AUTOMATICO
        return normalized

    def set_routing_mode(self, mode: str) -> str:
        """Actualiza el tipo de enrutamiento en CONFIG."""

        normalized = str(mode or "").strip().upper()
        if normalized not in TIPOS_ENRUTAMIENTO:
            raise ValueError("tipo_enrutamiento debe ser AUTOMATICO o MANUAL")
        self.ensure_config_schema()
        worksheet = self.worksheet(self.settings.sheet_config)
        logger.warning("sheets.config.routing_mode_update mode=%s", normalized)
        self._call_with_retry(worksheet.update, "D2", [[normalized]])
        self._invalidate_sheet_rows_cache(self.settings.sheet_config)
        return normalized

    def read_headers(self, sheet_name: str) -> List[str]:
        """Lee y cachea los encabezados de una hoja."""

        if sheet_name not in self._header_cache:
            logger.info("sheets.headers.load sheet=%s", sheet_name)
            worksheet = self.worksheet(sheet_name)
            self._header_cache[sheet_name] = [
                normalize_header(value)
                for value in self._call_with_retry(worksheet.row_values, 1)
            ]
        return self._header_cache[sheet_name]

    def read_rows(self, sheet_name: str, force_refresh: bool = False) -> List[dict]:
        """Lee una hoja completa y la devuelve como lista de filas."""

        if force_refresh:
            self._invalidate_sheet_rows_cache(sheet_name)

        should_cache_rows = sheet_name == self.settings.sheet_turnos
        if should_cache_rows and self._cache_expired(self._row_cache_at.get(sheet_name)):
            self._row_cache.pop(sheet_name, None)
        if should_cache_rows and sheet_name in self._row_cache:
            logger.info("sheets.rows.cache_hit sheet=%s rows=%s", sheet_name, len(self._row_cache[sheet_name]))
            return self._row_cache[sheet_name]

        logger.info("sheets.rows.load sheet=%s", sheet_name)
        worksheet = self.worksheet(sheet_name)
        values = self._call_with_retry(worksheet.get_all_values)
        if not values:
            return []

        raw_headers = values[0]
        headers = [normalize_header(header) for header in raw_headers]
        self._header_cache[sheet_name] = headers
        rows: List[dict] = []
        for row_index, row in enumerate(values[1:], start=2):
            record = {"_row": row_index}
            for index, header in enumerate(headers):
                record[header] = row[index] if index < len(row) else ""
            rows.append(record)
        if should_cache_rows:
            self._row_cache[sheet_name] = rows
            self._row_cache_at[sheet_name] = time.monotonic()
        return rows

    def list_servicios(self) -> List[ServicioPlan]:
        """Carga la hoja de servicios completa."""

        return [ServicioPlan.from_sheet_row(row) for row in self.read_rows(self.settings.sheet_servicios)]

    def list_turnos(self, include_expired: bool = False) -> List[TurnoPlan]:
        """Carga turnos de la hoja; por defecto solo vigentes y futuros."""

        now_value = now_bogota()
        turnos = [TurnoPlan.from_sheet_row(row) for row in self.read_rows(self.settings.sheet_turnos)]
        if include_expired:
            return [turno for turno in turnos if turno.fecha_fin_turno]
        return [turno for turno in turnos if turno.fecha_fin_turno and turno.fecha_fin_turno >= now_value]

    def list_preasignaciones(self) -> List[PreasignacionPlan]:
        """Carga la hoja de preasignaciones."""

        return [
            PreasignacionPlan.from_sheet_row(row)
            for row in self.read_rows(self.settings.sheet_preasignaciones)
        ]

    def list_datos_tecnicos(self) -> List[dict]:
        """Carga la hoja DATOS_TECNICOS como lista de dicts crudos.

        Retorna todas las filas con sus headers normalizados.
        Si la hoja no existe, retorna lista vacia sin error.
        """

        try:
            rows = self.read_rows(self.settings.sheet_datos_tecnicos)
            return rows
        except Exception:
            logger.warning("sheets.datos_tecnicos.not_found sheet=%s", self.settings.sheet_datos_tecnicos)
            return []

    def find_servicio_in_snapshot(
        self,
        autorizacion: str,
        servicios: Iterable[ServicioPlan],
    ) -> Optional[ServicioPlan]:
        """Busca un servicio dentro de un snapshot ya cargado."""

        for servicio in servicios:
            if servicio.autorizacion == autorizacion:
                return servicio
        return None

    def get_servicio(self, autorizacion: str) -> Optional[ServicioPlan]:
        """Busca un servicio por autorizacion."""

        for servicio in self.list_servicios():
            if servicio.autorizacion == autorizacion:
                return servicio
        return None

    def load_dispatch_snapshot(self) -> DispatchSnapshot:
        """Carga el estado necesario para un request de dispatch."""

        started = time.monotonic()
        servicios_rows = self.read_rows(self.settings.sheet_servicios, force_refresh=True)
        preasignaciones_rows = self.read_rows(self.settings.sheet_preasignaciones, force_refresh=True)
        servicios = [ServicioPlan.from_sheet_row(row) for row in servicios_rows]
        preasignaciones = [PreasignacionPlan.from_sheet_row(row) for row in preasignaciones_rows]
        turnos = self.list_turnos()
        servicios_by_auth = {servicio.autorizacion: servicio for servicio in servicios}
        preasignacion_vigente_by_auth = self._select_current_preasignaciones(preasignaciones)
        max_row = max((servicio.row_index or 1) for servicio in servicios) if servicios else 1
        snapshot = DispatchSnapshot(
            servicios=servicios,
            preasignaciones=preasignaciones,
            turnos=turnos,
            servicios_by_auth=servicios_by_auth,
            turnos_by_id={turno.id_turno: turno for turno in turnos},
            preasignacion_vigente_by_auth=preasignacion_vigente_by_auth,
            next_servicio_row_index=max_row + 1,
        )
        logger.warning(
            "sheets.snapshot servicios_rows=%s preasignaciones_rows=%s turnos=%s duration_ms=%s",
            len(servicios_rows),
            len(preasignaciones_rows),
            len(turnos),
            int((time.monotonic() - started) * 1000),
        )
        return snapshot

    def build_servicio_from_payload(
        self,
        payload: dict,
        row_index: int,
        estado_operacion: str = ESTADO_RECIBIDO,
        estado_tecnico: str = ESTADO_RECIBIDO,
    ) -> ServicioPlan:
        """Construye el servicio equivalente a la fila append sin releer Sheets."""

        row = {
            "AUTORIZACION": payload.get("autorizacion", ""),
            "CASO": payload.get("caso", ""),
            "CIUDAD_ORIGEN": payload.get("ciudad_origen", ""),
            "CIUDAD_DESTINO": payload.get("ciudad_destino", ""),
            "DIRECCION_ORIGEN": payload.get("direccion_origen", ""),
            "DIRECCION_DE_DESTINO": payload.get("direccion_destino", ""),
            "FECHA_SERVICIO": payload.get("fecha_servicio", ""),
            "SERVICIO": payload.get("tipo_servicio", ""),
            "TIPO_SERVICIO": payload.get("modalidad_servicio", "PROGRAMADO"),
            "DEPARTAMENTO": payload.get("departamento", ""),
            "LATITUD_SERVICIO_ORIGEN": format_coordinate_for_sheet(payload.get("lat_origen", "")),
            "LONGITUD_SERVICIO_ORIGEN": format_coordinate_for_sheet(payload.get("lng_origen", "")),
            "LATITUD_SERVICIO_DESTINO": format_coordinate_for_sheet(payload.get("lat_destino", "")),
            "LONGITUD_SERVICIO_DESTINO": format_coordinate_for_sheet(payload.get("lng_destino", "")),
            "ESTADO_DEL_SERVICIO_OPERACION": estado_operacion,
            "ESTADO_DEL_SERVICIO_TECNICO": estado_tecnico,
            "ID_TURNO": "",
            "CEDULA_CONDUCTOR": "",
            "NOMBRE_CONDUCTOR": "",
            "CORREOS": payload.get("correos", ""),
            "_row": row_index,
        }
        return ServicioPlan.from_sheet_row(row)

    def append_servicio_recibido(self, payload: dict) -> None:
        """Crea el servicio en Sheets con estado interno RECIBIDO."""

        worksheet = self.worksheet(self.settings.sheet_servicios)
        headers = self.read_headers(self.settings.sheet_servicios)
        row_values = self._build_servicio_row(
            headers=headers,
            payload=payload,
            estado_operacion=ESTADO_RECIBIDO,
            estado_tecnico=ESTADO_RECIBIDO,
            id_turno="",
            cedula_conductor="",
            nombre_conductor="",
        )
        logger.info("sheets.servicio.append autorizacion=%s", payload.get("autorizacion"))
        self._call_with_retry(worksheet.append_row, row_values)
        self._invalidate_sheet_rows_cache(self.settings.sheet_servicios)

    def append_servicio_manual(self, payload: dict) -> None:
        """Crea el servicio en Sheets en estado MANUAL."""

        worksheet = self.worksheet(self.settings.sheet_servicios)
        headers = self.read_headers(self.settings.sheet_servicios)
        row_values = self._build_servicio_row(
            headers=headers,
            payload=payload,
            estado_operacion=ESTADO_MANUAL,
            estado_tecnico=ESTADO_MANUAL,
            id_turno="",
            cedula_conductor="",
            nombre_conductor="",
        )
        logger.warning("sheets.servicio.append_manual autorizacion=%s", payload.get("autorizacion"))
        self._call_with_retry(worksheet.append_row, row_values)
        self._invalidate_sheet_rows_cache(self.settings.sheet_servicios)

    def ensure_servicio_rechazado(
        self,
        payload: dict,
        razon: str,
        skip_lookup: bool = False,
        analisis: Optional[str] = None,
    ) -> bool:
        """Garantiza que un servicio quede persistido como rechazado."""

        existente = None
        if not skip_lookup:
            try:
                existente = self.get_servicio(payload["autorizacion"])
            except Exception as exc:
                logger.exception(
                    "sheets.servicio.ensure_rechazado.lookup_error autorizacion=%s error_type=%s error=%s",
                    payload.get("autorizacion"),
                    type(exc).__name__,
                    str(exc),
                )

        analisis_rechazo = analisis or self.classify_rejection_analysis(
            payload=payload,
            servicio=existente,
            razon=razon,
        )
        if existente and existente.estado_operacion in ESTADOS_TERMINALES:
            return True
        if existente:
            try:
                if existente.row_index:
                    return self.update_servicio_estado_by_row(
                        existente.row_index,
                        ESTADO_RECHAZADO_RVE,
                        cedula_conductor="",
                        nombre_conductor="",
                        id_turno="",
                        correos="",
                        analisis=analisis_rechazo,
                    )
                return self.update_servicio_estado(
                    existente.autorizacion,
                    ESTADO_RECHAZADO_RVE,
                    cedula_conductor="",
                    nombre_conductor="",
                    id_turno="",
                    correos="",
                    analisis=analisis_rechazo,
                )
            except Exception as exc:
                logger.exception(
                    "sheets.servicio.ensure_rechazado.update_error autorizacion=%s error_type=%s error=%s",
                    payload.get("autorizacion"),
                    type(exc).__name__,
                    str(exc),
                )
                return False

        try:
            worksheet = self.worksheet(self.settings.sheet_servicios)
            headers = self.read_headers(self.settings.sheet_servicios)
            row_values = self._build_servicio_row(
                headers=headers,
                payload=payload,
                estado_operacion=ESTADO_RECHAZADO_RVE,
                estado_tecnico=ESTADO_RECHAZADO_RVE,
                id_turno="",
                cedula_conductor="",
                nombre_conductor="",
                override_observaciones=razon,
                analisis=analisis_rechazo,
            )
            logger.warning(
                "sheets.servicio.append_rechazado autorizacion=%s razon=%s",
                payload.get("autorizacion"),
                razon,
            )
            self._call_with_retry(worksheet.append_row, row_values)
            self._invalidate_sheet_rows_cache(self.settings.sheet_servicios)
            return True
        except Exception as exc:
            logger.exception(
                "sheets.servicio.ensure_rechazado.append_error autorizacion=%s error_type=%s error=%s razon=%s",
                payload.get("autorizacion"),
                type(exc).__name__,
                str(exc),
                razon,
            )
            return False

    def _build_servicio_row(
        self,
        headers: List[str],
        payload: dict,
        estado_operacion: str,
        estado_tecnico: str,
        id_turno: str,
        cedula_conductor: str,
        nombre_conductor: str,
        override_observaciones: str | None = None,
        analisis: Optional[str] = None,
    ) -> List[str]:
        """Construye la fila a persistir en SERVICIOS."""

        fecha_creacion = payload.get("fecha_creacion_servicio") or format_datetime(now_bogota())
        fecha_recepcion = payload.get("fecha_recepcion_rb") or format_datetime(now_bogota())
        observaciones = override_observaciones if override_observaciones is not None else payload.get("observaciones", "")

        row_by_header = {
            "AUTORIZACION": payload.get("autorizacion", ""),
            "CASO": payload.get("caso", ""),
            "PLACA": payload.get("placa", ""),
            "CIUDAD_ORIGEN": payload.get("ciudad_origen", ""),
            "CIUDAD_DESTINO": payload.get("ciudad_destino", ""),
            "DIRECCION_ORIGEN": payload.get("direccion_origen", ""),
            "DIRECCION_DE_DESTINO": payload.get("direccion_destino", ""),
            "FECHA_SERVICIO": payload.get("fecha_servicio", ""),
            "CEDULA_ASEGURADO": payload.get("cedula_asegurado", ""),
            "ASEGURADO": payload.get("asegurado", ""),
            "CELULAR_ASEGURADO": payload.get("celular_asegurado", ""),
            "CLV": payload.get("clv", ""),
            "ID_TURNO": id_turno,
            "CEDULA_CONDUCTOR": cedula_conductor,
            "NOMBRE_CONDUCTOR": nombre_conductor,
            "ESTADO_DEL_SERVICIO_OPERACION": estado_operacion,
            "ESTADO_DEL_SERVICIO_TECNICO": estado_tecnico,
            "OBSERVACIONES": observaciones,
            "TIPO_CIUDAD": payload.get("tipo_ciudad", ""),
            "TIPO_FALLIDO": payload.get("tipo_fallido", ""),
            "FECHA_CREACION_SERVICIO": fecha_creacion,
            "FECHA_RECEPCION_RUTA_BOLIVAR": fecha_recepcion,
            "LATITUD_SERVICIO_ORIGEN": format_coordinate_for_sheet(payload.get("lat_origen", "")),
            "LONGITUD_SERVICIO_ORIGEN": format_coordinate_for_sheet(payload.get("lng_origen", "")),
            "LATITUD_SERVICIO_DESTINO": format_coordinate_for_sheet(payload.get("lat_destino", "")),
            "LONGITUD_SERVICIO_DESTINO": format_coordinate_for_sheet(payload.get("lng_destino", "")),
            "SERVICIO": payload.get("tipo_servicio", ""),
            "TIPO_SERVICIO": payload.get("modalidad_servicio", "PROGRAMADO"),
            "DEPARTAMENTO": payload.get("departamento", ""),
            "EVIDENCIAS": payload.get("evidencias", ""),
            "CORREOS": payload.get("correos", ""),
        }
        self._log_missing_headers(
            headers,
            {
                "AUTORIZACION",
                "ESTADO_DEL_SERVICIO_OPERACION",
                "ESTADO_DEL_SERVICIO_TECNICO",
                "ID_TURNO",
                "CEDULA_CONDUCTOR",
                "NOMBRE_CONDUCTOR",
                "CORREOS",
            },
            "build_servicio_row",
        )
        row_values = [row_by_header.get(header, "") for header in headers]
        if analisis is not None:
            while len(row_values) <= SERVICIOS_ANALISIS_INDEX:
                row_values.append("")
            row_values[SERVICIOS_ANALISIS_INDEX] = analisis
        return row_values

    def classify_rejection_analysis(
        self,
        payload: Optional[dict] = None,
        servicio: Optional[ServicioPlan] = None,
        razon: str = "",
        analysis_code: str = "",
    ) -> str:
        """Clasifica rechazos para la columna fija AH de SERVICIOS."""

        if self._has_invalid_rejection_coordinates(payload, servicio):
            return ANALISIS_DATOS_ERRONEOS
        if self._is_emergency_rejection(payload, servicio):
            return ANALISIS_SERVICIO_EMERGENCIA
        if analysis_code in {
            ANALISIS_FUERA_RADIO_MAXIMO,
            ANALISIS_SATURACION_TURNO,
            ANALISIS_NO_LLEGA_TIEMPO,
            ANALISIS_INSERCION_NO_FACTIBLE,
        }:
            return analysis_code
        razon_text = str(razon)
        if "No existe turno compatible" in razon_text:
            return ANALISIS_SIN_DISPONIBILIDAD_TURNOS
        if "Servicio fuera del radio maximo" in razon_text:
            return ANALISIS_FUERA_RADIO_MAXIMO
        if (
            "Servicio fuera de los limites del turno" in razon_text
            or "Servicio fuera de los límites del turno" in razon_text
        ):
            return ANALISIS_SERVICIO_FUERA_LIMITES_TURNO
        if "No existe insercion factible" in razon_text:
            return ANALISIS_INSERCION_NO_FACTIBLE
        return ANALISIS_OTRO_RECHAZO

    def _has_invalid_rejection_coordinates(
        self,
        payload: Optional[dict],
        servicio: Optional[ServicioPlan],
    ) -> bool:
        values = []
        if payload:
            values.extend(
                [
                    payload.get("lat_origen"),
                    payload.get("lng_origen"),
                    payload.get("lat_destino"),
                    payload.get("lng_destino"),
                ]
            )
        elif servicio:
            values.extend(
                [
                    servicio.origen.lat,
                    servicio.origen.lng,
                    servicio.destino.lat,
                    servicio.destino.lng,
                ]
            )
        if len(values) != 4:
            return True
        return any(parse_coordinate(value) == 0.0 for value in values)

    def _is_emergency_rejection(
        self,
        payload: Optional[dict],
        servicio: Optional[ServicioPlan],
    ) -> bool:
        fecha_servicio = None
        if payload:
            fecha_servicio = parse_datetime(payload.get("fecha_servicio"))
        if fecha_servicio is None and servicio:
            fecha_servicio = servicio.fecha_servicio
        if fecha_servicio is None:
            return False
        min_allowed = now_bogota() + timedelta(minutes=self.settings.min_notice_minutes)
        return fecha_servicio < min_allowed

    def update_servicio_estado(
        self,
        autorizacion: str,
        nuevo_estado: str,
        cedula_conductor: Optional[str] = None,
        nombre_conductor: Optional[str] = None,
        id_turno: Optional[str] = None,
        correos: Optional[str] = None,
        analisis: Optional[str] = None,
    ) -> bool:
        """Actualiza el estado del servicio sin tocar otros campos humanos."""

        servicio = self.get_servicio(autorizacion)
        if not servicio or not servicio.row_index:
            return False

        worksheet = self.worksheet(self.settings.sheet_servicios)
        header_map = self._header_map(worksheet)
        updates = {
            "ESTADO_DEL_SERVICIO_OPERACION": nuevo_estado,
            "ESTADO_DEL_SERVICIO_TECNICO": nuevo_estado,
        }
        if id_turno is not None:
            updates["ID_TURNO"] = id_turno
        if cedula_conductor is not None:
            updates["CEDULA_CONDUCTOR"] = cedula_conductor
        if nombre_conductor is not None:
            updates["NOMBRE_CONDUCTOR"] = nombre_conductor
        if correos is not None:
            updates["CORREOS"] = correos
        logger.info("sheets.servicio.update autorizacion=%s estado=%s", autorizacion, nuevo_estado)
        fixed_updates = self._analisis_fixed_update(servicio.row_index, analisis)
        self._update_row(worksheet, servicio.row_index, header_map, updates, fixed_updates=fixed_updates)
        self._invalidate_sheet_rows_cache(self.settings.sheet_servicios)
        return True

    def update_servicio_estado_by_row(
        self,
        row_index: int,
        nuevo_estado: str,
        cedula_conductor: Optional[str] = None,
        nombre_conductor: Optional[str] = None,
        id_turno: Optional[str] = None,
        correos: Optional[str] = None,
        analisis: Optional[str] = None,
    ) -> bool:
        """Actualiza el estado del servicio con una fila ya conocida."""

        worksheet = self.worksheet(self.settings.sheet_servicios)
        header_map = self._header_map(worksheet)
        updates = {
            "ESTADO_DEL_SERVICIO_OPERACION": nuevo_estado,
            "ESTADO_DEL_SERVICIO_TECNICO": nuevo_estado,
        }
        if id_turno is not None:
            updates["ID_TURNO"] = id_turno
        if cedula_conductor is not None:
            updates["CEDULA_CONDUCTOR"] = cedula_conductor
        if nombre_conductor is not None:
            updates["NOMBRE_CONDUCTOR"] = nombre_conductor
        if correos is not None:
            updates["CORREOS"] = correos
        logger.info("sheets.servicio.update_by_row row=%s estado=%s", row_index, nuevo_estado)
        fixed_updates = self._analisis_fixed_update(row_index, analisis)
        self._update_row(worksheet, row_index, header_map, updates, fixed_updates=fixed_updates)
        self._invalidate_sheet_rows_cache(self.settings.sheet_servicios)
        return True

    def _analisis_fixed_update(
        self,
        row_index: int,
        analisis: Optional[str],
    ) -> Optional[Dict[str, str]]:
        if analisis is None:
            return None
        return {f"{SERVICIOS_ANALISIS_COLUMN}{row_index}": analisis}

    def upsert_preasignacion(
        self,
        autorizacion: str,
        turno: TurnoPlan,
        estado_preasignacion: str = PREASIGNACION_ACTIVA,
        orden_en_ruta: int = 1,
    ) -> bool:
        """Crea o actualiza la preasignacion activa de un servicio."""

        worksheet = self.worksheet(self.settings.sheet_preasignaciones)
        header_map = self._header_map(worksheet)
        existentes = sorted(
            self.list_preasignaciones(),
            key=lambda item: item.row_index or 0,
            reverse=True,
        )
        fecha_preasignacion = format_datetime(now_bogota())

        for preasignacion in existentes:
            if preasignacion.autorizacion != autorizacion:
                continue
            updates = {
                "ID_TURNO": turno.id_turno,
                "CEDULA_CONDUCTOR": turno.cedula_conductor,
                "NOMBRE_TECNICO_PREASIGNACION": turno.nombre_conductor,
                "FECHA_PREASIGNACION": fecha_preasignacion,
                "ESTADO_PREASIGNACION": estado_preasignacion,
                "ORDEN_EN_RUTA": str(orden_en_ruta),
            }
            logger.info(
                "sheets.preasignacion.update autorizacion=%s turno=%s estado=%s",
                autorizacion,
                turno.id_turno,
                estado_preasignacion,
            )
            self._update_row(worksheet, preasignacion.row_index, header_map, updates)
            return True

        headers = self.read_headers(self.settings.sheet_preasignaciones)
        numeric_ids = [
            int(item.id_preasignacion)
            for item in existentes
            if str(item.id_preasignacion).isdigit()
        ]
        next_id = str((max(numeric_ids) if numeric_ids else 0) + 1)
        row_by_header = {
            "ID_PREASIGNACION": next_id,
            "AUTORIZACION": autorizacion,
            "ID_TURNO": turno.id_turno,
            "CEDULA_CONDUCTOR": turno.cedula_conductor,
            "NOMBRE_TECNICO_PREASIGNACION": turno.nombre_conductor,
            "FECHA_PREASIGNACION": fecha_preasignacion,
            "ESTADO_PREASIGNACION": estado_preasignacion,
            "ORDEN_EN_RUTA": str(orden_en_ruta),
        }
        logger.info(
            "sheets.preasignacion.append autorizacion=%s turno=%s estado=%s",
            autorizacion,
            turno.id_turno,
            estado_preasignacion,
        )
        self._call_with_retry(worksheet.append_row, [row_by_header.get(header, "") for header in headers])
        self._invalidate_sheet_rows_cache(self.settings.sheet_preasignaciones)
        return True

    def upsert_manual_preasignacion(
        self,
        autorizacion: str,
        id_turno: str,
        cedula_conductor: str,
        nombre_conductor: str,
        estado_preasignacion: str = PREASIGNACION_CONGELADA,
        orden_en_ruta: int = 1,
    ) -> bool:
        """Sincroniza PREASIGNACIONES para servicios gestionados manualmente."""

        if not id_turno:
            return False

        worksheet = self.worksheet(self.settings.sheet_preasignaciones)
        header_map = self._header_map(worksheet)
        existentes = sorted(
            self.list_preasignaciones(),
            key=lambda item: item.row_index or 0,
            reverse=True,
        )
        fecha_preasignacion = format_datetime(now_bogota())
        updates = {
            "ID_TURNO": id_turno,
            "CEDULA_CONDUCTOR": cedula_conductor,
            "NOMBRE_TECNICO_PREASIGNACION": nombre_conductor,
            "FECHA_PREASIGNACION": fecha_preasignacion,
            "ESTADO_PREASIGNACION": estado_preasignacion,
            "ORDEN_EN_RUTA": str(orden_en_ruta),
        }

        for preasignacion in existentes:
            if preasignacion.autorizacion != autorizacion:
                continue
            logger.info(
                "sheets.preasignacion.manual_update autorizacion=%s turno=%s estado=%s",
                autorizacion,
                id_turno,
                estado_preasignacion,
            )
            self._update_row(worksheet, preasignacion.row_index, header_map, updates)
            self._invalidate_sheet_rows_cache(self.settings.sheet_preasignaciones)
            return True

        headers = self.read_headers(self.settings.sheet_preasignaciones)
        numeric_ids = [
            int(item.id_preasignacion)
            for item in existentes
            if str(item.id_preasignacion).isdigit()
        ]
        next_id = str((max(numeric_ids) if numeric_ids else 0) + 1)
        row_by_header = {
            "ID_PREASIGNACION": next_id,
            "AUTORIZACION": autorizacion,
            **updates,
        }
        logger.info(
            "sheets.preasignacion.manual_append autorizacion=%s turno=%s estado=%s",
            autorizacion,
            id_turno,
            estado_preasignacion,
        )
        self._call_with_retry(worksheet.append_row, [row_by_header.get(header, "") for header in headers])
        self._invalidate_sheet_rows_cache(self.settings.sheet_preasignaciones)
        return True

    def cancelar_preasignacion(self, autorizacion: str) -> None:
        """Marca la preasignacion como cancelada si existe."""

        worksheet = self.worksheet(self.settings.sheet_preasignaciones)
        header_map = self._header_map(worksheet)
        for preasignacion in self.list_preasignaciones():
            if preasignacion.autorizacion == autorizacion and preasignacion.row_index:
                self._update_row(
                    worksheet,
                    preasignacion.row_index,
                    header_map,
                    {"ESTADO_PREASIGNACION": PREASIGNACION_CANCELADA},
                )
        self._invalidate_sheet_rows_cache(self.settings.sheet_preasignaciones)

    def congelar_preasignacion(self, autorizacion: str) -> None:
        """Congela la preasignacion para que quede fuera de reoptimizacion."""

        worksheet = self.worksheet(self.settings.sheet_preasignaciones)
        header_map = self._header_map(worksheet)
        for preasignacion in self.list_preasignaciones():
            if preasignacion.autorizacion == autorizacion and preasignacion.row_index:
                self._update_row(
                    worksheet,
                    preasignacion.row_index,
                    header_map,
                    {"ESTADO_PREASIGNACION": PREASIGNACION_CONGELADA},
                )
        self._invalidate_sheet_rows_cache(self.settings.sheet_preasignaciones)

    def marcar_servicio_cancelado(self, autorizacion: str) -> bool:
        """Cancela el servicio y su preasignacion."""

        changed = self.update_servicio_estado(autorizacion, ESTADO_CANCELADO)
        self.cancelar_preasignacion(autorizacion)
        return changed

    def marcar_servicio_completado(self, autorizacion: str) -> bool:
        """Marca un servicio como completado y lo saca del analisis futuro."""

        changed = self.update_servicio_estado(autorizacion, ESTADO_COMPLETADO)
        self.cancelar_preasignacion(autorizacion)
        return changed

    def lock_due_services(
        self,
        servicios: Optional[List[ServicioPlan]] = None,
        preasignaciones: Optional[List[PreasignacionPlan]] = None,
        turnos: Optional[List[TurnoPlan]] = None,
    ) -> dict:
        """Congela servicios que entraron a la ventana configurada de aviso."""

        count = 0
        servicios_lista = servicios if servicios is not None else self.list_servicios()
        preasignaciones_lista = (
            preasignaciones if preasignaciones is not None else self.list_preasignaciones()
        )
        servicios_map = {servicio.autorizacion: servicio for servicio in servicios_lista}
        now_value = now_bogota()
        vigentes: Dict[str, PreasignacionPlan] = {}
        turnos_lista = turnos if turnos is not None else self.list_turnos()
        turnos_historicos_lista: Optional[List[TurnoPlan]] = None

        for preasignacion in preasignaciones_lista:
            if preasignacion.estado_preasignacion not in {PREASIGNACION_ACTIVA, PREASIGNACION_CONGELADA}:
                continue
            anterior = vigentes.get(preasignacion.autorizacion)
            if anterior is None:
                vigentes[preasignacion.autorizacion] = preasignacion
                continue
            if (
                preasignacion.estado_preasignacion == PREASIGNACION_CONGELADA
                and anterior.estado_preasignacion != PREASIGNACION_CONGELADA
            ):
                vigentes[preasignacion.autorizacion] = preasignacion
                continue
            if (preasignacion.row_index or 0) > (anterior.row_index or 0):
                vigentes[preasignacion.autorizacion] = preasignacion

        servicios_updates: Dict[int, Dict[str, str]] = {}
        preasignaciones_updates: Dict[int, Dict[str, str]] = {}
        turnos_no_encontrados: list[dict[str, str]] = []

        for autorizacion, preasignacion in vigentes.items():
            servicio = servicios_map.get(preasignacion.autorizacion)
            if not servicio or servicio.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio.estado_operacion in ESTADOS_MANUALES:
                continue
            if preasignacion.estado_preasignacion not in {PREASIGNACION_ACTIVA, PREASIGNACION_CONGELADA}:
                continue
            if not servicio.fecha_servicio:
                continue
            delta_min = (servicio.fecha_servicio - now_value).total_seconds() / 60
            if delta_min > self.settings.min_notice_minutes:
                continue
            needs_pre_freeze = preasignacion.estado_preasignacion != PREASIGNACION_CONGELADA
            needs_service_freeze = servicio.estado_operacion != ESTADO_ASIGNADO_FINAL
            if preasignacion.row_index and needs_pre_freeze:
                preasignaciones_updates[preasignacion.row_index] = {
                    "ESTADO_PREASIGNACION": PREASIGNACION_CONGELADA,
                }
            turno_actual = self._resolve_turno_for_preasignacion(
                preasignacion,
                servicio,
                turnos_lista,
            )
            if not turno_actual and preasignacion.id_turno:
                if turnos_historicos_lista is None:
                    turnos_historicos_lista = self.list_turnos(include_expired=True)
                turno_actual = self._resolve_turno_for_preasignacion(
                    preasignacion,
                    servicio,
                    turnos_historicos_lista,
                )
            turno_no_encontrado = bool(preasignacion.id_turno and not turno_actual)
            if turno_no_encontrado:
                logger.warning(
                    "sheets.lock_due_services.turn_not_found autorizacion=%s id_turno=%s departamento=%s",
                    servicio.autorizacion,
                    preasignacion.id_turno,
                    servicio.departamento,
                )
                turnos_no_encontrados.append(
                    {
                        "autorizacion": servicio.autorizacion,
                        "id_turno": preasignacion.id_turno,
                        "departamento": servicio.departamento,
                    }
                )
            cedula_turno = turno_actual.cedula_conductor if turno_actual else ""
            nombre_turno = turno_actual.nombre_conductor if turno_actual else ""
            correo_turno = turno_actual.correo if turno_actual else ""
            id_turno_servicio = turno_actual.id_turno if turno_actual else ""
            cedula_servicio = cedula_turno or servicio.cedula_conductor or preasignacion.cedula_conductor
            nombre_servicio = (
                nombre_turno
                or servicio.nombre_conductor
                or preasignacion.nombre_tecnico_preasignacion
            )
            needs_service_turn_sync = servicio.estado_operacion == ESTADO_ASIGNADO_FINAL and (
                servicio.id_turno != id_turno_servicio
            )
            needs_service_contact_sync = (
                servicio.estado_operacion == ESTADO_ASIGNADO_FINAL
                and (
                    (bool(cedula_servicio) and servicio.cedula_conductor != cedula_servicio)
                    or (bool(nombre_servicio) and servicio.nombre_conductor != nombre_servicio)
                    or (bool(correo_turno) and servicio.correos != correo_turno)
                )
            )
            needs_pre_turn_clear = turno_no_encontrado and bool(preasignacion.id_turno)
            needs_pre_contact_sync = bool(turno_actual) and (
                preasignacion.id_turno != turno_actual.id_turno
                or preasignacion.cedula_conductor != turno_actual.cedula_conductor
                or preasignacion.nombre_tecnico_preasignacion != turno_actual.nombre_conductor
            )
            if servicio.row_index and (
                needs_service_freeze or needs_service_turn_sync or needs_service_contact_sync
            ):
                servicios_updates[servicio.row_index] = {
                    "ESTADO_DEL_SERVICIO_OPERACION": ESTADO_ASIGNADO_FINAL,
                    "ESTADO_DEL_SERVICIO_TECNICO": ESTADO_ASIGNADO_FINAL,
                    "ID_TURNO": id_turno_servicio,
                    "CEDULA_CONDUCTOR": cedula_servicio,
                    "NOMBRE_CONDUCTOR": nombre_servicio,
                    "CORREOS": correo_turno or servicio.correos,
                }
            if preasignacion.row_index and needs_pre_turn_clear:
                preasignaciones_updates.setdefault(preasignacion.row_index, {}).update(
                    {"ID_TURNO": ""}
                )
            if preasignacion.row_index and needs_pre_contact_sync:
                preasignaciones_updates.setdefault(preasignacion.row_index, {}).update(
                    {
                        "ID_TURNO": turno_actual.id_turno,
                        "CEDULA_CONDUCTOR": turno_actual.cedula_conductor,
                        "NOMBRE_TECNICO_PREASIGNACION": turno_actual.nombre_conductor,
                    }
                )
            if (
                needs_pre_freeze
                or needs_service_freeze
                or needs_service_turn_sync
                or needs_service_contact_sync
                or needs_pre_turn_clear
                or needs_pre_contact_sync
            ):
                count += 1

        if servicios is not None or preasignaciones is not None:
            for servicio in servicios_lista:
                if servicio.row_index in servicios_updates:
                    servicio.estado_operacion = ESTADO_ASIGNADO_FINAL
                    servicio.estado_tecnico = ESTADO_ASIGNADO_FINAL
                    servicio.id_turno = servicios_updates[servicio.row_index].get("ID_TURNO", servicio.id_turno)
                    servicio.cedula_conductor = servicios_updates[servicio.row_index].get(
                        "CEDULA_CONDUCTOR",
                        servicio.cedula_conductor,
                    )
                    servicio.nombre_conductor = servicios_updates[servicio.row_index].get(
                        "NOMBRE_CONDUCTOR",
                        servicio.nombre_conductor,
                    )
                    servicio.correos = servicios_updates[servicio.row_index].get(
                        "CORREOS",
                        servicio.correos,
                    )
            for preasignacion in preasignaciones_lista:
                if preasignacion.row_index in preasignaciones_updates:
                    updates = preasignaciones_updates[preasignacion.row_index]
                    preasignacion.estado_preasignacion = updates.get(
                        "ESTADO_PREASIGNACION",
                        PREASIGNACION_CONGELADA,
                    )
                    preasignacion.id_turno = updates.get("ID_TURNO", preasignacion.id_turno)
                    preasignacion.cedula_conductor = updates.get(
                        "CEDULA_CONDUCTOR",
                        preasignacion.cedula_conductor,
                    )
                    preasignacion.nombre_tecnico_preasignacion = updates.get(
                        "NOMBRE_TECNICO_PREASIGNACION",
                        preasignacion.nombre_tecnico_preasignacion,
                    )

        if preasignaciones_updates:
            worksheet = self.worksheet(self.settings.sheet_preasignaciones)
            header_map = self._header_map(worksheet)
            self._batch_update_rows(worksheet, header_map, preasignaciones_updates)
        if servicios_updates:
            worksheet = self.worksheet(self.settings.sheet_servicios)
            header_map = self._header_map(worksheet)
            self._batch_update_rows(worksheet, header_map, servicios_updates)

        logger.info(
            "sheets.lock_due_services count=%s preasignaciones=%s servicios=%s",
            count,
            len(preasignaciones_updates),
            len(servicios_updates),
        )

        return {
            "resultado": "OK",
            "cantidad": count,
            "turnos_no_encontrados": turnos_no_encontrados,
        }

    def finalize_manual_mode_preassignments(
        self,
        servicios: Optional[List[ServicioPlan]] = None,
        preasignaciones: Optional[List[PreasignacionPlan]] = None,
        turnos: Optional[List[TurnoPlan]] = None,
        lookahead_hours: int = 8,
    ) -> dict:
        """Cierra preasignaciones proximas cuando el sistema opera en modo MANUAL."""

        servicios_lista = servicios if servicios is not None else self.list_servicios()
        preasignaciones_lista = (
            preasignaciones if preasignaciones is not None else self.list_preasignaciones()
        )
        turnos_lista = turnos if turnos is not None else self.list_turnos()
        vigentes = self._select_current_preasignaciones(preasignaciones_lista)
        turnos_by_id = {turno.id_turno: turno for turno in turnos_lista}
        now_value = now_bogota()
        cutoff = now_value + timedelta(hours=lookahead_hours)

        servicios_updates: Dict[int, Dict[str, str]] = {}
        preasignaciones_updates: Dict[int, Dict[str, str]] = {}
        asignados: list[str] = []
        urgentes: list[str] = []

        for servicio in servicios_lista:
            if servicio.estado_operacion != ESTADO_PREASIGNADO:
                continue
            if not servicio.fecha_servicio or not (now_value <= servicio.fecha_servicio <= cutoff):
                continue
            preasignacion = vigentes.get(servicio.autorizacion)
            turno = turnos_by_id.get(preasignacion.id_turno) if preasignacion else None
            if servicio.row_index and preasignacion and turno:
                servicios_updates[servicio.row_index] = {
                    "ESTADO_DEL_SERVICIO_OPERACION": ESTADO_ASIGNADO_FINAL,
                    "ESTADO_DEL_SERVICIO_TECNICO": ESTADO_ASIGNADO_FINAL,
                    "ID_TURNO": turno.id_turno,
                    "CEDULA_CONDUCTOR": turno.cedula_conductor or preasignacion.cedula_conductor,
                    "NOMBRE_CONDUCTOR": turno.nombre_conductor or preasignacion.nombre_tecnico_preasignacion,
                    "CORREOS": turno.correo or servicio.correos,
                }
                if preasignacion.row_index:
                    preasignaciones_updates[preasignacion.row_index] = {
                        "ID_TURNO": turno.id_turno,
                        "CEDULA_CONDUCTOR": turno.cedula_conductor,
                        "NOMBRE_TECNICO_PREASIGNACION": turno.nombre_conductor,
                        "ESTADO_PREASIGNACION": PREASIGNACION_CONGELADA,
                    }
                asignados.append(servicio.autorizacion)
                continue

            if servicio.row_index:
                servicios_updates[servicio.row_index] = {
                    "ESTADO_DEL_SERVICIO_OPERACION": ESTADO_URGENTE_GESTIONAR_MANUAL,
                    "ESTADO_DEL_SERVICIO_TECNICO": ESTADO_URGENTE_GESTIONAR_MANUAL,
                    "ID_TURNO": "",
                }
            if preasignacion and preasignacion.row_index:
                preasignaciones_updates[preasignacion.row_index] = {
                    "ID_TURNO": "",
                    "ESTADO_PREASIGNACION": PREASIGNACION_CONGELADA,
                }
            urgentes.append(servicio.autorizacion)

        if servicios is not None:
            for servicio in servicios_lista:
                updates = servicios_updates.get(servicio.row_index or 0)
                if not updates:
                    continue
                servicio.estado_operacion = updates["ESTADO_DEL_SERVICIO_OPERACION"]
                servicio.estado_tecnico = updates["ESTADO_DEL_SERVICIO_TECNICO"]
                servicio.id_turno = updates.get("ID_TURNO", servicio.id_turno)
                servicio.cedula_conductor = updates.get("CEDULA_CONDUCTOR", servicio.cedula_conductor)
                servicio.nombre_conductor = updates.get("NOMBRE_CONDUCTOR", servicio.nombre_conductor)
                servicio.correos = updates.get("CORREOS", servicio.correos)
            for preasignacion in preasignaciones_lista:
                updates = preasignaciones_updates.get(preasignacion.row_index or 0)
                if not updates:
                    continue
                preasignacion.id_turno = updates.get("ID_TURNO", preasignacion.id_turno)
                preasignacion.cedula_conductor = updates.get(
                    "CEDULA_CONDUCTOR",
                    preasignacion.cedula_conductor,
                )
                preasignacion.nombre_tecnico_preasignacion = updates.get(
                    "NOMBRE_TECNICO_PREASIGNACION",
                    preasignacion.nombre_tecnico_preasignacion,
                )
                preasignacion.estado_preasignacion = updates.get(
                    "ESTADO_PREASIGNACION",
                    preasignacion.estado_preasignacion,
                )

        if preasignaciones_updates:
            worksheet = self.worksheet(self.settings.sheet_preasignaciones)
            header_map = self._header_map(worksheet)
            self._batch_update_rows(worksheet, header_map, preasignaciones_updates)
        if servicios_updates:
            worksheet = self.worksheet(self.settings.sheet_servicios)
            header_map = self._header_map(worksheet)
            self._batch_update_rows(worksheet, header_map, servicios_updates)

        logger.warning(
            "sheets.manual_mode.finalize asignados=%s urgentes=%s",
            len(asignados),
            len(urgentes),
        )
        return {
            "resultado": "OK",
            "asignados_final": len(asignados),
            "urgente_gestionar_manual": len(urgentes),
            "asignados": asignados,
            "urgentes": urgentes,
            "lookahead_hours": lookahead_hours,
        }

    def _resolve_turno_for_preasignacion(
        self,
        preasignacion: PreasignacionPlan,
        servicio: ServicioPlan,
        turnos: Iterable[TurnoPlan],
    ) -> Optional[TurnoPlan]:
        """Encuentra el turno exacto para copiar datos humanos al servicio."""

        turnos_lista = list(turnos)
        exactos = [turno for turno in turnos_lista if turno.id_turno == preasignacion.id_turno]
        for turno in exactos:
            if turno.correo:
                return turno
        if exactos:
            return exactos[-1]
        return None

    def reconcile_terminal_services(
        self,
        servicios: List[ServicioPlan],
        preasignaciones: List[PreasignacionPlan],
    ) -> int:
        """Sincroniza preasignaciones activas con servicios cerrados manualmente."""

        vigentes = self._select_current_preasignaciones(preasignaciones)
        servicios_by_auth = {servicio.autorizacion: servicio for servicio in servicios}
        updates: Dict[int, Dict[str, str]] = {}

        for autorizacion, preasignacion in vigentes.items():
            servicio = servicios_by_auth.get(autorizacion)
            if not servicio or not preasignacion.row_index:
                continue
            if servicio.estado_operacion not in {ESTADO_CANCELADO, ESTADO_COMPLETADO}:
                continue
            if preasignacion.estado_preasignacion == PREASIGNACION_CANCELADA:
                continue
            updates[preasignacion.row_index] = {"ESTADO_PREASIGNACION": PREASIGNACION_CANCELADA}
            preasignacion.estado_preasignacion = PREASIGNACION_CANCELADA

        if updates:
            worksheet = self.worksheet(self.settings.sheet_preasignaciones)
            header_map = self._header_map(worksheet)
            self._batch_update_rows(worksheet, header_map, updates)
        return len(updates)

    def apply_dynamic_assignments(
        self,
        new_service_auth: str,
        assignments: Dict[str, TurnoPlan],
        existing_services: Dict[str, ServicioPlan],
        current_preasignaciones: Optional[List[PreasignacionPlan]] = None,
        sync_service_auths: Optional[set[str]] = None,
    ) -> None:
        """Persiste las preasignaciones resueltas por el motor OR."""

        started = time.monotonic()
        sync_service_auths = sync_service_auths or set()
        preasignaciones = current_preasignaciones or self.list_preasignaciones()
        vigentes = self._select_current_preasignaciones(preasignaciones)
        fecha_preasignacion = format_datetime(now_bogota())

        affected_turns = {turno.id_turno for turno in assignments.values()}
        turn_by_auth: Dict[str, str] = {}
        for autorizacion, preasignacion in vigentes.items():
            servicio = existing_services.get(autorizacion)
            if not servicio or servicio.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio.estado_operacion in ESTADOS_MANUALES:
                continue
            turn_by_auth[autorizacion] = preasignacion.id_turno

        for autorizacion, turno in assignments.items():
            turn_by_auth[autorizacion] = turno.id_turno
            affected_turns.add(turno.id_turno)

        orden_por_autorizacion: Dict[str, int] = {}
        servicios_por_turno: Dict[str, List[ServicioPlan]] = {}
        for autorizacion, turno_id in turn_by_auth.items():
            if turno_id not in affected_turns:
                continue
            servicio = existing_services.get(autorizacion)
            if not servicio or servicio.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio.estado_operacion in ESTADOS_MANUALES:
                continue
            servicios_por_turno.setdefault(turno_id, []).append(servicio)

        for turno_id, servicios_turno in servicios_por_turno.items():
            servicios_ordenados = sorted(
                servicios_turno,
                key=lambda item: (item.fecha_servicio, item.autorizacion),
            )
            for orden, servicio in enumerate(servicios_ordenados, start=1):
                orden_por_autorizacion[servicio.autorizacion] = orden

        worksheet_pre = self.worksheet(self.settings.sheet_preasignaciones)
        header_map_pre = self._header_map(worksheet_pre)
        headers_pre = self.read_headers(self.settings.sheet_preasignaciones)
        pre_updates: Dict[int, Dict[str, str]] = {}
        pre_appends: List[List[str]] = []
        numeric_ids = [
            int(item.id_preasignacion)
            for item in preasignaciones
            if str(item.id_preasignacion).isdigit()
        ]
        next_id = (max(numeric_ids) if numeric_ids else 0) + 1
        next_row_index = (max((item.row_index or 1) for item in preasignaciones) if preasignaciones else 1) + 1

        for autorizacion, turno_id in turn_by_auth.items():
            if turno_id not in affected_turns:
                continue
            servicio = existing_services.get(autorizacion)
            if not servicio or servicio.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio.estado_operacion in ESTADOS_MANUALES:
                continue
            orden_en_ruta = orden_por_autorizacion.get(autorizacion, 1)
            vigente = vigentes.get(autorizacion)
            assigned_turn = assignments.get(autorizacion)
            updates = {
                "ORDEN_EN_RUTA": str(orden_en_ruta),
            }
            if assigned_turn:
                updates.update(
                    {
                        "ID_TURNO": assigned_turn.id_turno,
                        "CEDULA_CONDUCTOR": assigned_turn.cedula_conductor,
                        "NOMBRE_TECNICO_PREASIGNACION": assigned_turn.nombre_conductor,
                        "FECHA_PREASIGNACION": fecha_preasignacion,
                        "ESTADO_PREASIGNACION": PREASIGNACION_ACTIVA,
                    }
                )
            if vigente and vigente.row_index:
                pre_updates[vigente.row_index] = updates
                continue
            if not assigned_turn:
                continue
            row_by_header = {
                "ID_PREASIGNACION": str(next_id),
                "AUTORIZACION": autorizacion,
                **updates,
            }
            pre_appends.append([row_by_header.get(header, "") for header in headers_pre])
            next_id += 1

        if pre_updates:
            logger.warning("sheets.preasignaciones.update_rows rows=%s", len(pre_updates))
            self._batch_update_rows(worksheet_pre, header_map_pre, pre_updates)
        if pre_appends:
            logger.warning("sheets.preasignaciones.append_rows rows=%s", len(pre_appends))
            self._call_with_retry(worksheet_pre.append_rows, pre_appends)
            self._invalidate_sheet_rows_cache(self.settings.sheet_preasignaciones)

        worksheet_servicios = self.worksheet(self.settings.sheet_servicios)
        header_map_servicios = self._header_map(worksheet_servicios)
        servicio_updates: Dict[int, Dict[str, str]] = {}

        for autorizacion, turno in assignments.items():
            servicio_existente = existing_services.get(autorizacion)
            if not servicio_existente or not servicio_existente.row_index:
                continue
            if servicio_existente.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio_existente.estado_operacion in ESTADOS_MANUALES:
                continue
            if (
                servicio_existente.estado_operacion == ESTADO_ASIGNADO_FINAL
                and autorizacion in sync_service_auths
            ):
                servicio_updates[servicio_existente.row_index] = {
                    "ID_TURNO": turno.id_turno,
                    "CEDULA_CONDUCTOR": turno.cedula_conductor,
                    "NOMBRE_CONDUCTOR": turno.nombre_conductor,
                    "CORREOS": turno.correo,
                }
                continue
            if servicio_existente.estado_operacion in {ESTADO_ASIGNADO_FINAL, ESTADO_CANCELADO}:
                continue

            servicio_updates[servicio_existente.row_index] = {
                "ESTADO_DEL_SERVICIO_OPERACION": ESTADO_PREASIGNADO,
                "ESTADO_DEL_SERVICIO_TECNICO": ESTADO_PREASIGNADO,
                "ID_TURNO": "",
                "CEDULA_CONDUCTOR": "",
                "NOMBRE_CONDUCTOR": NOMBRE_CONDUCTOR_PENDIENTE,
                "CORREOS": "",
            }

        if servicio_updates:
            logger.warning("sheets.servicios.update_rows rows=%s", len(servicio_updates))
            self._batch_update_rows(worksheet_servicios, header_map_servicios, servicio_updates)

        logger.warning(
            "sheets.apply_dynamic_assignments assignments=%s pre_updates=%s pre_appends=%s servicio_updates=%s duration_ms=%s",
            len(assignments),
            len(pre_updates),
            len(pre_appends),
            len(servicio_updates),
            int((time.monotonic() - started) * 1000),
        )

        if current_preasignaciones is not None:
            vigentes_actualizados = self._select_current_preasignaciones(current_preasignaciones)
            for autorizacion, turno_id in turn_by_auth.items():
                vigente = vigentes_actualizados.get(autorizacion)
                orden = orden_por_autorizacion.get(autorizacion)
                if vigente and orden is not None:
                    vigente.orden_en_ruta = orden
                    if autorizacion in assignments:
                        vigente.id_turno = assignments[autorizacion].id_turno
                        vigente.cedula_conductor = assignments[autorizacion].cedula_conductor
                        vigente.nombre_tecnico_preasignacion = assignments[autorizacion].nombre_conductor
                        vigente.fecha_preasignacion = parse_datetime(fecha_preasignacion)
                        vigente.estado_preasignacion = PREASIGNACION_ACTIVA
            for servicio in existing_services.values():
                if servicio.row_index in servicio_updates:
                    updates = servicio_updates[servicio.row_index]
                    if servicio.estado_operacion == ESTADO_ASIGNADO_FINAL:
                        servicio.id_turno = updates.get("ID_TURNO", servicio.id_turno)
                        servicio.cedula_conductor = updates.get(
                            "CEDULA_CONDUCTOR",
                            servicio.cedula_conductor,
                        )
                        servicio.nombre_conductor = updates.get(
                            "NOMBRE_CONDUCTOR",
                            servicio.nombre_conductor,
                        )
                        servicio.correos = updates.get("CORREOS", servicio.correos)
                    else:
                        servicio.estado_operacion = ESTADO_PREASIGNADO
                        servicio.estado_tecnico = ESTADO_PREASIGNADO
                        servicio.id_turno = ""
                        servicio.cedula_conductor = ""
                        servicio.nombre_conductor = NOMBRE_CONDUCTOR_PENDIENTE
                        servicio.correos = ""
            for row in pre_appends:
                row_by_header = dict(zip(headers_pre, row))
                current_preasignaciones.append(
                    PreasignacionPlan.from_sheet_row(
                        {
                            **row_by_header,
                            "_row": next_row_index,
                        }
                    )
                )
                next_row_index += 1

    def _header_map(self, worksheet) -> Dict[str, int]:
        """Mapea cada header a su indice de columna."""

        return {
            normalize_header(header): index
            for index, header in enumerate(self.read_headers(worksheet.title), start=1)
        }

    def _update_row(
        self,
        worksheet,
        row_index: int,
        header_map: Dict[str, int],
        updates: Dict[str, str],
        fixed_updates: Optional[Dict[str, str]] = None,
    ) -> None:
        """Actualiza una fila por nombre de columna."""

        self._log_missing_headers(header_map.keys(), updates.keys(), f"update_row:{worksheet.title}")
        batch = []
        for header, value in updates.items():
            column_index = header_map.get(header)
            if not column_index:
                continue
            batch.append(
                {
                    "range": gspread.utils.rowcol_to_a1(row_index, column_index),
                    "values": [[value]],
                }
            )
        for cell_range, value in (fixed_updates or {}).items():
            batch.append(
                {
                    "range": cell_range,
                    "values": [[value]],
                }
            )
        if batch:
            logger.warning(
                "sheets.row_update sheet=%s row=%s cells=%s",
                worksheet.title,
                row_index,
                len(batch),
            )
            self._call_with_retry(worksheet.batch_update, batch)
            self._invalidate_sheet_rows_cache(worksheet.title)

    def _batch_update_rows(
        self,
        worksheet,
        header_map: Dict[str, int],
        updates_by_row: Dict[int, Dict[str, str]],
    ) -> None:
        """Aplica multiples actualizaciones de varias filas en una sola llamada."""

        batch = []
        for row_index, updates in updates_by_row.items():
            self._log_missing_headers(header_map.keys(), updates.keys(), f"batch_update:{worksheet.title}")
            for header, value in updates.items():
                column_index = header_map.get(header)
                if not column_index:
                    continue
                batch.append(
                    {
                        "range": gspread.utils.rowcol_to_a1(row_index, column_index),
                        "values": [[value]],
                    }
                )
        if batch:
            logger.warning(
                "sheets.rows_batch_update sheet=%s rows=%s cells=%s",
                worksheet.title,
                len(updates_by_row),
                len(batch),
            )
            self._call_with_retry(worksheet.batch_update, batch)
            self._invalidate_sheet_rows_cache(worksheet.title)

    def _log_missing_headers(self, available_headers, required_headers, context: str) -> None:
        """Registra headers esperados que no existen en la hoja."""

        available = {str(header).strip().upper() for header in available_headers}
        missing = sorted({str(header).strip().upper() for header in required_headers} - available)
        if missing:
            logger.warning("sheets.headers.missing context=%s headers=%s", context, ",".join(missing))

    def _select_current_preasignaciones(
        self,
        preasignaciones: Iterable[PreasignacionPlan],
    ) -> Dict[str, PreasignacionPlan]:
        """Selecciona la preasignacion vigente por autorizacion."""

        selected: Dict[str, PreasignacionPlan] = {}
        for preasignacion in preasignaciones:
            if preasignacion.estado_preasignacion not in {PREASIGNACION_ACTIVA, PREASIGNACION_CONGELADA}:
                continue
            previous = selected.get(preasignacion.autorizacion)
            if previous is None:
                selected[preasignacion.autorizacion] = preasignacion
                continue
            if (
                preasignacion.estado_preasignacion == PREASIGNACION_CONGELADA
                and previous.estado_preasignacion != PREASIGNACION_CONGELADA
            ):
                selected[preasignacion.autorizacion] = preasignacion
                continue
            if (preasignacion.row_index or 0) > (previous.row_index or 0):
                selected[preasignacion.autorizacion] = preasignacion
        return selected

    def _invalidate_sheet_cache(self, sheet_name: str) -> None:
        """Invalida el cache local de una hoja tras una escritura."""

        self._invalidate_sheet_rows_cache(sheet_name)
        self._header_cache.pop(sheet_name, None)
        self._header_cache_at.pop(sheet_name, None)

    def _invalidate_sheet_rows_cache(self, sheet_name: str) -> None:
        """Invalida solo el cache de filas sin tocar headers."""

        self._row_cache.pop(sheet_name, None)
        self._row_cache_at.pop(sheet_name, None)

    def _cache_expired(self, loaded_at: float | None) -> bool:
        """Indica si una entrada de cache ya supero su TTL."""

        if loaded_at is None:
            return False
        return (time.monotonic() - loaded_at) > self.settings.sheet_cache_ttl_seconds

    def _remaining_budget_seconds(self) -> float | None:
        """Devuelve el tiempo restante del request actual, si existe."""

        deadline = self._request_deadline.get()
        if deadline is None:
            return None
        return deadline - time.monotonic()

    def _resolve_http_timeout_seconds(self) -> float:
        """Calcula el timeout HTTP por llamada a Sheets."""

        remaining = self._remaining_budget_seconds()
        default_timeout = float(self.settings.sheets_http_timeout_seconds)
        if remaining is None:
            return default_timeout
        if remaining <= 1.0:
            raise TimeoutError("Google Sheets request deadline exceeded before HTTP call")
        return max(1.0, min(default_timeout, remaining - 1.0))

    def _call_with_retry(self, func, *args, **kwargs):
        """Reintenta lecturas/escrituras transitorias contra Google Sheets."""

        delay_seconds = 2
        last_error = None
        operation_name = getattr(func, "__name__", func.__class__.__name__)
        max_retries = max(1, self.settings.sheets_max_retries)
        for attempt in range(max_retries):
            remaining = self._remaining_budget_seconds()
            if attempt == 0 or (remaining is not None and remaining <= 30):
                logger.warning(
                    "sheets.call.start operation=%s attempt=%s remaining_budget_s=%s",
                    operation_name,
                    attempt + 1,
                    None if remaining is None else round(remaining, 2),
                )
            if remaining is not None and remaining <= 1.0:
                raise TimeoutError(
                    f"Google Sheets request deadline exceeded during {operation_name}"
                )
            start_time = time.monotonic()
            try:
                result = func(*args, **kwargs)
                elapsed_ms = int((time.monotonic() - start_time) * 1000)
                if elapsed_ms >= 1000 or attempt > 0:
                    logger.warning(
                        "sheets.call.ok operation=%s attempt=%s duration_ms=%s",
                        operation_name,
                        attempt + 1,
                        elapsed_ms,
                    )
                return result
            except APIError as exc:
                last_error = exc
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code not in {429, 500, 502, 503, 504}:
                    logger.exception(
                        "sheets.call.error operation=%s attempt=%s status=%s",
                        operation_name,
                        attempt + 1,
                        status_code,
                    )
                    raise
                if attempt == max_retries - 1:
                    break
                remaining = self._remaining_budget_seconds()
                if remaining is not None and remaining <= (delay_seconds + 1.0):
                    raise TimeoutError(
                        f"Google Sheets retry budget exhausted during {operation_name}"
                    )
                logger.warning(
                    "sheets.call.retry operation=%s attempt=%s status=%s next_sleep_s=%s",
                    operation_name,
                    attempt + 1,
                    status_code,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 30)
            except RefreshError as exc:
                last_error = exc
                message = str(exc).lower()
                transient = any(
                    marker in message
                    for marker in (
                        "internal_failure",
                        "temporarily",
                        "timeout",
                        "unavailable",
                        "backenderror",
                    )
                )
                if not transient or attempt == max_retries - 1:
                    logger.exception(
                        "sheets.call.auth_error operation=%s attempt=%s transient=%s",
                        operation_name,
                        attempt + 1,
                        transient,
                    )
                    raise
                remaining = self._remaining_budget_seconds()
                if remaining is not None and remaining <= (delay_seconds + 1.0):
                    raise TimeoutError(
                        f"Google Sheets auth retry budget exhausted during {operation_name}"
                    )
                logger.warning(
                    "sheets.call.auth_retry operation=%s attempt=%s next_sleep_s=%s",
                    operation_name,
                    attempt + 1,
                    delay_seconds,
                )
                time.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 30)
        if last_error:
            raise last_error
        return func(*args, **kwargs)
