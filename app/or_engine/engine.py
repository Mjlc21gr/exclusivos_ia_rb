"""Motor OR de asignacion dinamica."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, List, Optional

from or_engine.models import Coordenadas, DecisionOutcome, PreasignacionPlan, ServicioPlan, TurnoPlan
from utils.config import get_settings
from utils.constants import (
    ESTADO_ASIGNADO_FINAL,
    ESTADOS_ACEPTADOS,
    ESTADOS_MANUALES,
    ESTADOS_TERMINALES,
    PREASIGNACION_CONGELADA,
)
from utils.geo import haversine_km, travel_minutes
from utils.time_utils import now_bogota

logger = logging.getLogger(__name__)


@dataclass
class SearchSolution:
    """Solucion encontrada por backtracking."""

    assignments: Dict[str, TurnoPlan]
    total_distance_km: float


@dataclass
class RouteEvaluation:
    """Resultado detallado de validar una ruta sobre un turno."""

    feasible: bool
    total_distance_km: float
    failure_reason: str = ""


class DecisionEngine:
    """Evalua si un servicio debe aceptarse o rechazarse."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def decidir(
        self,
        nuevo_servicio: ServicioPlan,
        servicios: List[ServicioPlan],
        turnos: List[TurnoPlan],
        preasignaciones: List[PreasignacionPlan],
    ) -> DecisionOutcome:
        """Ejecuta la decision final ACEPTAR o RECHAZAR."""

        started = time.monotonic()
        now_value = now_bogota()
        logger.warning(
            "engine.decidir.start autorizacion=%s servicios=%s turnos=%s preasignaciones=%s",
            nuevo_servicio.autorizacion,
            len(servicios),
            len(turnos),
            len(preasignaciones),
        )
        if not nuevo_servicio.fecha_servicio:
            return DecisionOutcome(nuevo_servicio.autorizacion, "RECHAZAR", "FECHA_SERVICIO invalida")

        min_allowed = now_value + timedelta(minutes=self.settings.min_notice_minutes)
        max_allowed = now_value + timedelta(days=self.settings.horizon_days)
        if nuevo_servicio.fecha_servicio < min_allowed:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                f"FECHA_SERVICIO debe ser al menos {self.settings.min_notice_minutes} minutos despues de ahora",
            )
        if nuevo_servicio.fecha_servicio > max_allowed:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                f"FECHA_SERVICIO excede el horizonte de {self.settings.horizon_days} dias",
            )

        turnos_compatibles = self._candidate_turns(nuevo_servicio, turnos, now_value)
        logger.warning(
            "engine.decidir.candidates autorizacion=%s compatibles=%s",
            nuevo_servicio.autorizacion,
            len(turnos_compatibles),
        )
        if not turnos_compatibles:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                "No existe turno compatible para departamento, servicio y horario",
            )

        assignments_by_auth = self._select_current_assignments(preasignaciones)
        servicios_by_auth = {servicio.autorizacion: servicio for servicio in servicios}
        for autorizacion, preasignacion in assignments_by_auth.items():
            servicio = servicios_by_auth.get(autorizacion)
            if servicio:
                servicio.id_turno_preasignado = preasignacion.id_turno

        locked_by_turn: Dict[str, List[ServicioPlan]] = {turno.id_turno: [] for turno in turnos}
        dynamic_services: List[ServicioPlan] = []
        current_turn_by_auth: Dict[str, str] = {}

        for servicio in servicios:
            if servicio.autorizacion == nuevo_servicio.autorizacion:
                continue
            if servicio.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio.estado_operacion in ESTADOS_MANUALES:
                continue
            if self._is_historical_service(servicio, now_value):
                continue

            preasignacion = assignments_by_auth.get(servicio.autorizacion)
            if servicio.estado_operacion in ESTADOS_ACEPTADOS and not preasignacion:
                logger.warning(
                    "engine.legacy.inconsistent_accepted_without_preassignment ignored_autorizacion=%s new_autorizacion=%s estado=%s",
                    servicio.autorizacion,
                    nuevo_servicio.autorizacion,
                    servicio.estado_operacion,
                )
                continue
            if not preasignacion:
                continue

            current_turn_by_auth[servicio.autorizacion] = preasignacion.id_turno
            if self._is_locked(servicio, preasignacion, now_value):
                locked_by_turn.setdefault(preasignacion.id_turno, []).append(servicio)
            else:
                dynamic_services.append(servicio)

        for turno in turnos:
            evaluation = self._route_metrics(turno, locked_by_turn.get(turno.id_turno, []))
            if not evaluation.feasible:
                return DecisionOutcome(
                    nuevo_servicio.autorizacion,
                    "RECHAZAR",
                    f"Estado inconsistente en turno {turno.id_turno}: {self._humanize_failure_reason(evaluation.failure_reason)}",
                )

        dynamic_services.append(nuevo_servicio)
        solution = self._solve_dynamic_assignment(
            dynamic_services=dynamic_services,
            locked_by_turn=locked_by_turn,
            turnos=turnos,
            current_turn_by_auth=current_turn_by_auth,
            now_value=now_value,
        )
        if solution is None or nuevo_servicio.autorizacion not in solution.assignments:
            logger.warning(
                "engine.decidir.end autorizacion=%s decision=RECHAZAR duration_ms=%s",
                nuevo_servicio.autorizacion,
                int((time.monotonic() - started) * 1000),
            )
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                self._diagnose_rejection(
                    nuevo_servicio,
                    turnos_compatibles,
                    locked_by_turn,
                ),
            )

        assigned_turn = solution.assignments[nuevo_servicio.autorizacion]
        logger.warning(
            "engine.decidir.end autorizacion=%s decision=ACEPTAR duration_ms=%s assigned_turn=%s total_distance_km=%.2f",
            nuevo_servicio.autorizacion,
            int((time.monotonic() - started) * 1000),
            assigned_turn.id_turno,
            solution.total_distance_km,
        )
        return DecisionOutcome(
            autorizacion=nuevo_servicio.autorizacion,
            decision="ACEPTAR",
            razon=f"Aceptado en turno {assigned_turn.id_turno} ({assigned_turn.nombre_conductor})",
            assignments=solution.assignments,
        )

    def _solve_dynamic_assignment(
        self,
        dynamic_services: List[ServicioPlan],
        locked_by_turn: Dict[str, List[ServicioPlan]],
        turnos: List[TurnoPlan],
        current_turn_by_auth: Dict[str, str],
        now_value,
    ) -> Optional[SearchSolution]:
        """Busca una asignacion factible para todos los servicios dinamicos."""

        started = time.monotonic()
        turnos_by_id = {turno.id_turno: turno for turno in turnos}
        candidate_map: Dict[str, List[TurnoPlan]] = {}
        backtrack_calls = 0

        for servicio in dynamic_services:
            candidates = self._candidate_turns(servicio, turnos, now_value)
            current_turn = current_turn_by_auth.get(servicio.autorizacion)
            candidates.sort(
                key=lambda turno: (
                    0 if turno.id_turno == current_turn else 1,
                    haversine_km(
                        turno.punto_inicio.lat,
                        turno.punto_inicio.lng,
                        servicio.origen.lat,
                        servicio.origen.lng,
                    ),
                )
            )
            if not candidates:
                logger.warning(
                    "engine.solve.no_candidates autorizacion=%s dynamic_services=%s duration_ms=%s",
                    servicio.autorizacion,
                    len(dynamic_services),
                    int((time.monotonic() - started) * 1000),
                )
                return None
            candidate_map[servicio.autorizacion] = candidates

        ordered_services = sorted(
            dynamic_services,
            key=lambda servicio: (
                len(candidate_map[servicio.autorizacion]),
                servicio.fecha_servicio,
                servicio.autorizacion,
            ),
        )

        assigned_by_turn: Dict[str, List[ServicioPlan]] = {turno.id_turno: [] for turno in turnos}
        assignment: Dict[str, TurnoPlan] = {}
        best: Optional[SearchSolution] = None

        def backtrack(position: int) -> None:
            nonlocal best
            nonlocal backtrack_calls
            backtrack_calls += 1
            if position == len(ordered_services):
                total_distance = 0.0
                for turno in turnos:
                    evaluation = self._route_metrics(
                        turno,
                        locked_by_turn.get(turno.id_turno, []) + assigned_by_turn.get(turno.id_turno, []),
                    )
                    if not evaluation.feasible:
                        return
                    total_distance += evaluation.total_distance_km
                if best is None or total_distance < best.total_distance_km:
                    best = SearchSolution(assignments=dict(assignment), total_distance_km=total_distance)
                return

            servicio = ordered_services[position]
            for turno in candidate_map[servicio.autorizacion]:
                assigned_by_turn.setdefault(turno.id_turno, []).append(servicio)
                evaluation = self._route_metrics(
                    turno,
                    locked_by_turn.get(turno.id_turno, []) + assigned_by_turn[turno.id_turno],
                )
                if evaluation.feasible:
                    assignment[servicio.autorizacion] = turno
                    backtrack(position + 1)
                    assignment.pop(servicio.autorizacion, None)
                assigned_by_turn[turno.id_turno].pop()

        backtrack(0)
        logger.warning(
            "engine.solve.end dynamic_services=%s ordered_services=%s backtrack_calls=%s best_found=%s duration_ms=%s",
            len(dynamic_services),
            len(ordered_services),
            backtrack_calls,
            best is not None,
            int((time.monotonic() - started) * 1000),
        )
        return best

    def _candidate_turns(
        self,
        servicio: ServicioPlan,
        turnos: List[TurnoPlan],
        now_value,
    ) -> List[TurnoPlan]:
        """Filtra turnos por C3, C5 y C6."""

        candidates: List[TurnoPlan] = []
        for turno in turnos:
            if not turno.fecha_inicio_turno or not turno.fecha_fin_turno:
                continue
            if turno.fecha_fin_turno < now_value:
                continue
            if turno.departamento.strip().upper() != servicio.departamento.strip().upper():
                continue
            if turno.servicio.strip().upper() != servicio.servicio.strip().upper():
                continue
            if not servicio.fecha_servicio:
                continue
            if not (turno.fecha_inicio_turno <= servicio.fecha_servicio <= turno.fecha_fin_turno):
                continue
            candidates.append(turno)
        return candidates

    def _is_locked(
        self,
        servicio: ServicioPlan,
        preasignacion: PreasignacionPlan,
        now_value,
    ) -> bool:
        """Determina si un servicio ya no puede ser reasignado."""

        if servicio.estado_operacion == ESTADO_ASIGNADO_FINAL:
            return True
        if preasignacion.estado_preasignacion == PREASIGNACION_CONGELADA:
            return True
        if not servicio.fecha_servicio:
            return False
        return servicio.fecha_servicio <= now_value + timedelta(minutes=self.settings.min_notice_minutes)

    def _is_historical_service(self, servicio: ServicioPlan, now_value) -> bool:
        """Ignora servicios ya ocurridos al reoptimizar nuevas solicitudes."""

        return bool(servicio.fecha_servicio and servicio.fecha_servicio < now_value)

    def _select_current_assignments(
        self,
        preasignaciones: List[PreasignacionPlan],
    ) -> Dict[str, PreasignacionPlan]:
        """Escoge la preasignacion vigente por autorizacion."""

        selected: Dict[str, PreasignacionPlan] = {}
        for preasignacion in preasignaciones:
            if preasignacion.estado_preasignacion not in {"ACTIVA", "CONGELADA"}:
                continue
            previous = selected.get(preasignacion.autorizacion)
            if previous is None:
                selected[preasignacion.autorizacion] = preasignacion
                continue
            if preasignacion.estado_preasignacion == PREASIGNACION_CONGELADA:
                selected[preasignacion.autorizacion] = preasignacion
                continue
            if (
                previous.estado_preasignacion != PREASIGNACION_CONGELADA
                and preasignacion.row_index
                and previous.row_index
                and preasignacion.row_index > previous.row_index
            ):
                selected[preasignacion.autorizacion] = preasignacion
        return selected

    def _route_metrics(self, turno: TurnoPlan, services: List[ServicioPlan]) -> RouteEvaluation:
        """Valida la ruta completa y retorna distancia total."""

        if not services:
            return RouteEvaluation(True, 0.0)

        ordered = sorted(services, key=lambda servicio: (servicio.fecha_servicio, servicio.autorizacion))
        current_time = turno.fecha_inicio_turno - timedelta(minutes=self.settings.pre_shift_travel_minutes)
        current_position = turno.punto_inicio
        total_distance = 0.0

        for servicio in ordered:
            if not current_time or not turno.fecha_fin_turno or not servicio.fecha_servicio:
                return RouteEvaluation(False, total_distance, "DATOS_INVALIDOS")

            if not self._within_radius(turno.punto_inicio, servicio.origen):
                return RouteEvaluation(False, total_distance, "RADIO_MAXIMO_EXCEDIDO")
            if not self._within_radius(turno.punto_inicio, servicio.destino):
                return RouteEvaluation(False, total_distance, "RADIO_MAXIMO_EXCEDIDO")

            distance_to_origin = haversine_km(
                current_position.lat,
                current_position.lng,
                servicio.origen.lat,
                servicio.origen.lng,
            )
            arrival_time = current_time + timedelta(minutes=travel_minutes(distance_to_origin))
            latest_arrival_time = servicio.fecha_servicio + timedelta(minutes=self.settings.buffer_minutes)
            if arrival_time > latest_arrival_time:
                return RouteEvaluation(False, total_distance, "SOLAPAMIENTO_O_LLEGADA_TARDE")
            service_start_time = max(arrival_time, servicio.fecha_servicio)

            service_distance = haversine_km(
                servicio.origen.lat,
                servicio.origen.lng,
                servicio.destino.lat,
                servicio.destino.lng,
            )
            end_time = service_start_time + timedelta(minutes=self.settings.onsite_minutes + travel_minutes(service_distance))
            if end_time > turno.fecha_fin_turno:
                return RouteEvaluation(False, total_distance, "FUERA_DE_TURNO")

            total_distance += distance_to_origin + service_distance
            current_time = end_time
            current_position = servicio.destino

        return RouteEvaluation(True, total_distance)

    def _within_radius(self, origin: Coordenadas, point: Coordenadas) -> bool:
        """Verifica la restriccion C4 respecto al punto inicial del turno."""

        return haversine_km(origin.lat, origin.lng, point.lat, point.lng) <= self.settings.max_radius_km

    def _diagnose_rejection(
        self,
        nuevo_servicio: ServicioPlan,
        turnos_compatibles: List[TurnoPlan],
        locked_by_turn: Dict[str, List[ServicioPlan]],
    ) -> str:
        """Explica por que el nuevo servicio no pudo insertarse."""

        failures: List[str] = []
        feasible_with_locked = False
        for turno in turnos_compatibles:
            evaluation = self._route_metrics(
                turno,
                locked_by_turn.get(turno.id_turno, []) + [nuevo_servicio],
            )
            if evaluation.feasible:
                feasible_with_locked = True
                continue
            failures.append(evaluation.failure_reason)

        if not feasible_with_locked and failures:
            return self._humanize_failure_reason(failures[0])
        return "No existe insercion factible por solapamiento o preservacion de compromisos vigentes"

    def _humanize_failure_reason(self, failure_reason: str) -> str:
        """Convierte causas tecnicas internas en razones legibles."""

        if failure_reason == "RADIO_MAXIMO_EXCEDIDO":
            return f"Servicio fuera del radio maximo de {self.settings.max_radius_km:g} km"
        if failure_reason == "FUERA_DE_TURNO":
            return "Servicio fuera de los limites del turno"
        if failure_reason == "SOLAPAMIENTO_O_LLEGADA_TARDE":
            return "Servicio genera solapamiento o llegada tardia frente a la hora programada"
        if failure_reason == "DATOS_INVALIDOS":
            return "Datos invalidos del turno o del servicio"
        return "No existe insercion factible sin romper compromisos vigentes"
