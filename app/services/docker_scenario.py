"""Runner local para simular varios dias de operacion usando Docker."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from urllib.error import URLError
from dataclasses import dataclass
from typing import List

IMAGE_NAME = "rve-local-sim"
CONTAINER_NAME = "rve-local-sim"
BASE_URL = "http://127.0.0.1:18080"


@dataclass(frozen=True)
class SimulationStep:
    """Representa una accion del escenario."""

    kind: str
    now_value: str
    payload: dict | None = None
    autorizacion: str | None = None
    label: str = ""


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variable requerida no configurada: {name}")
    return value


def build_image() -> None:
    """Construye la imagen Docker local."""

    subprocess.run(
        ["docker", "build", "-t", IMAGE_NAME, "./app"],
        check=True,
        env=docker_process_env(),
    )


def stop_container() -> None:
    """Detiene el contenedor si existe."""

    subprocess.run(
        ["docker", "rm", "-f", CONTAINER_NAME],
        check=False,
        capture_output=True,
        env=docker_process_env(),
    )


def start_container() -> None:
    """Arranca el contenedor una sola vez para toda la simulacion."""

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


def wait_for_health(timeout_seconds: int = 30) -> None:
    """Espera hasta que el contenedor responda health."""

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{BASE_URL}/health", timeout=3) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError("El contenedor no quedo saludable dentro del timeout")


def api_post(path: str, payload: dict | None = None, timeout_seconds: int = 15) -> dict:
    """Invoca un endpoint POST del microservicio."""

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    endpoint_api_key = os.getenv("ENDPOINT_API_KEY", os.getenv("END_POINT_API_KEY", "")).strip()
    if endpoint_api_key:
        headers["X-Api-Key"] = endpoint_api_key
    request = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw_body = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            payload = {"raw_body": raw_body}
        payload["_http_status"] = exc.code
        return payload
    except TimeoutError as exc:
        return {"_client_error": str(exc), "_error_type": "TimeoutError"}
    except URLError as exc:
        return {"_client_error": str(exc), "_error_type": "URLError"}


def api_post_query(path: str, params: dict[str, str], timeout_seconds: int = 15) -> dict:
    """Invoca endpoints POST con query string."""

    query = urllib.parse.urlencode(params)
    return api_post(f"{path}?{query}", timeout_seconds=timeout_seconds)


def api_get(path: str, timeout_seconds: int = 15) -> dict:
    """Invoca un endpoint GET del microservicio."""

    headers = {}
    endpoint_api_key = os.getenv("ENDPOINT_API_KEY", os.getenv("END_POINT_API_KEY", "")).strip()
    if endpoint_api_key:
        headers["X-Api-Key"] = endpoint_api_key
    request = urllib.request.Request(f"{BASE_URL}{path}", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw_body = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            payload = {"raw_body": raw_body}
        payload["_http_status"] = exc.code
        return payload
    except TimeoutError as exc:
        return {"_client_error": str(exc), "_error_type": "TimeoutError"}
    except URLError as exc:
        return {"_client_error": str(exc), "_error_type": "URLError"}


def set_simulated_now(now_value: str) -> dict:
    """Fija la hora simulada del proceso en ejecucion."""

    return api_post("/rve/_debug/now", {"now_value": now_value}, timeout_seconds=15)


def reset_lock() -> dict:
    """Limpia el lock desde el mismo backend en ejecucion."""

    return api_post("/rve/_debug/reset-lock", timeout_seconds=15)


def make_service(auth: str, caso: str, fecha: str, lat_o: str, lng_o: str, lat_d: str, lng_d: str, origen: str, destino: str) -> dict:
    """Construye un payload de servicio."""

    return {
        "autorizacion": auth,
        "caso": caso,
        "placa": "SIM123",
        "ciudad_origen": origen,
        "ciudad_destino": destino,
        "departamento": "CUNDINAMARCA",
        "tipo_servicio": "CONDUCTOR ELEGIDO",
        "fecha_servicio": fecha,
        "lat_origen": lat_o,
        "lng_origen": lng_o,
        "lat_destino": lat_d,
        "lng_destino": lng_d,
        "direccion_origen": f"Origen {auth}",
        "direccion_destino": f"Destino {auth}",
        "cedula_asegurado": f"9{caso[-6:]}",
        "asegurado": f"Asegurado {auth}",
        "celular_asegurado": "3000000000",
        "clv": "ORO",
        "tipo_ciudad": "URBANO",
        "observaciones": "Simulacion Docker local",
    }


def scenario_steps(prefix: str) -> List[SimulationStep]:
    """Define 10 escenarios de varios dias."""

    return [
        SimulationStep(
            kind="create",
            now_value="2026-04-19 18:30:00",
            label="Aceptado turno 3",
            payload=make_service(f"{prefix}-01", "SIM0001", "2026-04-19 21:30:00", "4,700", "-74,051", "4,690", "-74,080", "BOGOTA", "BOGOTA"),
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-19 18:35:00",
            label="Aceptado turno 3 ruta secuencial",
            payload=make_service(f"{prefix}-02", "SIM0002", "2026-04-19 22:40:00", "4,702", "-74,060", "4,720", "-74,090", "BOGOTA", "LA CALERA"),
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-19 18:40:00",
            label="Aceptado turno 3 cierre de ruta",
            payload=make_service(f"{prefix}-03", "SIM0003", "2026-04-20 00:10:00", "4,720", "-74,090", "4,700", "-74,051", "LA CALERA", "BOGOTA"),
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-19 18:45:00",
            label="Rechazo por overlap",
            payload=make_service(f"{prefix}-04", "SIM0004", "2026-04-19 21:35:00", "4,701", "-74,052", "4,700", "-74,060", "BOGOTA", "BOGOTA"),
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-19 20:20:00",
            label="Rechazo por menos de 1 hora",
            payload=make_service(f"{prefix}-05", "SIM0005", "2026-04-19 20:50:00", "4,700", "-74,051", "4,690", "-74,070", "BOGOTA", "BOGOTA"),
        ),
        SimulationStep(
            kind="validate",
            now_value="2026-04-19 20:31:00",
            label="Congelacion 19 abril",
        ),
        SimulationStep(
            kind="complete",
            now_value="2026-04-20 01:30:00",
            label="Completar servicio congelado",
            autorizacion=f"{prefix}-01",
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-20 10:00:00",
            label="Aceptado turno 6 jornada larga",
            payload=make_service(f"{prefix}-06", "SIM0006", "2026-04-20 15:00:00", "4,700", "-74,051", "4,730", "-74,100", "BOGOTA", "CHIA"),
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-20 10:10:00",
            label="Aceptado con decimal punto",
            payload=make_service(f"{prefix}-07", "SIM0007", "2026-04-20 18:00:00", "4.730", "-74.100", "4.700", "-74.051", "CHIA", "BOGOTA"),
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-20 10:15:00",
            label="Rechazo por radio > 60 km",
            payload=make_service(f"{prefix}-08", "SIM0008", "2026-04-20 18:30:00", "5,200", "-74,600", "5,250", "-74,650", "LEJOS", "LEJOS"),
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-20 11:00:00",
            label="Rechazo por falta de turno en fecha",
            payload=make_service(f"{prefix}-09", "SIM0009", "2026-04-22 12:00:00", "4,700", "-74,051", "4,730", "-74,100", "BOGOTA", "CHIA"),
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-20 11:05:00",
            label="Rechazo por horizonte > 8 dias",
            payload=make_service(f"{prefix}-10", "SIM0010", "2026-05-05 12:00:00", "4,700", "-74,051", "4,730", "-74,100", "BOGOTA", "CHIA"),
        ),
        SimulationStep(
            kind="validate",
            now_value="2026-04-20 14:05:00",
            label="Congelacion 20 abril",
        ),
        SimulationStep(
            kind="cancel",
            now_value="2026-04-20 14:06:00",
            label="Cancelar servicio preasignado aun no congelado",
            autorizacion=f"{prefix}-07",
        ),
        SimulationStep(
            kind="create",
            now_value="2026-04-23 18:00:00",
            label="Aceptado turno 6 del 23 abril",
            payload=make_service(f"{prefix}-11", "SIM0011", "2026-04-23 23:30:00", "4,700", "-74,051", "4,680", "-74,030", "BOGOTA", "BOGOTA"),
        ),
        SimulationStep(
            kind="validate",
            now_value="2026-04-23 22:35:00",
            label="Congelacion 23 abril",
        ),
    ]


def run() -> None:
    """Ejecuta la simulacion completa."""

    build_image()
    prefix = f"SIM{int(time.time())}"
    report = []
    known_auths: List[str] = []
    stop_container()
    start_container()

    try:
        for step in scenario_steps(prefix):
            set_simulated_now(step.now_value)
            reset_lock()
            print(f"RUNNING kind={step.kind} label={step.label} now={step.now_value}", flush=True)
            timeout_seconds = 60 if step.kind == "validate" else 20
            api_start = time.monotonic()
            if step.kind == "create" and step.payload:
                step_auth = step.payload["autorizacion"]
                response = api_post("/rve/servicio", step.payload, timeout_seconds=timeout_seconds)
                step = SimulationStep(
                    kind=step.kind,
                    now_value=step.now_value,
                    payload=step.payload,
                    autorizacion=step_auth,
                    label=step.label,
                )
                if step_auth not in known_auths:
                    known_auths.append(step_auth)
            elif step.kind == "validate":
                response = api_post("/rve/validar", timeout_seconds=timeout_seconds)
            elif step.kind == "cancel" and step.autorizacion:
                response = api_post_query(
                    "/rve/cancelacion",
                    {"autorizacion": step.autorizacion},
                    timeout_seconds=timeout_seconds,
                )
            elif step.kind == "complete" and step.autorizacion:
                response = api_post_query(
                    "/rve/completar",
                    {"autorizacion": step.autorizacion},
                    timeout_seconds=timeout_seconds,
                )
            else:
                raise RuntimeError(f"Paso invalido: {step}")
            api_duration_ms = int((time.monotonic() - api_start) * 1000)
            report.append(verify_state(step, response, known_auths, api_duration_ms))
            print(
                f"DONE kind={step.kind} label={step.label} duration_ms={api_duration_ms}",
                flush=True,
            )
    except Exception:
        print(f"FAILED kind={step.kind} label={step.label} now={step.now_value}", flush=True)
        raise
    finally:
        stop_container()

    print(json.dumps(report, indent=2, ensure_ascii=True))


def docker_env_args(now_value: str | None = None) -> List[str]:
    """Construye los argumentos de entorno para docker run."""

    spreadsheet_id = _required_env("SPREADSHEET_ID")
    service_account_file = _required_env("SERVICE_ACCOUNT_FILE")
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
        "-v",
        f"{service_account_file}:/run/secrets/service-account.json:ro",
    ]
    endpoint_api_key = os.getenv("ENDPOINT_API_KEY", os.getenv("END_POINT_API_KEY", "")).strip()
    if endpoint_api_key:
        args.extend(["-e", f"ENDPOINT_API_KEY={endpoint_api_key}"])
    datadog_enabled = os.getenv("DATADOG_ENABLED", "false")
    args.extend(["-e", f"DATADOG_ENABLED={datadog_enabled}"])
    args.extend(["-e", f"ALLOW_TEST_CLOCK={os.getenv('ALLOW_TEST_CLOCK', 'true')}"])
    if now_value:
        args.extend(["-e", f"RVE_FIXED_NOW={now_value}"])
    return args


def snapshot_auth(autorizacion: str) -> dict:
    """Lee el estado de una autorizacion usando el mismo backend en ejecucion."""

    return api_get(f"/rve/_debug/snapshot/{autorizacion}", timeout_seconds=15)


def verify_state(
    step: SimulationStep,
    response: dict,
    known_auths: List[str],
    api_duration_ms: int,
) -> dict:
    """Lee el estado actual desde un contenedor one-shot y lo resume."""

    summary = {
        "label": step.label,
        "kind": step.kind,
        "api_duration_ms": api_duration_ms,
        "response": response,
    }
    if step.kind == "validate":
        summary["snapshots"] = [
            snapshot_auth(autorizacion)
            for autorizacion in known_auths
        ]
        return summary
    if step.kind not in {"create", "cancel", "complete"} or not step.autorizacion:
        return summary

    summary.update(snapshot_auth(step.autorizacion))
    return summary


def docker_process_env() -> dict[str, str]:
    """Construye un entorno Docker compatible con WSL sin credential helper externo."""

    env = os.environ.copy()
    docker_config = env.get("DOCKER_CONFIG", "/tmp/rve-docker-config")
    os.makedirs(docker_config, exist_ok=True)
    config_path = os.path.join(docker_config, "config.json")
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="ascii") as handle:
            handle.write("{}")
    env["DOCKER_CONFIG"] = docker_config
    return env


if __name__ == "__main__":
    run()
