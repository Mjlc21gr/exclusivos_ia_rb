"""Simulacion local del cluster RVE desde CSV usando Docker y test clock."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = APP_DIR.parent
SIMULATION_INPUTS_DIR = REPO_DIR / "simulation_inputs"
SIMULATION_RUNS_DIR = REPO_DIR / "simulation_runs"

IMAGE_NAME = "rve-local-sim"
CONTAINER_NAME = "rve-local-sim"
BASE_URL = "http://127.0.0.1:18080"
DEV_SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
TURNOS_CSV_NAME = "turnos_template.csv"
SUPPORTED_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
)
DEFAULT_FROM = "2026-04-01"
DEFAULT_TO = "2026-04-20"
DEFAULT_DELAY_SECONDS = 10
DEFAULT_VALIDAR_EVERY_MINUTES = 10
DEFAULT_GOOGLE_MATRIX_PRICE_PER_1000_USD = 10.0
DEFAULT_GOOGLE_MATRIX_MAX_ELEMENTS_PER_REQUEST = 100
GOOGLE_DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
GOOGLE_DISTANCE_MATRIX_SKU = "Distance Matrix Advanced"
CSV_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


@dataclass(frozen=True)
class TechnicianTemplate:
    """Datos base para crear turnos repetibles por departamento."""

    cedula: str
    nombre: str
    celular: str
    correo: str
    proveedor: str
    direccion_origen: str
    latitud: str
    longitud: str
    departamento: str


@dataclass(frozen=True)
class SimulationEvent:
    """Evento cronologico de la simulacion."""

    event_time: datetime
    kind: str
    label: str
    payload: dict[str, str] | None = None
    source_auth: str = ""
    priority: int = field(default=0, compare=False)


@dataclass(frozen=True)
class LoadedSimulationData:
    """Datos normalizados de entrada para una corrida."""

    turnos: list[dict[str, str]]
    payloads: list[dict[str, str]]
    cancelaciones: dict[str, datetime]
    window_start: datetime
    window_end: datetime


class GoogleDistanceMatrixClient:
    """Cliente minimo de Distance Matrix para replay de KPIs."""

    def __init__(self, api_key: str, departure_time: datetime) -> None:
        if not api_key:
            raise RuntimeError("GOOGLE_MAPS_API_KEY requerido para replay Distance Matrix")
        self.api_key = api_key
        self.departure_time = departure_time
        self.cache: dict[tuple[float, float, float, float], float] = {}

    def travel_minutes(self, origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float) -> float:
        key = (
            round(origin_lat, 6),
            round(origin_lng, 6),
            round(dest_lat, 6),
            round(dest_lng, 6),
        )
        if key in self.cache:
            return self.cache[key]
        query = urllib.parse.urlencode(
            {
                "origins": f"{origin_lat:.6f},{origin_lng:.6f}",
                "destinations": f"{dest_lat:.6f},{dest_lng:.6f}",
                "mode": "driving",
                "departure_time": self._departure_timestamp(),
                "traffic_model": "best_guess",
                "key": self.api_key,
            }
        )
        request = urllib.request.Request(f"{GOOGLE_DISTANCE_MATRIX_URL}?{query}")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8")
            raise RuntimeError(f"Distance Matrix HTTP {exc.code}: {raw_body[:300]}") from exc
        except URLError as exc:
            raise RuntimeError(f"Distance Matrix error: {exc}") from exc
        if body.get("status") != "OK":
            raise RuntimeError(f"Distance Matrix status={body.get('status')}: {body.get('error_message', '')}")
        rows = body.get("rows") or []
        elements = rows[0].get("elements") if rows else []
        element = elements[0] if elements else {}
        if element.get("status") != "OK":
            raise RuntimeError(f"Distance Matrix element_status={element.get('status')} para tramo {key}")
        duration = element.get("duration_in_traffic") or element.get("duration") or {}
        seconds = duration.get("value")
        if seconds is None:
            raise RuntimeError(f"Distance Matrix sin duracion para tramo {key}: {body}")
        minutes = float(seconds) / 60.0
        self.cache[key] = minutes
        return minutes

    def _departure_timestamp(self) -> int:
        if self.departure_time.tzinfo is None:
            departure_time = self.departure_time.replace(tzinfo=ZoneInfo("America/Bogota"))
        else:
            departure_time = self.departure_time
        return int(departure_time.timestamp())


TECHNICIANS = [
    TechnicianTemplate(
        cedula="1000954201",
        nombre="SANTIAGO ROJAS",
        celular="3104100001",
        correo="santiago.rojas.sim@example.com",
        proveedor="MYS",
        direccion_origen="Centro Comercial Cedritos, Bogota",
        latitud="4,724900",
        longitud="-74,047500",
        departamento="CUNDINAMARCA",
    ),
    TechnicianTemplate(
        cedula="1000954202",
        nombre="LAURA GOMEZ",
        celular="3104100002",
        correo="laura.gomez.sim@example.com",
        proveedor="MYS",
        direccion_origen="Parque de la 93, Bogota",
        latitud="4,676300",
        longitud="-74,047900",
        departamento="CUNDINAMARCA",
    ),
    TechnicianTemplate(
        cedula="1000954203",
        nombre="MATEO PARRA",
        celular="3104100003",
        correo="mateo.parra.sim@example.com",
        proveedor="MYS",
        direccion_origen="Portal del Sur, Bogota",
        latitud="4,597100",
        longitud="-74,169400",
        departamento="CUNDINAMARCA",
    ),
    TechnicianTemplate(
        cedula="1000954301",
        nombre="JUAN DAVID ARANGO",
        celular="3104200001",
        correo="juan.arango.sim@example.com",
        proveedor="MYS",
        direccion_origen="Parque de Bello, Antioquia",
        latitud="6,337300",
        longitud="-75,558200",
        departamento="ANTIOQUIA",
    ),
    TechnicianTemplate(
        cedula="1000954302",
        nombre="CAMILA RESTREPO",
        celular="3104200002",
        correo="camila.restrepo.sim@example.com",
        proveedor="MYS",
        direccion_origen="Estadio Atanasio Girardot, Medellin",
        latitud="6,257900",
        longitud="-75,590600",
        departamento="ANTIOQUIA",
    ),
    TechnicianTemplate(
        cedula="1000954303",
        nombre="DANIELA TORO",
        celular="3104200003",
        correo="daniela.toro.sim@example.com",
        proveedor="MYS",
        direccion_origen="Parque de Envigado, Antioquia",
        latitud="6,175900",
        longitud="-75,591700",
        departamento="ANTIOQUIA",
    ),
]

TURNOS_HEADERS = [
    "ID_TURNO",
    "CEDULA_CONDUCTOR",
    "NOMBRE_CONDUCTOR",
    "CELULAR_TECNICO",
    "CORREO",
    "PROVEEDOR",
    "DIRECCION_ORIGEN",
    "LATITUD_SERVICIO_ORIGEN",
    "LONGITUD_SERVICIO_ORIGEN",
    "FECHA_INICIO_TURNO",
    "FECHA_FIN_TURNO",
    "SERVICIO",
    "TIPO_SERVICIO",
    "DEPARTAMENTO",
]


def parse_datetime_value(value: str) -> datetime:
    """Parsea fechas del CSV y del CLI."""

    text = str(value or "").strip()
    for fmt in SUPPORTED_DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"Fecha invalida: {value}")


def format_datetime_value(value: datetime) -> str:
    """Formatea fechas para API y Google Sheets."""

    return value.strftime("%Y-%m-%d %H:%M:%S")


def _duration_to_minutes(value: str) -> float:
    """Convierte duraciones tipo Google '123.4s' a minutos."""

    text = str(value or "").strip()
    if not text.endswith("s"):
        raise ValueError(f"Duracion Google invalida: {value}")
    return float(text[:-1]) / 60.0


def google_departure_reference(simulation_date: str, now_value: datetime | None = None) -> datetime:
    """Calcula una fecha futura equivalente a 21:00 para Google Distance Matrix."""

    reference = datetime.strptime(simulation_date, "%Y-%m-%d").replace(hour=21, minute=0, second=0)
    current = now_value or datetime.now()
    while reference <= current:
        reference += timedelta(days=7)
    return reference


def weekend_dates(from_date: str = DEFAULT_FROM, to_date: str = DEFAULT_TO) -> list[datetime]:
    """Retorna viernes, sabados y domingos del rango inclusivo."""

    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    dates = []
    current = start
    while current <= end:
        if current.weekday() in {4, 5, 6}:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def generate_turnos(from_date: str = DEFAULT_FROM, to_date: str = DEFAULT_TO) -> list[dict[str, str]]:
    """Genera turnos nocturnos para todos los tecnicos y fechas configuradas."""

    rows: list[dict[str, str]] = []
    for shift_date in weekend_dates(from_date, to_date):
        start = shift_date.replace(hour=21, minute=0, second=0)
        end = (shift_date + timedelta(days=1)).replace(hour=4, minute=0, second=0)
        department_counts: dict[str, int] = {}
        for technician in TECHNICIANS:
            department_counts[technician.departamento] = department_counts.get(technician.departamento, 0) + 1
            dept_code = "CUN" if technician.departamento == "CUNDINAMARCA" else "ANT"
            row = {
                "ID_TURNO": f"SIM-{shift_date:%Y%m%d}-{dept_code}-{department_counts[technician.departamento]}",
                "CEDULA_CONDUCTOR": technician.cedula,
                "NOMBRE_CONDUCTOR": technician.nombre,
                "CELULAR_TECNICO": technician.celular,
                "CORREO": technician.correo,
                "PROVEEDOR": technician.proveedor,
                "DIRECCION_ORIGEN": technician.direccion_origen,
                "LATITUD_SERVICIO_ORIGEN": technician.latitud,
                "LONGITUD_SERVICIO_ORIGEN": technician.longitud,
                "FECHA_INICIO_TURNO": format_datetime_value(start),
                "FECHA_FIN_TURNO": format_datetime_value(end),
                "SERVICIO": "CONDUCTOR ELEGIDO",
                "TIPO_SERVICIO": "PROGRAMADO",
                "DEPARTAMENTO": technician.departamento,
            }
            rows.append(row)
    return rows


def write_turnos_csv(rows: Iterable[dict[str, str]], output_path: Path) -> None:
    """Escribe el CSV de turnos."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TURNOS_HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variable requerida no configurada: {name}")
    return value


def load_env_file(path: Path) -> None:
    """Carga un .env simple sin sobrescribir variables existentes."""

    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def docker_process_env() -> dict[str, str]:
    """Configura Docker para WSL sin depender del credential helper del host."""

    env = os.environ.copy()
    docker_config = env.get("DOCKER_CONFIG", "/tmp/rve-docker-config")
    os.makedirs(docker_config, exist_ok=True)
    config_path = os.path.join(docker_config, "config.json")
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="ascii") as handle:
            handle.write("{}")
    env["DOCKER_CONFIG"] = docker_config
    return env


def docker_env_args() -> list[str]:
    """Construye variables para docker run."""

    spreadsheet_id = _required_env("SPREADSHEET_ID")
    service_account_file = Path(_required_env("SERVICE_ACCOUNT_FILE"))
    if not service_account_file.is_absolute():
        service_account_file = REPO_DIR / service_account_file
    args = [
        "-e",
        f"SPREADSHEET_ID={spreadsheet_id}",
        "-e",
        f"SHEET_TURNOS={os.getenv('SHEET_TURNOS', 'TURNOS_TECNICOS')}",
        "-e",
        f"SHEET_SERVICIOS={os.getenv('SHEET_SERVICIOS', 'SERVICIOS')}",
        "-e",
        f"SHEET_PREASIGNACIONES={os.getenv('SHEET_PREASIGNACIONES', 'PREASIGNACIONES')}",
        "-e",
        f"SHEET_CONFIG={os.getenv('SHEET_CONFIG', 'CONFIG')}",
        "-e",
        "SERVICE_ACCOUNT_FILE=/run/secrets/service-account.json",
        "-e",
        "ALLOW_TEST_CLOCK=true",
        "-e",
        f"DATADOG_ENABLED={os.getenv('DATADOG_ENABLED', 'false')}",
        "-e",
        f"ESTIMATE_GOOGLE_MATRIX_COST={os.getenv('ESTIMATE_GOOGLE_MATRIX_COST', 'false')}",
        "-v",
        f"{service_account_file}:/run/secrets/service-account.json:ro",
    ]
    endpoint_api_key = os.getenv("ENDPOINT_API_KEY", os.getenv("END_POINT_API_KEY", "")).strip()
    if endpoint_api_key:
        args.extend(["-e", f"ENDPOINT_API_KEY={endpoint_api_key}"])
    return args


def build_image() -> None:
    """Construye la imagen Docker local."""

    subprocess.run(
        ["docker", "build", "-t", IMAGE_NAME, str(APP_DIR)],
        check=True,
        env=docker_process_env(),
    )


def stop_container() -> None:
    """Detiene el contenedor de simulacion si existe."""

    subprocess.run(
        ["docker", "rm", "-f", CONTAINER_NAME],
        check=False,
        capture_output=True,
        env=docker_process_env(),
    )


def start_container() -> None:
    """Arranca el contenedor local para la simulacion."""

    command = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        CONTAINER_NAME,
        "-p",
        "18080:8080",
        *docker_env_args(),
        IMAGE_NAME,
    ]
    subprocess.run(command, check=True, env=docker_process_env())
    wait_for_health()


def wait_for_health(timeout_seconds: int = 45) -> None:
    """Espera a que el servicio quede disponible."""

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError("El contenedor no quedo saludable dentro del timeout")


def api_request(method: str, path: str, payload: dict | None = None, timeout_seconds: int = 60) -> dict:
    """Invoca un endpoint del microservicio y normaliza errores HTTP."""

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    endpoint_api_key = os.getenv("ENDPOINT_API_KEY", os.getenv("END_POINT_API_KEY", "")).strip()
    if endpoint_api_key:
        headers["X-Api-Key"] = endpoint_api_key
    request = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw_body = exc.read().decode("utf-8")
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            body = {"raw_body": raw_body}
        body["_http_status"] = exc.code
        return body
    except TimeoutError as exc:
        return {"_client_error": str(exc), "_error_type": "TimeoutError"}
    except URLError as exc:
        return {"_client_error": str(exc), "_error_type": "URLError"}


def api_request_query(method: str, path: str, params: dict[str, str], timeout_seconds: int = 60) -> dict:
    """Invoca un endpoint con query string."""

    query = urllib.parse.urlencode(params)
    return api_request(method, f"{path}?{query}", timeout_seconds=timeout_seconds)


def set_simulated_now(value: datetime) -> dict:
    """Fija el reloj simulado en el proceso FastAPI."""

    return api_request("POST", "/rve/_debug/now", {"now_value": format_datetime_value(value)})


def reset_lock() -> dict:
    """Limpia el lock desde el mismo proceso FastAPI."""

    return api_request("POST", "/rve/_debug/reset-lock")


def snapshot_auth(autorizacion: str) -> dict:
    """Obtiene snapshot debug de una autorizacion."""

    return api_request("GET", f"/rve/_debug/snapshot/{autorizacion}", timeout_seconds=30)


def normalize_decimal_text(value: str) -> str:
    """Normaliza coordenadas al formato recomendado para JSON."""

    text = str(value or "").strip().replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    return text


def _row_value(row: dict[str, str], *keys: str) -> str:
    """Lee columnas exactas o equivalentes por mayusculas/minusculas."""

    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    lower_map = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        value = lower_map.get(key.lower())
        if value not in (None, ""):
            return str(value).strip()
    return ""


def open_csv_dict_reader(csv_path: Path):
    """Abre CSVs exportados desde Sheets, Excel o Windows."""

    last_error: UnicodeDecodeError | None = None
    for encoding in CSV_ENCODINGS:
        try:
            handle = csv_path.open(newline="", encoding=encoding)
            reader = csv.DictReader(handle)
            # Fuerza lectura del header para detectar errores de encoding aqui.
            _ = reader.fieldnames
            return handle, reader
        except UnicodeDecodeError as exc:
            last_error = exc
            try:
                handle.close()
            except Exception:
                pass
    raise RuntimeError(f"No fue posible leer {csv_path} con codificaciones {CSV_ENCODINGS}") from last_error


def service_payload_from_row(row: dict[str, str], auth_prefix: str = "") -> dict[str, str]:
    """Convierte una fila del CSV cluster al contrato de /rve/servicio."""

    autorizacion = _row_value(row, "NUMERO_AUTORIZACION", "num_autorizacion")
    fecha_creacion_raw = _row_value(row, "FECHA_CREACION_SERVICIO", "FECHA_CREACION")
    fecha_creacion = format_datetime_value(parse_datetime_value(fecha_creacion_raw))
    fecha_servicio_raw = _row_value(
        row,
        "fecha_hora_programacion_servicio",
        "FECHA_HORA_PROGRAMACION_SERVICIO",
        "FECHA_SERVICIO",
    )
    return {
        "autorizacion": f"{auth_prefix}{autorizacion}",
        "caso": _row_value(row, "NUMERO_CASO", "numero_de_caso"),
        "placa": _row_value(row, "placa_identificacion"),
        "ciudad_origen": _row_value(row, "CIUDAD_ORIGEN", "origen"),
        "ciudad_destino": _row_value(row, "CIUDAD_DESTINO", "destino"),
        "departamento": _row_value(row, "DEPARTAMENTO", "departamento_asignacion"),
        "tipo_servicio": _row_value(row, "SERVICIO", "servicio_a_prestar") or "CONDUCTOR ELEGIDO",
        "modalidad_servicio": _row_value(row, "TIPO_SERVICIO", "clase_de_servicio") or "PROGRAMADO",
        "fecha_servicio": format_datetime_value(parse_datetime_value(fecha_servicio_raw)),
        "lat_origen": normalize_decimal_text(_row_value(row, "lat_origen", "ORIGEN_LAT")),
        "lng_origen": normalize_decimal_text(_row_value(row, "lon_origen", "ORIGEN_LON")),
        "lat_destino": normalize_decimal_text(_row_value(row, "lat_destino", "DESTINO_LAT")),
        "lng_destino": normalize_decimal_text(_row_value(row, "lon_destino", "DESTINO_LON")),
        "direccion_origen": _row_value(row, "direccion_origen", "ORIGEN_DIR"),
        "direccion_destino": _row_value(row, "direccion_destino", "DESTINO_DIR", "DIRECCION_DE_DESTINO"),
        "cedula_asegurado": _row_value(row, "CEDULA_ASEGURADO"),
        "asegurado": _row_value(row, "ASEGURADO"),
        "celular_asegurado": _row_value(row, "CELULAR_ASEGURADO"),
        "clv": _row_value(row, "CLV"),
        "tipo_ciudad": _row_value(row, "TIPO_CIUDAD"),
        "tipo_fallido": _row_value(row, "TIPO_FALLIDO"),
        "fecha_creacion_servicio": fecha_creacion,
        "fecha_recepcion_rb": fecha_creacion,
        "observaciones": _row_value(row, "OBSERVACIONES_SF", "NOM_ESTADO_SERVICIO", "ESTADO_SERVICIO"),
        "evidencias": _row_value(row, "ID_CITA_SERVICIO"),
    }


def cancellation_time_from_row(row: dict[str, str]) -> datetime | None:
    """Obtiene la fecha de cancelacion de una fila, si existe."""

    raw_value = _row_value(row, "FECHA_HORA_CANCELACION", "FECHA_HORA_CANCELACION_SERVICIO")
    if not raw_value:
        return None
    return parse_datetime_value(raw_value)


def turno_row_from_csv(row: dict[str, str], turno_prefix: str = "") -> dict[str, str]:
    """Normaliza una fila externa de turnos."""

    normalized = {header: str(row.get(header, "") or "").strip() for header in TURNOS_HEADERS}
    normalized["ID_TURNO"] = f"{turno_prefix}{normalized['ID_TURNO']}"
    normalized["LATITUD_SERVICIO_ORIGEN"] = normalize_decimal_text(normalized["LATITUD_SERVICIO_ORIGEN"])
    normalized["LONGITUD_SERVICIO_ORIGEN"] = normalize_decimal_text(normalized["LONGITUD_SERVICIO_ORIGEN"])
    normalized["FECHA_INICIO_TURNO"] = format_datetime_value(
        parse_datetime_value(normalized["FECHA_INICIO_TURNO"])
    )
    normalized["FECHA_FIN_TURNO"] = format_datetime_value(
        parse_datetime_value(normalized["FECHA_FIN_TURNO"])
    )
    return normalized


def load_turnos_csv(csv_path: Path, turno_prefix: str = "") -> list[dict[str, str]]:
    """Carga turnos desde un CSV externo."""

    handle, reader = open_csv_dict_reader(csv_path)
    with handle:
        return [turno_row_from_csv(row, turno_prefix=turno_prefix) for row in reader]


def load_service_payloads(
    csv_path: Path,
    from_date: str,
    to_date: str,
    auth_prefix: str,
    creation_start: datetime | None = None,
) -> list[dict[str, str]]:
    """Lee, filtra y transforma el CSV de servicios."""

    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    payloads: list[dict[str, str]] = []
    handle, reader = open_csv_dict_reader(csv_path)
    with handle:
        for row in reader:
            service_time = parse_datetime_value(
                _row_value(row, "fecha_hora_programacion_servicio", "FECHA_HORA_PROGRAMACION_SERVICIO", "FECHA_SERVICIO")
            )
            if start <= service_time <= end:
                payload = service_payload_from_row(row, auth_prefix=auth_prefix)
                if creation_start and parse_datetime_value(payload["fecha_creacion_servicio"]) < creation_start:
                    continue
                payloads.append(payload)
    return sorted(
        payloads,
        key=lambda item: (
            parse_datetime_value(item["fecha_creacion_servicio"]),
            parse_datetime_value(item["fecha_servicio"]),
            item["autorizacion"],
        ),
    )


def load_simulation_data(
    services_csv_path: Path,
    turnos_csv_path: Path,
    from_date: str,
    to_date: str,
    auth_prefix: str,
    turno_prefix: str,
    creation_start: datetime | None = None,
) -> LoadedSimulationData:
    """Carga servicios, cancelaciones y turnos para la corrida."""

    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    payloads: list[dict[str, str]] = []
    cancelaciones: dict[str, datetime] = {}

    handle, reader = open_csv_dict_reader(services_csv_path)
    with handle:
        for row in reader:
            service_time = parse_datetime_value(
                _row_value(row, "fecha_hora_programacion_servicio", "FECHA_HORA_PROGRAMACION_SERVICIO", "FECHA_SERVICIO")
            )
            if not (start <= service_time <= end):
                continue
            payload = service_payload_from_row(row, auth_prefix=auth_prefix)
            if creation_start and parse_datetime_value(payload["fecha_creacion_servicio"]) < creation_start:
                continue
            payloads.append(payload)
            cancellation_time = cancellation_time_from_row(row)
            if cancellation_time is not None:
                cancelaciones[payload["autorizacion"]] = cancellation_time

    payloads = sorted(
        payloads,
        key=lambda item: (
            parse_datetime_value(item["fecha_creacion_servicio"]),
            parse_datetime_value(item["fecha_servicio"]),
            item["autorizacion"],
        ),
    )
    turnos = load_turnos_csv(turnos_csv_path, turno_prefix=turno_prefix)
    if turnos:
        window_start = min(parse_datetime_value(row["FECHA_INICIO_TURNO"]) for row in turnos)
        window_end = max(parse_datetime_value(row["FECHA_FIN_TURNO"]) for row in turnos)
    elif payloads:
        window_start = min(parse_datetime_value(item["fecha_creacion_servicio"]) for item in payloads)
        window_end = max(parse_datetime_value(item["fecha_servicio"]) for item in payloads)
    else:
        window_start = start
        window_end = end
    return LoadedSimulationData(
        turnos=turnos,
        payloads=payloads,
        cancelaciones=cancelaciones,
        window_start=window_start,
        window_end=window_end,
    )


def build_events(
    payloads: Iterable[dict[str, str]],
    from_date: str = DEFAULT_FROM,
    to_date: str = DEFAULT_TO,
    validar_hourly: bool = True,
    cancelaciones: dict[str, datetime] | None = None,
    validar_every_minutes: int = DEFAULT_VALIDAR_EVERY_MINUTES,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[SimulationEvent]:
    """Construye la cola cronologica de servicios y validaciones."""

    events: list[SimulationEvent] = []
    cancelaciones = cancelaciones or {}
    for payload in payloads:
        service_time = parse_datetime_value(payload["fecha_servicio"])
        event_time = (
            parse_datetime_value(payload["fecha_creacion_servicio"])
            if payload.get("fecha_creacion_servicio")
            else service_time - timedelta(hours=2)
        )
        events.append(
            SimulationEvent(
                event_time=event_time,
                kind="servicio",
                label=f"servicio {payload['autorizacion']} fecha={payload['fecha_servicio']}",
                payload=payload,
                source_auth=payload["autorizacion"],
                priority=0,
            )
        )
        cancellation_time = cancelaciones.get(payload["autorizacion"])
        if cancellation_time is not None:
            events.append(
                SimulationEvent(
                    event_time=cancellation_time,
                    kind="cancelacion",
                    label=f"cancelacion {payload['autorizacion']}",
                    source_auth=payload["autorizacion"],
                    priority=1,
                )
            )
    if validar_hourly:
        if window_start is not None and window_end is not None:
            event_time = window_start
            step = timedelta(minutes=max(1, validar_every_minutes))
            while event_time <= window_end:
                events.append(
                    SimulationEvent(
                        event_time=event_time,
                        kind="validar",
                        label=f"validar {format_datetime_value(event_time)}",
                        priority=2,
                    )
                )
                event_time += step
        else:
            for shift_date in weekend_dates(from_date, to_date):
                start = shift_date.replace(hour=21, minute=0, second=0)
                for hour_offset in range(0, 8):
                    event_time = start + timedelta(hours=hour_offset)
                    events.append(
                        SimulationEvent(
                            event_time=event_time,
                            kind="validar",
                            label=f"validar {format_datetime_value(event_time)}",
                            priority=2,
                        )
                    )
    return sorted(events, key=lambda item: (item.event_time, item.priority, item.label))


def validation_window_for_simulation_date(
    simulation_date: str,
    payloads: Iterable[dict[str, str]],
    cancelaciones: dict[str, datetime],
    current_window_end: datetime,
) -> tuple[datetime, datetime]:
    """Valida desde inicio del dia simulado hasta el ultimo evento operativo."""

    window_start = datetime.strptime(simulation_date, "%Y-%m-%d")
    end_candidates = [current_window_end]
    for payload in payloads:
        end_candidates.append(parse_datetime_value(payload["fecha_servicio"]))
    end_candidates.extend(cancelaciones.values())
    return window_start, max(end_candidates)


def load_turnos_to_sheets(rows: list[dict[str, str]]) -> int:
    """Inserta en Sheets los turnos que no existan por ID_TURNO."""

    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    from services.google_sheets import GoogleSheetsRepository

    repository = GoogleSheetsRepository()
    sheet_name = repository.settings.sheet_turnos
    worksheet = repository.worksheet(sheet_name)
    headers = repository.read_headers(sheet_name)
    existing_ids = {
        str(row.get("ID_TURNO", "")).strip()
        for row in repository.read_rows(sheet_name, force_refresh=True)
    }
    duplicated = sorted(row["ID_TURNO"] for row in rows if row["ID_TURNO"] in existing_ids)
    if duplicated:
        raise RuntimeError(
            "La corrida no puede actualizar turnos existentes. "
            f"IDs duplicados: {', '.join(duplicated[:10])}"
        )
    inserted = 0
    for row in rows:
        values = [row.get(header, "") for header in headers]
        repository._call_with_retry(worksheet.append_row, values)
        existing_ids.add(row["ID_TURNO"])
        inserted += 1
    return inserted


def verify_dev_backend(expected_spreadsheet_id: str) -> dict:
    """Confirma que el backend local apunta al spreadsheet dev esperado."""

    response = api_request("GET", "/rve/_debug/config", timeout_seconds=15)
    if response.get("resultado") != "OK":
        raise RuntimeError(f"No fue posible confirmar configuracion debug: {response}")
    if response.get("spreadsheet_id") != expected_spreadsheet_id:
        raise RuntimeError(
            "Backend no apunta al spreadsheet dev esperado. "
            f"expected={expected_spreadsheet_id} actual={response.get('spreadsheet_id')}"
        )
    if response.get("allow_test_clock") is not True:
        raise RuntimeError(f"ALLOW_TEST_CLOCK no esta activo en el backend: {response}")
    return response


def reset_google_matrix_estimates() -> None:
    """Limpia acumulados del estimador en el backend."""

    response = api_request("POST", "/rve/_debug/google-matrix-estimates/reset", timeout_seconds=15)
    if response.get("resultado") != "OK":
        raise RuntimeError(f"No fue posible limpiar estimaciones de matriz: {response}")


def fetch_google_matrix_estimates() -> list[dict]:
    """Obtiene estimaciones acumuladas del backend."""

    response = api_request("GET", "/rve/_debug/google-matrix-estimates", timeout_seconds=15)
    if response.get("resultado") != "OK":
        raise RuntimeError(f"No fue posible obtener estimaciones de matriz: {response}")
    return list(response.get("records") or [])


def _overlaps_window(start_text: str, end_text: str, window_start: datetime, window_end: datetime) -> bool:
    try:
        row_start = parse_datetime_value(start_text)
        row_end = parse_datetime_value(end_text)
    except ValueError:
        return False
    return row_start <= window_end and row_end >= window_start


def assert_no_foreign_rows_in_window(
    run_prefix: str,
    window_start: datetime,
    window_end: datetime,
) -> None:
    """Aborta si hay datos ajenos cruzados con la ventana de simulacion."""

    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    from services.google_sheets import GoogleSheetsRepository

    repository = GoogleSheetsRepository()
    foreign_turns = []
    for row in repository.read_rows(repository.settings.sheet_turnos, force_refresh=True):
        turno_id = str(row.get("ID_TURNO", "")).strip()
        if turno_id.startswith(run_prefix):
            continue
        if _overlaps_window(
            str(row.get("FECHA_INICIO_TURNO", "")),
            str(row.get("FECHA_FIN_TURNO", "")),
            window_start,
            window_end,
        ):
            foreign_turns.append(turno_id or f"row:{row.get('_row')}")

    foreign_services = []
    for row in repository.read_rows(repository.settings.sheet_servicios, force_refresh=True):
        autorizacion = str(row.get("AUTORIZACION", "")).strip()
        if autorizacion.startswith(run_prefix):
            continue
        try:
            service_time = parse_datetime_value(str(row.get("FECHA_SERVICIO", "")))
        except ValueError:
            continue
        if window_start <= service_time <= window_end:
            foreign_services.append(autorizacion or f"row:{row.get('_row')}")

    if foreign_turns or foreign_services:
        raise RuntimeError(
            "La simulacion no continua porque hay datos ajenos en la ventana. "
            "No se borrara nada. "
            f"turnos_ajenos={foreign_turns[:10]} servicios_ajenos={foreign_services[:10]}"
        )


def run_simulation(events: list[SimulationEvent], delay_seconds: int, snapshot_on_validar: bool) -> list[dict]:
    """Ejecuta eventos contra el contenedor."""

    report = []
    known_auths: list[str] = []
    totals = {
        "servicio": sum(1 for event in events if event.kind == "servicio"),
        "validar": sum(1 for event in events if event.kind == "validar"),
        "cancelacion": sum(1 for event in events if event.kind == "cancelacion"),
    }
    progress = {
        "servicio": 0,
        "validar": 0,
        "cancelacion": 0,
        "aceptados": 0,
        "rechazados": 0,
        "cancelados": 0,
        "errores": 0,
    }
    total_events = len(events)
    for index, event in enumerate(events, start=1):
        expected_now = format_datetime_value(event.event_time)
        clock_response = set_simulated_now(event.event_time)
        if (
            clock_response.get("resultado") != "OK"
            or clock_response.get("hora_simulada") != expected_now
        ):
            raise RuntimeError(
                "No fue posible fijar el reloj simulado "
                f"expected={expected_now} response={clock_response}"
            )
        lock_response = reset_lock()
        if lock_response.get("resultado") != "OK":
            raise RuntimeError(f"No fue posible limpiar el lock response={lock_response}")
        print(
            f"RUNNING kind={event.kind} now={expected_now} label={event.label}",
            flush=True,
        )
        print(
            "PROGRESS "
            f"event={index}/{total_events} "
            f"services={progress['servicio']}/{totals['servicio']} "
            f"validar={progress['validar']}/{totals['validar']} "
            f"cancelaciones={progress['cancelacion']}/{totals['cancelacion']} "
            f"aceptados={progress['aceptados']} "
            f"rechazados={progress['rechazados']} "
            f"cancelados={progress['cancelados']} "
            f"errores={progress['errores']}",
            flush=True,
        )
        started = time.monotonic()
        if event.kind == "servicio" and event.payload:
            response = api_request("POST", "/rve/servicio", event.payload, timeout_seconds=90)
            if event.source_auth and event.source_auth not in known_auths:
                known_auths.append(event.source_auth)
        elif event.kind == "cancelacion" and event.source_auth:
            response = api_request_query(
                "POST",
                "/rve/cancelacion",
                {"autorizacion": event.source_auth},
                timeout_seconds=90,
            )
        elif event.kind == "validar":
            response = api_request("POST", "/rve/validar", timeout_seconds=120)
        else:
            raise RuntimeError(f"Evento invalido: {event}")
        duration_ms = int((time.monotonic() - started) * 1000)
        item = {
            "kind": event.kind,
            "label": event.label,
            "now": expected_now,
            "duration_ms": duration_ms,
            "response": response,
        }
        if event.kind in {"servicio", "cancelacion"} and event.source_auth:
            item["snapshot"] = snapshot_auth(event.source_auth)
        if event.kind == "validar" and snapshot_on_validar:
            item["snapshots"] = [snapshot_auth(autorizacion) for autorizacion in known_auths]
        report.append(item)
        if event.kind == "servicio":
            progress["servicio"] += 1
            decision = str(response.get("decision", "")).upper()
            if decision == "ACEPTAR":
                progress["aceptados"] += 1
            elif decision == "RECHAZAR":
                progress["rechazados"] += 1
            elif response.get("_http_status") or response.get("_client_error"):
                progress["errores"] += 1
            print(
                "DONE "
                f"kind={event.kind} duration_ms={duration_ms} "
                f"decision={response.get('decision', '')} "
                f"turno={response.get('id_turno') or ''} "
                f"conductor={response.get('nombre_conductor') or ''} "
                f"razon={response.get('razon', '')}",
                flush=True,
            )
        elif event.kind == "validar":
            progress["validar"] += 1
            http_status = response.get("_http_status", "")
            client_error = response.get("_client_error", "")
            if http_status or client_error:
                progress["errores"] += 1
            print(
                "DONE "
                f"kind={event.kind} duration_ms={duration_ms} "
                f"resultado={response.get('resultado', '')} "
                f"cantidad={response.get('cantidad', '')} "
                f"http_status={http_status} "
                f"error={client_error or response.get('detail', '')}",
                flush=True,
            )
        elif event.kind == "cancelacion":
            progress["cancelacion"] += 1
            if str(response.get("resultado", "")).upper() in {"OK", "NO_ENCONTRADO"}:
                progress["cancelados"] += 1
            elif response.get("_http_status") or response.get("_client_error"):
                progress["errores"] += 1
            print(
                "DONE "
                f"kind={event.kind} duration_ms={duration_ms} "
                f"resultado={response.get('resultado', '')} "
                f"estado={response.get('estado', '')}",
                flush=True,
            )
        else:
            print(f"DONE kind={event.kind} duration_ms={duration_ms}", flush=True)
        if event.kind == "servicio" and delay_seconds > 0:
            time.sleep(delay_seconds)
    print(
        "PROGRESS_DONE "
        f"events={total_events} "
        f"services={progress['servicio']} "
        f"validar={progress['validar']} "
        f"cancelaciones={progress['cancelacion']} "
        f"aceptados={progress['aceptados']} "
        f"rechazados={progress['rechazados']} "
        f"cancelados={progress['cancelados']} "
        f"errores={progress['errores']}",
        flush=True,
    )
    return report


def _float_value(value: str) -> float:
    text = normalize_decimal_text(value)
    return float(text) if text else 0.0


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371.0
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _travel_minutes(distance_km: float, average_speed_kmh: float) -> float:
    if average_speed_kmh <= 0:
        return 0.0
    return (distance_km / average_speed_kmh) * 60.0


def summarize_google_matrix_estimates(
    records: list[dict],
    price_per_1000_usd: float,
    max_elements_per_request: int,
) -> dict:
    """Resume registros de costo Distance Matrix sin aplicar free tier."""

    total_core = sum(int(record.get("core_elements", 0) or 0) for record in records)
    total_full = sum(int(record.get("full_elements", 0) or 0) for record in records)
    max_record = max(records, key=lambda item: int(item.get("full_elements", 0) or 0), default={})
    return {
        "provider": "google_distance_matrix_legacy",
        "sku": GOOGLE_DISTANCE_MATRIX_SKU,
        "traffic_model": "best_guess",
        "free_tier_applied": False,
        "records": records,
        "total_core_elements": total_core,
        "total_full_elements": total_full,
        "price_per_1000_usd": price_per_1000_usd,
        "max_elements_per_request": max_elements_per_request,
        "estimated_core_cost_usd": round((total_core / 1000.0) * price_per_1000_usd, 4),
        "estimated_full_cost_usd": round((total_full / 1000.0) * price_per_1000_usd, 4),
        "estimated_core_matrix_requests": math.ceil(total_core / max(1, max_elements_per_request)),
        "estimated_full_matrix_requests": math.ceil(total_full / max(1, max_elements_per_request)),
        "max_single_solve_elements": int(max_record.get("full_elements", 0) or 0),
        "max_single_solve_context": {
            "endpoint": max_record.get("endpoint", ""),
            "request_id": max_record.get("request_id", ""),
            "autorizacion": max_record.get("autorizacion", ""),
            "department": max_record.get("department", ""),
            "timestamp": max_record.get("timestamp", ""),
        },
    }


def _latest_snapshots(report: list[dict]) -> dict[str, dict]:
    snapshots: dict[str, dict] = {}
    for item in report:
        snapshot = item.get("snapshot")
        if isinstance(snapshot, dict) and snapshot.get("autorizacion"):
            snapshots[snapshot["autorizacion"]] = snapshot
        for nested in item.get("snapshots") or []:
            if isinstance(nested, dict) and nested.get("autorizacion"):
                snapshots[nested["autorizacion"]] = nested
    return snapshots


def _payloads_by_auth(payloads: Iterable[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {payload["autorizacion"]: payload for payload in payloads}


def _turnos_by_id(turnos: Iterable[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {turno["ID_TURNO"]: turno for turno in turnos}


def _turn_minutes(turno: dict[str, str]) -> float:
    start = parse_datetime_value(turno["FECHA_INICIO_TURNO"])
    end = parse_datetime_value(turno["FECHA_FIN_TURNO"])
    return max(0.0, (end - start).total_seconds() / 60.0)


def compute_kpis(
    *,
    run_id: str,
    simulation_date: str,
    spreadsheet_id: str,
    payloads: list[dict[str, str]],
    turnos: list[dict[str, str]],
    report: list[dict],
    google_matrix_summary: dict,
    route_minutes_provider=None,
    replay_source: str = "haversine",
) -> dict:
    """Calcula KPIs finales de la corrida."""

    service_events = [item for item in report if item.get("kind") == "servicio"]
    cancel_events = [item for item in report if item.get("kind") == "cancelacion"]
    accepted = [
        item for item in service_events
        if str(item.get("response", {}).get("decision", "")).upper() == "ACEPTAR"
    ]
    rejected = [
        item for item in service_events
        if str(item.get("response", {}).get("decision", "")).upper() == "RECHAZAR"
    ]
    rejection_reasons = Counter(
        str(item.get("response", {}).get("razon", "") or "SIN_RAZON")
        for item in rejected
    )
    snapshots = _latest_snapshots(report)
    payload_by_auth = _payloads_by_auth(payloads)
    turno_by_id = _turnos_by_id(turnos)
    final_assigned = []
    for auth, snapshot in snapshots.items():
        state = str(snapshot.get("servicio_estado", "")).upper()
        if state in {"CANCELADO", "RECHAZADO_RVE"}:
            continue
        if snapshot.get("id_turno") and auth in payload_by_auth:
            final_assigned.append(auth)

    onsite_minutes = float(os.getenv("ONSITE_MINUTES", "10"))
    average_speed_kmh = float(os.getenv("AVERAGE_SPEED_KMH", "26"))
    total_shift_minutes = sum(_turn_minutes(turno) for turno in turnos)
    prestation_minutes = 0.0
    total_busy_minutes = 0.0
    punctual_count = 0

    services_by_turn: dict[str, list[str]] = {}
    for auth in final_assigned:
        turn_id = snapshots[auth].get("id_turno", "")
        services_by_turn.setdefault(turn_id, []).append(auth)

    for turn_id, auths in services_by_turn.items():
        turno = turno_by_id.get(turn_id)
        if not turno:
            continue
        current_lat = _float_value(turno["LATITUD_SERVICIO_ORIGEN"])
        current_lng = _float_value(turno["LONGITUD_SERVICIO_ORIGEN"])
        current_time = parse_datetime_value(turno["FECHA_INICIO_TURNO"])
        ordered_auths = sorted(
            auths,
            key=lambda auth: (
                parse_datetime_value(payload_by_auth[auth]["fecha_servicio"]),
                auth,
            ),
        )
        for auth in ordered_auths:
            payload = payload_by_auth[auth]
            origin_lat = _float_value(payload["lat_origen"])
            origin_lng = _float_value(payload["lng_origen"])
            dest_lat = _float_value(payload["lat_destino"])
            dest_lng = _float_value(payload["lng_destino"])
            service_time = parse_datetime_value(payload["fecha_servicio"])
            if route_minutes_provider:
                travel_to_origin = route_minutes_provider(current_lat, current_lng, origin_lat, origin_lng)
            else:
                travel_to_origin = _travel_minutes(
                    _haversine_km(current_lat, current_lng, origin_lat, origin_lng),
                    average_speed_kmh,
                )
            arrival = current_time + timedelta(minutes=travel_to_origin)
            start_service = max(arrival, service_time)
            if route_minutes_provider:
                trip_minutes = route_minutes_provider(origin_lat, origin_lng, dest_lat, dest_lng)
            else:
                trip_minutes = _travel_minutes(
                    _haversine_km(origin_lat, origin_lng, dest_lat, dest_lng),
                    average_speed_kmh,
                )
            prestation = onsite_minutes + trip_minutes
            total_busy_minutes += travel_to_origin + prestation
            prestation_minutes += prestation
            if start_service <= service_time:
                punctual_count += 1
            current_time = start_service + timedelta(minutes=prestation)
            current_lat = dest_lat
            current_lng = dest_lng

    received_count = len(service_events)
    rejected_count = len(rejected)
    final_assigned_count = len(final_assigned)
    turn_count = len(turnos)
    return {
        "run_id": run_id,
        "simulation_date": simulation_date,
        "spreadsheet_id": spreadsheet_id,
        "servicios_recibidos": received_count,
        "servicios_aceptados": len(accepted),
        "servicios_rechazados": rejected_count,
        "porcentaje_rechazados": round((rejected_count / received_count) * 100, 2) if received_count else 0.0,
        "motivos_rechazo": dict(sorted(rejection_reasons.items())),
        "motivos_rechazo_json": json.dumps(dict(sorted(rejection_reasons.items())), ensure_ascii=True),
        "servicios_cancelados": len(
            [
                item for item in cancel_events
                if str(item.get("response", {}).get("resultado", "")).upper() in {"OK", "NO_ENCONTRADO"}
            ]
        ),
        "turnos_cargados": turn_count,
        "servicios_asignados_final_sin_cancelados": final_assigned_count,
        "servicios_por_turno_realizado": round(final_assigned_count / turn_count, 4) if turn_count else 0.0,
        "ocupacion_total_promedio_pct": round((total_busy_minutes / total_shift_minutes) * 100, 2)
        if total_shift_minutes else 0.0,
        "ocupacion_prestacion_promedio_pct": round((prestation_minutes / total_shift_minutes) * 100, 2)
        if total_shift_minutes else 0.0,
        "servicios_puntuales": punctual_count,
        "porcentaje_servicios_puntuales": round((punctual_count / final_assigned_count) * 100, 2)
        if final_assigned_count else 0.0,
        "google_matrix_core_elements": google_matrix_summary.get("total_core_elements", 0),
        "google_matrix_full_elements": google_matrix_summary.get("total_full_elements", 0),
        "google_matrix_estimated_core_cost_usd": google_matrix_summary.get("estimated_core_cost_usd", 0.0),
        "google_matrix_estimated_full_cost_usd": google_matrix_summary.get("estimated_full_cost_usd", 0.0),
        "replay_source": replay_source,
    }


def write_summary_csv(summary: dict, output_path: Path) -> None:
    """Escribe un CSV de una fila con KPIs."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    csv_summary = {
        key: json.dumps(value, ensure_ascii=True) if isinstance(value, (dict, list)) else value
        for key, value in summary.items()
    }
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_summary.keys()))
        writer.writeheader()
        writer.writerow(csv_summary)


def resolve_default_auth_prefix(value: str | None) -> str:
    """Resuelve el prefijo de autorizacion por defecto."""

    if value is not None:
        return value
    return f"SIMCLUSTER-{int(time.time())}-"


def resolve_run_id(value: str | None) -> str:
    """Resuelve el identificador de corrida."""

    if value:
        return value.strip()
    return f"SIMCLUSTER-{int(time.time())}"


def resolve_google_maps_api_key(api_key: str = "", api_key_file: str = "") -> str:
    """Resuelve la llave de Google Maps sin imprimirla en comandos ni reportes."""

    if api_key:
        return api_key.strip()
    if api_key_file:
        return Path(api_key_file).read_text(encoding="utf-8").strip()
    return os.getenv("GOOGLE_MAPS_API_KEY", "").strip()


def parse_args() -> argparse.Namespace:
    """Parsea argumentos CLI."""

    parser = argparse.ArgumentParser(description="Simulacion cluster RVE con Docker y test clock")
    parser.add_argument("--csv", default=str(SIMULATION_INPUTS_DIR / "servicios.csv"))
    parser.add_argument("--services-csv", default="")
    parser.add_argument("--turnos-csv", default="")
    parser.add_argument("--from-date", default=DEFAULT_FROM)
    parser.add_argument("--to-date", default=DEFAULT_TO)
    parser.add_argument("--simulation-date", default="")
    parser.add_argument("--delay-seconds", type=int, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--service-delay-seconds", type=int, default=None)
    parser.add_argument("--validar-every-minutes", type=int, default=DEFAULT_VALIDAR_EVERY_MINUTES)
    parser.add_argument("--creation-lookback-days", type=int, default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--auth-prefix", default=None)
    parser.add_argument("--turno-prefix", default=None)
    parser.add_argument("--spreadsheet-id", default=DEV_SPREADSHEET_ID)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--generate-turnos-csv", action="store_true")
    parser.add_argument("--turnos-output", default=str(SIMULATION_INPUTS_DIR / TURNOS_CSV_NAME))
    parser.add_argument("--load-turnos", action="store_true")
    parser.add_argument("--no-validar-hourly", action="store_true")
    parser.add_argument("--no-snapshots-on-validar", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-container", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--env-file", default=str(REPO_DIR / ".env"))
    parser.add_argument("--report-output", default=str(SIMULATION_RUNS_DIR / "cluster_simulation_report.json"))
    parser.add_argument("--summary-output", default=str(SIMULATION_RUNS_DIR / "cluster_simulation_summary.csv"))
    parser.add_argument("--estimate-google-matrix-cost", action="store_true")
    parser.add_argument("--use-google-distance-matrix-replay", action="store_true")
    parser.add_argument("--use-google-routes-replay", action="store_true")
    parser.add_argument("--skip-google-distance-matrix-replay", action="store_true")
    parser.add_argument("--google-maps-api-key", default="")
    parser.add_argument("--google-maps-api-key-file", default="")
    parser.add_argument(
        "--google-matrix-price-per-1000-usd",
        type=float,
        default=DEFAULT_GOOGLE_MATRIX_PRICE_PER_1000_USD,
    )
    parser.add_argument(
        "--google-matrix-max-elements-per-request",
        type=int,
        default=DEFAULT_GOOGLE_MATRIX_MAX_ELEMENTS_PER_REQUEST,
    )
    return parser.parse_args()


def main() -> None:
    """Punto de entrada CLI."""

    global BASE_URL

    args = parse_args()
    load_env_file(Path(args.env_file))
    BASE_URL = args.base_url.rstrip("/")
    os.environ["SPREADSHEET_ID"] = args.spreadsheet_id
    os.environ["ESTIMATE_GOOGLE_MATRIX_COST"] = (
        "true" if args.estimate_google_matrix_cost else "false"
    )
    run_id = resolve_run_id(args.run_id)
    run_prefix = f"{run_id}-"
    auth_prefix = args.auth_prefix if args.auth_prefix is not None else run_prefix
    turno_prefix = args.turno_prefix if args.turno_prefix is not None else run_prefix
    if (
        args.turnos_csv
        and not args.simulation_date
        and args.from_date == DEFAULT_FROM
        and args.to_date == DEFAULT_TO
    ):
        raise RuntimeError(
            "Cuando usa --turnos-csv debe indicar --simulation-date YYYY-MM-DD "
            "o un rango explicito con --from-date/--to-date."
        )
    if args.simulation_date:
        simulation_start_date = datetime.strptime(args.simulation_date, "%Y-%m-%d")
        from_date = args.simulation_date
        to_date = (simulation_start_date + timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        from_date = args.from_date
        to_date = args.to_date
        simulation_start_date = datetime.strptime(from_date, "%Y-%m-%d")
    creation_start = None
    if args.creation_lookback_days is not None:
        creation_start = simulation_start_date - timedelta(days=max(0, args.creation_lookback_days))

    generated_turnos = generate_turnos(from_date, to_date)
    if args.generate_turnos_csv:
        turnos_output = Path(args.turnos_output)
        write_turnos_csv(generated_turnos, turnos_output)
        print(f"TURNOS_CSV path={turnos_output} rows={len(generated_turnos)}", flush=True)

    services_csv = Path(args.services_csv or args.csv)
    if args.turnos_csv:
        simulation_data = load_simulation_data(
            services_csv,
            Path(args.turnos_csv),
            from_date,
            to_date,
            auth_prefix,
            turno_prefix,
            creation_start=creation_start,
        )
        turnos = simulation_data.turnos
        payloads = simulation_data.payloads
        cancelaciones = simulation_data.cancelaciones
        window_start = simulation_data.window_start
        window_end = simulation_data.window_end
    else:
        turnos = generated_turnos
        payloads = load_service_payloads(services_csv, from_date, to_date, auth_prefix, creation_start=creation_start)
        cancelaciones = {}
        window_start = min(
            [parse_datetime_value(row["FECHA_INICIO_TURNO"]) for row in turnos]
            or [datetime.strptime(from_date, "%Y-%m-%d")]
        )
        window_end = max(
            [parse_datetime_value(row["FECHA_FIN_TURNO"]) for row in turnos]
            or [datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)]
        )
    if args.simulation_date:
        window_start, window_end = validation_window_for_simulation_date(
            args.simulation_date,
            payloads,
            cancelaciones,
            window_end,
        )

    events = build_events(
        payloads,
        from_date=from_date,
        to_date=to_date,
        validar_hourly=not args.no_validar_hourly,
        cancelaciones=cancelaciones,
        validar_every_minutes=args.validar_every_minutes,
        window_start=window_start,
        window_end=window_end,
    )
    delay_seconds = args.service_delay_seconds if args.service_delay_seconds is not None else args.delay_seconds
    print(
        f"SIMULATION payloads={len(payloads)} events={len(events)} "
        f"validar_hourly={not args.no_validar_hourly} delay_seconds={delay_seconds} "
        f"run_id={run_id} spreadsheet_id={args.spreadsheet_id} "
        f"creation_start={format_datetime_value(creation_start) if creation_start else ''}",
        flush=True,
    )
    if args.dry_run:
        preview = [
            {
                "kind": event.kind,
                "now": format_datetime_value(event.event_time),
                "label": event.label,
                "autorizacion": event.source_auth,
            }
            for event in events[:20]
        ]
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "turnos": len(turnos),
                    "payloads": len(payloads),
                    "cancelaciones": len(cancelaciones),
                    "creation_start": format_datetime_value(creation_start) if creation_start else "",
                    "events_preview": preview,
                },
                indent=2,
            )
        )
        return

    use_google_replay = not args.skip_google_distance_matrix_replay
    google_maps_api_key = ""
    if use_google_replay:
        google_maps_api_key = resolve_google_maps_api_key(
            args.google_maps_api_key,
            args.google_maps_api_key_file,
        )
        if not google_maps_api_key:
            raise RuntimeError(
                "GOOGLE_MAPS_API_KEY requerido para KPIs de puntualidad. "
                "Use --google-maps-api-key-file o --google-maps-api-key."
            )

    if not args.skip_build:
        build_image()
    stop_container()
    start_container()
    try:
        debug_config = verify_dev_backend(args.spreadsheet_id)
        print(f"DEBUG_CONFIG {json.dumps(debug_config, ensure_ascii=True)}", flush=True)
        if args.estimate_google_matrix_cost:
            reset_google_matrix_estimates()
        if args.load_turnos:
            assert_no_foreign_rows_in_window(run_prefix, window_start, window_end)
            inserted = load_turnos_to_sheets(turnos)
            print(f"TURNOS_SHEETS inserted={inserted}", flush=True)
        report = run_simulation(
            events,
            delay_seconds=delay_seconds,
            snapshot_on_validar=not args.no_snapshots_on_validar,
        )
        estimate_records = fetch_google_matrix_estimates() if args.estimate_google_matrix_cost else []
        google_matrix_summary = summarize_google_matrix_estimates(
            estimate_records,
            price_per_1000_usd=args.google_matrix_price_per_1000_usd,
            max_elements_per_request=args.google_matrix_max_elements_per_request,
        )
        route_provider = None
        replay_source = "haversine"
        if use_google_replay:
            google_client = GoogleDistanceMatrixClient(
                api_key=google_maps_api_key,
                departure_time=google_departure_reference(from_date),
            )
            route_provider = google_client.travel_minutes
            replay_source = "google_distance_matrix"
        try:
            summary = compute_kpis(
                run_id=run_id,
                simulation_date=from_date,
                spreadsheet_id=args.spreadsheet_id,
                payloads=payloads,
                turnos=turnos,
                report=report,
                google_matrix_summary=google_matrix_summary,
                route_minutes_provider=route_provider,
                replay_source=replay_source,
            )
        except RuntimeError as exc:
            if not route_provider:
                raise
            print(f"GOOGLE_DISTANCE_MATRIX_REPLAY_FAILED fallback=haversine error={exc}", flush=True)
            summary = compute_kpis(
                run_id=run_id,
                simulation_date=from_date,
                spreadsheet_id=args.spreadsheet_id,
                payloads=payloads,
                turnos=turnos,
                report=report,
                google_matrix_summary=google_matrix_summary,
                replay_source="haversine_fallback_after_google_distance_matrix_error",
            )
            summary["google_distance_matrix_error"] = str(exc)[:500]
        report_output = Path(args.report_output)
        report_output.parent.mkdir(parents=True, exist_ok=True)
        report_payload = {
            "run_id": run_id,
            "spreadsheet_id": args.spreadsheet_id,
            "events": report,
            "summary": summary,
            "google_matrix_cost_estimate": google_matrix_summary,
        }
        report_output.write_text(json.dumps(report_payload, indent=2, ensure_ascii=True), encoding="utf-8")
        summary_output = Path(args.summary_output)
        write_summary_csv(summary, summary_output)
        print(f"REPORT path={report_output} items={len(report)}", flush=True)
        print(f"SUMMARY path={summary_output}", flush=True)
        print(f"SUMMARY_JSON {json.dumps(summary, ensure_ascii=True)}", flush=True)
    finally:
        if not args.keep_container:
            stop_container()


if __name__ == "__main__":
    main()
