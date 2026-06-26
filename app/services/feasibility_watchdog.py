"""Monitor periodico de factibilidad por departamento."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from services.dispatch_service import DispatchService
from utils.time_utils import format_datetime, now_bogota

logger = logging.getLogger("feasibility_watchdog")

DEFAULT_DEPARTMENTS = ("CUNDINAMARCA", "ANTIOQUIA")
DEFAULT_INTERVAL_SECONDS = 300


@dataclass
class DepartmentFeasibility:
    departamento: str
    feasible: bool
    dynamic_count: int
    locked_count: int
    turn_count: int
    orphan_count: int
    orphan_turn_ids: List[str]
    ignored_without_preassignment: List[str]
    removal_candidates: List[str]
    manual_blocks_count: int


def _send_google_chat(webhook_url: str, text: str) -> None:
    payload = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        logger.info("alert.sent status=%s", response.status)


def _evaluate_department(
    service: DispatchService,
    snapshot,
    departamento: str,
) -> DepartmentFeasibility:
    normalized_department = departamento.strip().upper()
    state = service._build_department_state(snapshot, normalized_department)
    feasible = service._is_department_state_feasible(state)
    removal_candidates: List[str] = []

    candidates = service._ordered_recovery_candidates(state)
    if not feasible and candidates:
        for servicio in candidates:
            excluded_auths = {servicio.autorizacion}
            remaining = [
                item for item in state["dynamic_services"] if item.autorizacion not in excluded_auths
            ]
            candidate_solution = service._solve_department_state(
                remaining,
                state,
                excluded_auths=excluded_auths,
                manual_candidate_services=[servicio],
            )
            if candidate_solution is not None:
                removal_candidates.append(servicio.autorizacion)

    return DepartmentFeasibility(
        departamento=normalized_department,
        feasible=feasible,
        dynamic_count=len(state["dynamic_services"]),
        locked_count=sum(len(items) for items in state["locked_by_turn"].values()),
        turn_count=len(state["turnos"]),
        orphan_count=len(state.get("orphan_services", [])),
        orphan_turn_ids=list(state.get("orphan_turn_ids", [])),
        ignored_without_preassignment=list(state.get("accepted_without_preassignment", [])),
        removal_candidates=removal_candidates,
        manual_blocks_count=sum(len(items) for items in state["manual_blocks_by_turn"].values()),
    )


def _format_alert(results: List[DepartmentFeasibility]) -> str:
    failing = [result for result in results if not result.feasible]
    lines = [
        "🚨🚨🚨 *ALERTA RVE: factibilidad rota* 🚨🚨🚨",
        f"Timestamp: {format_datetime(now_bogota())}",
        "",
    ]
    for result in failing:
        lines.extend(
            [
                f"*{result.departamento}*",
                f"- Factible: NO",
                f"- Servicios dinamicos: {result.dynamic_count}",
                f"- Servicios bloqueados: {result.locked_count}",
                f"- Turnos activos: {result.turn_count}",
                f"- Servicios huerfanos por turno eliminado: {result.orphan_count}",
                f"- Reservas manuales blandas: {result.manual_blocks_count}",
            ]
        )
        if result.orphan_turn_ids:
            lines.append("- Turnos huerfanos: " + ", ".join(result.orphan_turn_ids[:12]))
        if result.removal_candidates:
            lines.append(
                "- Si se excluye uno de estos, el modelo vuelve a ser factible: "
                + ", ".join(result.removal_candidates[:12])
            )
        if result.ignored_without_preassignment:
            lines.append(
                "- Aceptados sin preasignacion vigente: "
                + ", ".join(result.ignored_without_preassignment[:12])
            )
        lines.append("")
    return "\n".join(lines).strip()


def run_check(departments: Iterable[str], webhook_url: str | None) -> bool:
    service = DispatchService()
    snapshot = service._load_snapshot()

    results = [
        _evaluate_department(
            service,
            snapshot,
            departamento,
        )
        for departamento in departments
    ]

    for result in results:
        status = "OK" if result.feasible else "BROKEN"
        logger.info(
            "department.status departamento=%s status=%s dynamic=%s locked=%s turns=%s orphan=%s manual_blocks=%s removal_candidates=%s",
            result.departamento,
            status,
            result.dynamic_count,
            result.locked_count,
            result.turn_count,
            result.orphan_count,
            result.manual_blocks_count,
            ",".join(result.removal_candidates),
        )

    all_feasible = all(result.feasible for result in results)
    if not all_feasible:
        alert = _format_alert(results)
        logger.error("factibility.broken\n%s", alert)
        if webhook_url:
            try:
                _send_google_chat(webhook_url, alert)
            except (urllib.error.URLError, TimeoutError, OSError):
                logger.exception("alert.failed")
        else:
            logger.warning("alert.skipped missing_webhook_url=true")

    return all_feasible


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor de factibilidad RVE")
    parser.add_argument("--once", action="store_true", help="Ejecuta una sola revision")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=int(os.getenv("FEASIBILITY_WATCHDOG_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)),
    )
    parser.add_argument(
        "--departments",
        default=os.getenv("FEASIBILITY_WATCHDOG_DEPARTMENTS", ",".join(DEFAULT_DEPARTMENTS)),
    )
    parser.add_argument(
        "--webhook-url",
        default=os.getenv("FEASIBILITY_WATCHDOG_WEBHOOK_URL", ""),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    departments = [
        department.strip().upper()
        for department in args.departments.split(",")
        if department.strip()
    ]
    logger.info(
        "watchdog.start departments=%s interval_seconds=%s once=%s",
        ",".join(departments),
        args.interval_seconds,
        args.once,
    )

    while True:
        try:
            run_check(departments, args.webhook_url or None)
        except Exception:
            logger.exception("watchdog.check.failed")
        if args.once:
            return 0
        time.sleep(max(1, args.interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
