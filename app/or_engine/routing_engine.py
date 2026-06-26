"""Motor de decision basado en OR-Tools RoutingModel."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from or_engine.engine import DecisionEngine, RouteEvaluation
from or_engine.models import DecisionOutcome, PreasignacionPlan, ServicioPlan, TurnoPlan
from services.google_matrix_estimator import record_solve_estimate
from utils.constants import (
    ANALISIS_FUERA_RADIO_MAXIMO,
    ANALISIS_INSERCION_NO_FACTIBLE,
    ANALISIS_NO_LLEGA_TIEMPO,
    ANALISIS_SATURACION_TURNO,
    ANALISIS_SERVICIO_FUERA_LIMITES_TURNO,
    ESTADO_ASIGNADO_FINAL,
    ESTADO_MANUAL,
    ESTADO_URGENTE_GESTIONAR_MANUAL,
    ESTADOS_ACEPTADOS,
    ESTADOS_TERMINALES,
    PREASIGNACION_CONGELADA,
)
from utils.geo import haversine_km, travel_minutes
from utils.time_utils import now_bogota

logger = logging.getLogger(__name__)
MANUAL_FALLBACK_TRAVEL_MINUTES = 20
MANUAL_BLOCK_PENALTY_UNITS = 10_000_000


@dataclass
class RoutingSolution:
    """Solucion encontrada por OR-Tools."""

    assignments: Dict[str, TurnoPlan]
    total_distance_km: float
    objective_value: int


@dataclass(frozen=True)
class ManualBlock:
    """Bloqueo manual efectivo que no puede romper la factibilidad actual."""

    autorizacion: str
    id_turno: str
    start: datetime
    end: datetime
    manual: ServicioPlan


class RoutingDecisionEngine(DecisionEngine):
    """Evalua servicios usando OR-Tools sin cambiar el contrato publico."""

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
            "engine.ortools.decidir.start autorizacion=%s servicios=%s turnos=%s preasignaciones=%s",
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
            "engine.ortools.decidir.candidates autorizacion=%s compatibles=%s",
            nuevo_servicio.autorizacion,
            len(turnos_compatibles),
        )
        if not turnos_compatibles:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                "No existe turno compatible para departamento, servicio y horario",
            )

        target_department = nuevo_servicio.departamento.strip().upper()
        turnos = [
            turno
            for turno in turnos
            if turno.departamento.strip().upper() == target_department
        ]
        servicios = [
            servicio
            for servicio in servicios
            if servicio.autorizacion == nuevo_servicio.autorizacion
            or servicio.departamento.strip().upper() == target_department
        ]
        turnos_ids = {turno.id_turno for turno in turnos}
        servicios_auth = {servicio.autorizacion for servicio in servicios}
        preasignaciones = [
            preasignacion
            for preasignacion in preasignaciones
            if preasignacion.autorizacion in servicios_auth
            or preasignacion.id_turno in turnos_ids
        ]
        logger.warning(
            "engine.ortools.decidir.scope autorizacion=%s departamento=%s servicios=%s turnos=%s preasignaciones=%s",
            nuevo_servicio.autorizacion,
            target_department,
            len(servicios),
            len(turnos),
            len(preasignaciones),
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
        manual_services: List[ServicioPlan] = []
        normal_services_by_turn: Dict[str, List[ServicioPlan]] = {turno.id_turno: [] for turno in turnos}

        for servicio in servicios:
            if servicio.autorizacion == nuevo_servicio.autorizacion:
                continue
            if servicio.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio.estado_operacion == ESTADO_URGENTE_GESTIONAR_MANUAL:
                continue
            if servicio.estado_operacion == ESTADO_MANUAL:
                manual_services.append(servicio)
                continue
            if self._is_historical_service(servicio, now_value):
                continue

            preasignacion = assignments_by_auth.get(servicio.autorizacion)
            if servicio.estado_operacion in ESTADOS_ACEPTADOS and not preasignacion:
                logger.warning(
                    "engine.ortools.inconsistent_accepted_without_preassignment ignored_autorizacion=%s new_autorizacion=%s estado=%s",
                    servicio.autorizacion,
                    nuevo_servicio.autorizacion,
                    servicio.estado_operacion,
                )
                continue
            if not preasignacion:
                continue

            current_turn_by_auth[servicio.autorizacion] = preasignacion.id_turno
            normal_services_by_turn.setdefault(preasignacion.id_turno, []).append(servicio)
            if self._is_locked(servicio, preasignacion, now_value):
                locked_by_turn.setdefault(preasignacion.id_turno, []).append(servicio)
            else:
                dynamic_services.append(servicio)

        manual_blocks_by_turn = self._build_manual_blocks_by_turn(
            manual_services=manual_services,
            turnos=turnos,
            normal_services_by_turn=normal_services_by_turn,
        )

        for turno in turnos:
            evaluation = self._route_metrics(turno, locked_by_turn.get(turno.id_turno, []))
            if not evaluation.feasible:
                return DecisionOutcome(
                    nuevo_servicio.autorizacion,
                    "RECHAZAR",
                    f"Estado inconsistente en turno {turno.id_turno}: {self._humanize_failure_reason(evaluation.failure_reason)}",
                    analysis_code=self._analysis_code_for_failure(evaluation.failure_reason),
                )

        dynamic_services.append(nuevo_servicio)
        solution = self._solve_dynamic_assignment(
            dynamic_services=dynamic_services,
            locked_by_turn=locked_by_turn,
            turnos=turnos,
            current_turn_by_auth=current_turn_by_auth,
            now_value=now_value,
            manual_blocks_by_turn=manual_blocks_by_turn,
        )
        if solution is None or nuevo_servicio.autorizacion not in solution.assignments:
            razon, analysis_code = self._diagnose_rejection_detail(
                nuevo_servicio,
                turnos_compatibles,
                locked_by_turn,
                manual_blocks_by_turn,
                normal_services_by_turn,
            )
            logger.warning(
                "engine.ortools.decidir.end autorizacion=%s decision=RECHAZAR duration_ms=%s",
                nuevo_servicio.autorizacion,
                int((time.monotonic() - started) * 1000),
            )
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                razon,
                analysis_code=analysis_code,
            )

        assigned_turn = solution.assignments[nuevo_servicio.autorizacion]
        logger.warning(
            "engine.ortools.decidir.end autorizacion=%s decision=ACEPTAR duration_ms=%s assigned_turn=%s total_distance_km=%.2f objective=%s",
            nuevo_servicio.autorizacion,
            int((time.monotonic() - started) * 1000),
            assigned_turn.id_turno,
            solution.total_distance_km,
            solution.objective_value,
        )
        return DecisionOutcome(
            autorizacion=nuevo_servicio.autorizacion,
            decision="ACEPTAR",
            razon=f"Aceptado en turno {assigned_turn.id_turno} ({assigned_turn.nombre_conductor})",
            assignments=solution.assignments,
        )

    def _diagnose_rejection_detail(
        self,
        nuevo_servicio: ServicioPlan,
        turnos_compatibles: List[TurnoPlan],
        locked_by_turn: Dict[str, List[ServicioPlan]],
        manual_blocks_by_turn: Dict[str, List[ManualBlock]],
        normal_services_by_turn: Dict[str, List[ServicioPlan]],
    ) -> tuple[str, str]:
        """Clasifica rechazos sin ejecutar busquedas adicionales de OR-Tools."""

        radius_candidates = [
            turno
            for turno in turnos_compatibles
            if self._within_radius(turno.punto_inicio, nuevo_servicio.origen)
            and self._within_radius(turno.punto_inicio, nuevo_servicio.destino)
        ]
        if not radius_candidates:
            return (
                self._humanize_failure_reason("RADIO_MAXIMO_EXCEDIDO"),
                ANALISIS_FUERA_RADIO_MAXIMO,
            )

        codes: List[str] = []
        for turno in radius_candidates:
            code = self._probe_turn_insertion_analysis(
                nuevo_servicio,
                turno,
                normal_services_by_turn.get(turno.id_turno, []),
                manual_blocks_by_turn,
            )
            if code:
                codes.append(code)

        if codes:
            if ANALISIS_INSERCION_NO_FACTIBLE in codes:
                return (
                    "No existe insercion factible sin romper el siguiente compromiso vigente",
                    ANALISIS_INSERCION_NO_FACTIBLE,
                )
            if ANALISIS_NO_LLEGA_TIEMPO in codes:
                return (
                    "Servicio no alcanza a llegar a tiempo desde la ruta vigente",
                    ANALISIS_NO_LLEGA_TIEMPO,
                )
            if ANALISIS_SATURACION_TURNO in codes:
                return (
                    "Horario saturado por servicios o bloqueos vigentes",
                    ANALISIS_SATURACION_TURNO,
                )
            if ANALISIS_SERVICIO_FUERA_LIMITES_TURNO in codes:
                return (
                    self._humanize_failure_reason("FUERA_DE_TURNO"),
                    ANALISIS_SERVICIO_FUERA_LIMITES_TURNO,
                )

        return (
            "No existe insercion factible por solapamiento o preservacion de compromisos vigentes",
            ANALISIS_INSERCION_NO_FACTIBLE,
        )

    def _probe_turn_insertion_analysis(
        self,
        nuevo_servicio: ServicioPlan,
        turno: TurnoPlan,
        current_services: List[ServicioPlan],
        manual_blocks_by_turn: Dict[str, List[ManualBlock]],
    ) -> str:
        """Evalua una insercion local barata para explicar el rechazo."""

        if self._overlaps_existing_service_window(nuevo_servicio, turno, current_services):
            return ANALISIS_SATURACION_TURNO

        ordered = sorted(
            [*current_services, nuevo_servicio],
            key=lambda servicio: (servicio.fecha_servicio, servicio.autorizacion),
        )
        current_time = turno.fecha_inicio_turno - timedelta(minutes=self.settings.pre_shift_travel_minutes)
        current_position = turno.punto_inicio
        previous_was_new = False

        for servicio in ordered:
            if not current_time or not turno.fecha_fin_turno or not servicio.fecha_servicio:
                continue

            distance_to_origin = haversine_km(
                current_position.lat,
                current_position.lng,
                servicio.origen.lat,
                servicio.origen.lng,
            )
            arrival_time = current_time + timedelta(minutes=travel_minutes(distance_to_origin))
            latest_arrival_time = servicio.fecha_servicio + timedelta(minutes=self.settings.buffer_minutes)
            is_new = servicio.autorizacion == nuevo_servicio.autorizacion
            if arrival_time > latest_arrival_time:
                return ANALISIS_NO_LLEGA_TIEMPO if is_new else (
                    ANALISIS_INSERCION_NO_FACTIBLE if previous_was_new else ""
                )

            service_start_time = max(arrival_time, servicio.fecha_servicio)
            service_end_time = service_start_time + timedelta(minutes=self._service_minutes(servicio))
            if service_end_time > turno.fecha_fin_turno:
                return ANALISIS_SERVICIO_FUERA_LIMITES_TURNO

            current_time = service_end_time
            current_position = servicio.destino
            previous_was_new = is_new

        return ""

    def _overlaps_existing_service_window(
        self,
        nuevo_servicio: ServicioPlan,
        turno: TurnoPlan,
        current_services: List[ServicioPlan],
    ) -> bool:
        if not nuevo_servicio.fecha_servicio:
            return False
        new_start = nuevo_servicio.fecha_servicio
        new_end = new_start + timedelta(minutes=self._service_minutes(nuevo_servicio))
        for servicio, start, end in self._service_windows_for_turn(turno, current_services):
            if servicio.autorizacion == nuevo_servicio.autorizacion:
                continue
            if new_start < end and new_end > start:
                return True
        return False

    def _service_minutes(self, servicio: ServicioPlan) -> float:
        distance = haversine_km(
            servicio.origen.lat,
            servicio.origen.lng,
            servicio.destino.lat,
            servicio.destino.lng,
        )
        return self.settings.onsite_minutes + travel_minutes(distance)

    def _analysis_code_for_failure(self, failure_reason: str) -> str:
        if failure_reason == "RADIO_MAXIMO_EXCEDIDO":
            return ANALISIS_FUERA_RADIO_MAXIMO
        if failure_reason == "FUERA_DE_TURNO":
            return ANALISIS_SERVICIO_FUERA_LIMITES_TURNO
        if failure_reason == "SOLAPAMIENTO_O_LLEGADA_TARDE":
            return ANALISIS_NO_LLEGA_TIEMPO
        return ANALISIS_INSERCION_NO_FACTIBLE

    def _solve_dynamic_assignment(
        self,
        dynamic_services: List[ServicioPlan],
        locked_by_turn: Dict[str, List[ServicioPlan]],
        turnos: List[TurnoPlan],
        current_turn_by_auth: Dict[str, str],
        now_value,
        manual_blocks_by_turn: Optional[Dict[str, List[ManualBlock]]] = None,
    ) -> Optional[RoutingSolution]:
        """Resuelve la asignacion con VRPTW en OR-Tools."""

        from ortools.constraint_solver import pywrapcp

        started = time.monotonic()
        manual_blocks_by_turn = manual_blocks_by_turn or {}

        services = self._unique_services(
            [service for values in locked_by_turn.values() for service in values] + dynamic_services
        )
        if not services:
            return RoutingSolution({}, 0.0, 0)

        active_turnos = [turno for turno in turnos if self._valid_turn(turno, now_value)]
        turnos_by_id = {turno.id_turno: turno for turno in active_turnos}
        if not active_turnos:
            return None
        record_solve_estimate(
            services_count=len(services),
            turns_count=len(active_turnos),
            manual_blocks_count=sum(len(items) for items in manual_blocks_by_turn.values()),
            dynamic_services_count=len(dynamic_services),
            locked_services_count=len(services) - len(dynamic_services),
            department=active_turnos[0].departamento if active_turnos else "",
        )

        candidate_vehicle_ids: Dict[str, List[int]] = {}
        for servicio in services:
            locked_turn_id = self._locked_turn_id(servicio, locked_by_turn)
            if locked_turn_id:
                if locked_turn_id not in turnos_by_id:
                    return None
                candidates = [active_turnos.index(turnos_by_id[locked_turn_id])]
            else:
                candidates = [
                    vehicle_id
                    for vehicle_id, turno in enumerate(active_turnos)
                    if self._is_service_compatible_with_turn(
                        servicio,
                        turno,
                        now_value,
                        manual_blocks_by_turn,
                    )
                ]
            if not candidates:
                logger.warning(
                    "engine.ortools.solve.no_candidates autorizacion=%s dynamic_services=%s duration_ms=%s",
                    servicio.autorizacion,
                    len(dynamic_services),
                    int((time.monotonic() - started) * 1000),
                )
                return None
            candidate_vehicle_ids[servicio.autorizacion] = candidates

        def service_for_node(node: int) -> Optional[ServicioPlan]:
            if node == 0:
                return None
            return services[node - 1]

        def service_duration_minutes(servicio: ServicioPlan) -> int:
            distance = haversine_km(
                servicio.origen.lat,
                servicio.origen.lng,
                servicio.destino.lat,
                servicio.destino.lng,
            )
            return int(math.ceil(self.settings.onsite_minutes + travel_minutes(distance)))

        def transit_minutes_for_vehicle(vehicle_id: int, from_node: int, to_node: int) -> int:
            to_service = service_for_node(to_node)
            from_service = service_for_node(from_node)
            if to_service is None:
                return service_duration_minutes(from_service) if from_service is not None else 0

            if from_service is None:
                turno = active_turnos[vehicle_id]
                distance = haversine_km(
                    turno.punto_inicio.lat,
                    turno.punto_inicio.lng,
                    to_service.origen.lat,
                    to_service.origen.lng,
                )
                return int(math.ceil(travel_minutes(distance)))

            distance = haversine_km(
                from_service.destino.lat,
                from_service.destino.lng,
                to_service.origen.lat,
                to_service.origen.lng,
            )
            return service_duration_minutes(from_service) + int(math.ceil(travel_minutes(distance)))

        def distance_km_for_vehicle(vehicle_id: int, from_node: int, to_node: int) -> float:
            to_service = service_for_node(to_node)
            if to_service is None:
                return 0.0

            from_service = service_for_node(from_node)
            if from_service is None:
                turno = active_turnos[vehicle_id]
                return haversine_km(
                    turno.punto_inicio.lat,
                    turno.punto_inicio.lng,
                    to_service.origen.lat,
                    to_service.origen.lng,
                )

            return haversine_km(
                from_service.destino.lat,
                from_service.destino.lng,
                to_service.origen.lat,
                to_service.origen.lng,
            )

        def solve_once(strategy_name: str, is_fallback: bool) -> Optional[RoutingSolution]:
            reference_time = self._reference_time(active_turnos, services)
            manager = pywrapcp.RoutingIndexManager(len(services) + 1, len(active_turnos), 0)
            routing = pywrapcp.RoutingModel(manager)

            transit_callbacks: List[int] = []
            move_penalty_units = int(round(self.settings.ortools_move_penalty_km * 1000))
            for vehicle_id, turno in enumerate(active_turnos):
                transit_callback = routing.RegisterTransitCallback(
                    lambda from_index, to_index, vehicle_id=vehicle_id: transit_minutes_for_vehicle(
                        vehicle_id,
                        manager.IndexToNode(from_index),
                        manager.IndexToNode(to_index),
                    )
                )
                cost_callback = routing.RegisterTransitCallback(
                    lambda from_index, to_index, vehicle_id=vehicle_id: self._arc_cost_units(
                        vehicle_id,
                        manager.IndexToNode(from_index),
                        manager.IndexToNode(to_index),
                        active_turnos,
                        current_turn_by_auth,
                        service_for_node,
                        distance_km_for_vehicle,
                        manual_blocks_by_turn,
                        move_penalty_units,
                    )
                )
                transit_callbacks.append(transit_callback)
                routing.SetArcCostEvaluatorOfVehicle(cost_callback, vehicle_id)

            routing.AddDimensionWithVehicleTransits(
                transit_callbacks,
                self._max_wait_minutes(reference_time, active_turnos),
                self._horizon_minutes(reference_time, active_turnos, services),
                False,
                "Time",
            )
            time_dimension = routing.GetDimensionOrDie("Time")

            for vehicle_id, turno in enumerate(active_turnos):
                start_index = routing.Start(vehicle_id)
                end_index = routing.End(vehicle_id)
                start_minute = self._minutes_since(reference_time, turno.fecha_inicio_turno)
                pre_shift_start_minute = self._minutes_since(
                    reference_time,
                    turno.fecha_inicio_turno - timedelta(minutes=self.settings.pre_shift_travel_minutes),
                )
                end_minute = self._minutes_since(reference_time, turno.fecha_fin_turno)
                time_dimension.CumulVar(start_index).SetRange(pre_shift_start_minute, end_minute)
                time_dimension.CumulVar(end_index).SetRange(start_minute, end_minute)

            for node, servicio in enumerate(services, start=1):
                index = manager.NodeToIndex(node)
                service_minute = self._minutes_since(reference_time, servicio.fecha_servicio)
                latest_service_minute = service_minute + self.settings.buffer_minutes
                time_dimension.CumulVar(index).SetRange(service_minute, latest_service_minute)
                routing.SetAllowedVehiclesForIndex(candidate_vehicle_ids[servicio.autorizacion], index)

            search_parameters = pywrapcp.DefaultRoutingSearchParameters()
            search_parameters.first_solution_strategy = self._first_solution_strategy(strategy_name)
            local_search_seconds = max(0, self.settings.ortools_local_search_seconds)
            if local_search_seconds:
                search_parameters.local_search_metaheuristic = self._local_search_metaheuristic()
                search_parameters.time_limit.seconds = local_search_seconds
            else:
                search_parameters.time_limit.seconds = max(1, self.settings.ortools_time_limit_seconds)
            search_parameters.log_search = False

            attempt_started = time.monotonic()
            logger.warning(
                "engine.ortools.solve.start dynamic_services=%s locked_services=%s turns=%s time_limit_s=%s strategy=%s local_search=%s fallback=%s",
                len(dynamic_services),
                len(services) - len(dynamic_services),
                len(active_turnos),
                search_parameters.time_limit.seconds,
                strategy_name,
                self.settings.ortools_local_search_metaheuristic if local_search_seconds else "disabled",
                is_fallback,
            )
            solution = routing.SolveWithParameters(search_parameters)
            duration_ms = int((time.monotonic() - attempt_started) * 1000)
            if solution is None:
                logger.warning(
                    "engine.ortools.solve.end dynamic_services=%s services=%s turns=%s found=False duration_ms=%s strategy=%s fallback=%s",
                    len(dynamic_services),
                    len(services),
                    len(active_turnos),
                    duration_ms,
                    strategy_name,
                    is_fallback,
                )
                return None

            assignments: Dict[str, TurnoPlan] = {}
            total_distance = 0.0
            dynamic_auths = {item.autorizacion for item in dynamic_services}
            for vehicle_id, turno in enumerate(active_turnos):
                index = routing.Start(vehicle_id)
                previous_node = manager.IndexToNode(index)
                while not routing.IsEnd(index):
                    next_index = solution.Value(routing.NextVar(index))
                    next_node = manager.IndexToNode(next_index)
                    servicio = service_for_node(next_node)
                    if servicio is not None and servicio.autorizacion in dynamic_auths:
                        assignments[servicio.autorizacion] = turno
                    total_distance += distance_km_for_vehicle(vehicle_id, previous_node, next_node)
                    previous_node = next_node
                    index = next_index

            validation = self._validate_solution(
                assignments=assignments,
                dynamic_services=dynamic_services,
                locked_by_turn=locked_by_turn,
                turnos=active_turnos,
            )
            if not validation.feasible:
                logger.warning(
                    "engine.ortools.solve.validation_failed dynamic_services=%s services=%s turns=%s reason=%s duration_ms=%s strategy=%s fallback=%s",
                    len(dynamic_services),
                    len(services),
                    len(active_turnos),
                    validation.failure_reason,
                    duration_ms,
                    strategy_name,
                    is_fallback,
                )
                return None

            moved_assignments = {
                auth: turno.id_turno
                for auth, turno in assignments.items()
                if current_turn_by_auth.get(auth) and current_turn_by_auth[auth] != turno.id_turno
            }
            logger.warning(
                "engine.ortools.solve.end dynamic_services=%s services=%s turns=%s found=True assignments=%s duration_ms=%s objective=%s total_distance_km=%.2f strategy=%s fallback=%s moved=%s",
                len(dynamic_services),
                len(services),
                len(active_turnos),
                len(assignments),
                duration_ms,
                solution.ObjectiveValue(),
                total_distance,
                strategy_name,
                is_fallback,
                moved_assignments,
            )
            return RoutingSolution(
                assignments=assignments,
                total_distance_km=total_distance,
                objective_value=solution.ObjectiveValue(),
            )

        configured_strategy = self.settings.ortools_first_solution_strategy.strip().upper()
        strategies = [configured_strategy]
        fallback_strategy = "LOCAL_CHEAPEST_INSERTION"
        if fallback_strategy not in strategies:
            strategies.append(fallback_strategy)

        for index, strategy_name in enumerate(strategies):
            if index > 0:
                logger.warning(
                    "engine.ortools.solve.fallback dynamic_services=%s services=%s turns=%s from_strategy=%s to_strategy=%s elapsed_ms=%s",
                    len(dynamic_services),
                    len(services),
                    len(active_turnos),
                    strategies[index - 1],
                    strategy_name,
                    int((time.monotonic() - started) * 1000),
                )
            solution = solve_once(strategy_name, is_fallback=index > 0)
            if solution is not None:
                return solution

        logger.warning(
            "engine.ortools.solve.all_failed dynamic_services=%s services=%s turns=%s strategies=%s duration_ms=%s",
            len(dynamic_services),
            len(services),
            len(active_turnos),
            strategies,
            int((time.monotonic() - started) * 1000),
        )
        return None

    def _validate_solution(
        self,
        assignments: Dict[str, TurnoPlan],
        dynamic_services: List[ServicioPlan],
        locked_by_turn: Dict[str, List[ServicioPlan]],
        turnos: List[TurnoPlan],
    ) -> RouteEvaluation:
        dynamic_by_auth = {servicio.autorizacion: servicio for servicio in dynamic_services}
        missing = sorted(set(dynamic_by_auth) - set(assignments))
        if missing:
            return RouteEvaluation(False, 0.0, f"ASIGNACION_FALTANTE:{','.join(missing[:5])}")

        services_by_turn: Dict[str, List[ServicioPlan]] = {
            turno.id_turno: list(locked_by_turn.get(turno.id_turno, []))
            for turno in turnos
        }
        for autorizacion, turno in assignments.items():
            services_by_turn.setdefault(turno.id_turno, []).append(dynamic_by_auth[autorizacion])

        total_distance = 0.0
        for turno in turnos:
            evaluation = self._route_metrics(turno, services_by_turn.get(turno.id_turno, []))
            if not evaluation.feasible:
                return evaluation
            total_distance += evaluation.total_distance_km
        return RouteEvaluation(True, total_distance)

    def _arc_cost_units(
        self,
        vehicle_id: int,
        from_node: int,
        to_node: int,
        turnos: List[TurnoPlan],
        current_turn_by_auth: Dict[str, str],
        service_for_node,
        distance_km_for_vehicle,
        manual_blocks_by_turn: Dict[str, List[ManualBlock]],
        move_penalty_units: int,
    ) -> int:
        servicio = service_for_node(to_node)
        distance_units = int(round(distance_km_for_vehicle(vehicle_id, from_node, to_node) * 1000))
        if servicio is None:
            return distance_units
        if self._overlaps_manual_block(servicio, turnos[vehicle_id], manual_blocks_by_turn):
            distance_units += MANUAL_BLOCK_PENALTY_UNITS
        current_turn = current_turn_by_auth.get(servicio.autorizacion)
        if current_turn and current_turn != turnos[vehicle_id].id_turno:
            return distance_units + move_penalty_units
        return distance_units

    def _is_service_compatible_with_turn(
        self,
        servicio: ServicioPlan,
        turno: TurnoPlan,
        now_value,
        manual_blocks_by_turn: Optional[Dict[str, List[ManualBlock]]] = None,
    ) -> bool:
        if turno not in self._candidate_turns(servicio, [turno], now_value):
            return False
        if not (
            self._within_radius(turno.punto_inicio, servicio.origen)
            and self._within_radius(
                turno.punto_inicio,
                servicio.destino,
            )
        ):
            return False
        return True

    def _build_manual_blocks_by_turn(
        self,
        manual_services: List[ServicioPlan],
        turnos: List[TurnoPlan],
        normal_services_by_turn: Dict[str, List[ServicioPlan]],
    ) -> Dict[str, List[ManualBlock]]:
        turnos_by_id = {turno.id_turno: turno for turno in turnos}
        blocks_by_turn: Dict[str, List[ManualBlock]] = {}
        for manual in manual_services:
            turno_id = manual.id_turno.strip()
            if not turno_id:
                logger.warning(
                    "engine.ortools.manual_block.ignored autorizacion=%s reason=missing_turn",
                    manual.autorizacion,
                )
                continue
            turno = turnos_by_id.get(turno_id)
            if not turno:
                logger.warning(
                    "engine.ortools.manual_block.ignored autorizacion=%s turno=%s reason=unknown_turn",
                    manual.autorizacion,
                    turno_id,
                )
                continue
            block = self._manual_block_for_turn(
                manual=manual,
                turno=turno,
                normal_services=normal_services_by_turn.get(turno_id, []),
            )
            if not block:
                continue
            blocks_by_turn.setdefault(turno_id, []).append(block)
        return blocks_by_turn

    def _manual_block_for_turn(
        self,
        manual: ServicioPlan,
        turno: TurnoPlan,
        normal_services: List[ServicioPlan],
    ) -> Optional[ManualBlock]:
        if not manual.fecha_servicio or not turno.fecha_inicio_turno or not turno.fecha_fin_turno:
            return None

        travel_to_manual_minutes = self._manual_travel_to_origin_minutes(manual, turno)
        manual_trip_minutes = self._manual_trip_minutes(manual)
        block_start = manual.fecha_servicio - timedelta(minutes=travel_to_manual_minutes)
        block_end = manual.fecha_servicio + timedelta(
            minutes=self.settings.onsite_minutes + manual_trip_minutes
        )
        block_start = max(block_start, turno.fecha_inicio_turno)
        block_end = min(block_end, turno.fecha_fin_turno)

        service_windows = self._service_windows_for_turn(turno, normal_services)
        for servicio, service_start, service_end in service_windows:
            if not servicio.fecha_servicio:
                continue
            if service_end <= block_start or service_start >= block_end:
                continue
            if servicio.fecha_servicio <= manual.fecha_servicio:
                block_start = max(block_start, service_end)
            else:
                block_end = min(block_end, service_start)

        if block_end <= block_start:
            logger.warning(
                "engine.ortools.manual_block.zero autorizacion=%s turno=%s",
                manual.autorizacion,
                turno.id_turno,
            )
            return None

        logger.warning(
            "engine.ortools.manual_block.created autorizacion=%s turno=%s start=%s end=%s",
            manual.autorizacion,
            turno.id_turno,
            block_start.isoformat(),
            block_end.isoformat(),
        )
        return ManualBlock(
            autorizacion=manual.autorizacion,
            id_turno=turno.id_turno,
            start=block_start,
            end=block_end,
            manual=manual,
        )

    def _manual_travel_to_origin_minutes(self, manual: ServicioPlan, turno: TurnoPlan) -> float:
        if not self._has_coordinates(manual.origen):
            return MANUAL_FALLBACK_TRAVEL_MINUTES
        distance_to_manual = haversine_km(
            turno.punto_inicio.lat,
            turno.punto_inicio.lng,
            manual.origen.lat,
            manual.origen.lng,
        )
        return travel_minutes(distance_to_manual)

    def _manual_trip_minutes(self, manual: ServicioPlan) -> float:
        if not self._has_coordinates(manual.origen) or not self._has_coordinates(manual.destino):
            return MANUAL_FALLBACK_TRAVEL_MINUTES
        manual_distance = haversine_km(
            manual.origen.lat,
            manual.origen.lng,
            manual.destino.lat,
            manual.destino.lng,
        )
        return travel_minutes(manual_distance)

    def _manual_travel_to_service_minutes(self, manual: ServicioPlan, servicio: ServicioPlan) -> float:
        if not self._has_coordinates(manual.destino) or not self._has_coordinates(servicio.origen):
            return MANUAL_FALLBACK_TRAVEL_MINUTES
        distance_to_service = haversine_km(
            manual.destino.lat,
            manual.destino.lng,
            servicio.origen.lat,
            servicio.origen.lng,
        )
        return travel_minutes(distance_to_service)

    def _has_coordinates(self, point) -> bool:
        return not (point.lat == 0.0 and point.lng == 0.0)

    def _service_windows_for_turn(
        self,
        turno: TurnoPlan,
        services: List[ServicioPlan],
    ) -> List[tuple[ServicioPlan, datetime, datetime]]:
        if not turno.fecha_inicio_turno:
            return []
        ordered = sorted(
            [servicio for servicio in services if servicio.fecha_servicio],
            key=lambda servicio: (servicio.fecha_servicio, servicio.autorizacion),
        )
        current_time = turno.fecha_inicio_turno - timedelta(minutes=self.settings.pre_shift_travel_minutes)
        current_position = turno.punto_inicio
        windows: List[tuple[ServicioPlan, datetime, datetime]] = []

        for servicio in ordered:
            distance_to_origin = haversine_km(
                current_position.lat,
                current_position.lng,
                servicio.origen.lat,
                servicio.origen.lng,
            )
            arrival_time = current_time + timedelta(minutes=travel_minutes(distance_to_origin))
            service_start_time = max(arrival_time, servicio.fecha_servicio)
            service_distance = haversine_km(
                servicio.origen.lat,
                servicio.origen.lng,
                servicio.destino.lat,
                servicio.destino.lng,
            )
            end_time = service_start_time + timedelta(
                minutes=self.settings.onsite_minutes + travel_minutes(service_distance)
            )
            windows.append((servicio, service_start_time, end_time))
            current_time = end_time
            current_position = servicio.destino
        return windows

    def _overlaps_manual_block(
        self,
        servicio: ServicioPlan,
        turno: TurnoPlan,
        manual_blocks_by_turn: Dict[str, List[ManualBlock]],
    ) -> bool:
        if not servicio.fecha_servicio:
            return False
        blocks = manual_blocks_by_turn.get(turno.id_turno, [])
        if not blocks:
            return False
        service_distance = haversine_km(
            servicio.origen.lat,
            servicio.origen.lng,
            servicio.destino.lat,
            servicio.destino.lng,
        )
        service_start = servicio.fecha_servicio
        service_end = service_start + timedelta(
            minutes=self.settings.onsite_minutes + travel_minutes(service_distance)
        )
        for block in blocks:
            block_end = block.end + timedelta(
                minutes=self._manual_travel_to_service_minutes(block.manual, servicio)
            )
            if service_start < block_end and service_end > block.start:
                logger.debug(
                    "engine.ortools.manual_block.candidate_penalized autorizacion=%s turno=%s manual=%s",
                    servicio.autorizacion,
                    turno.id_turno,
                    block.autorizacion,
                )
                return True
        return False

    def _valid_turn(self, turno: TurnoPlan, now_value) -> bool:
        return bool(turno.fecha_inicio_turno and turno.fecha_fin_turno and turno.fecha_fin_turno >= now_value)

    def _locked_turn_id(self, servicio: ServicioPlan, locked_by_turn: Dict[str, List[ServicioPlan]]) -> str:
        for turno_id, services in locked_by_turn.items():
            if any(item.autorizacion == servicio.autorizacion for item in services):
                return turno_id
        return ""

    def _unique_services(self, services: List[ServicioPlan]) -> List[ServicioPlan]:
        selected: Dict[str, ServicioPlan] = {}
        for servicio in services:
            selected[servicio.autorizacion] = servicio
        return list(selected.values())

    def _reference_time(self, turnos: List[TurnoPlan], services: List[ServicioPlan]) -> datetime:
        values = [
            value
            for value in [
                *(
                    turno.fecha_inicio_turno - timedelta(minutes=self.settings.pre_shift_travel_minutes)
                    for turno in turnos
                    if turno.fecha_inicio_turno
                ),
                *(turno.fecha_inicio_turno for turno in turnos),
                *(servicio.fecha_servicio for servicio in services),
            ]
            if value is not None
        ]
        return min(values)

    def _minutes_since(self, reference_time: datetime, value: Optional[datetime]) -> int:
        if value is None:
            return 0
        return int(math.floor((value - reference_time).total_seconds() / 60))

    def _horizon_minutes(self, reference_time: datetime, turnos: List[TurnoPlan], services: List[ServicioPlan]) -> int:
        end_values = [turno.fecha_fin_turno for turno in turnos if turno.fecha_fin_turno]
        service_values = [servicio.fecha_servicio for servicio in services if servicio.fecha_servicio]
        latest = max([*end_values, *service_values])
        return max(1, self._minutes_since(reference_time, latest) + 24 * 60)

    def _max_wait_minutes(self, reference_time: datetime, turnos: List[TurnoPlan]) -> int:
        earliest = min(turno.fecha_inicio_turno for turno in turnos if turno.fecha_inicio_turno)
        latest = max(turno.fecha_fin_turno for turno in turnos if turno.fecha_fin_turno)
        return max(0, self._minutes_since(earliest, latest) + self._minutes_since(reference_time, earliest))

    def _first_solution_strategy(self, strategy_name: Optional[str] = None):
        from ortools.constraint_solver import routing_enums_pb2

        selected = (strategy_name or self.settings.ortools_first_solution_strategy).strip().upper()
        strategies = {
            "PATH_CHEAPEST_ARC": routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC,
            "PARALLEL_CHEAPEST_INSERTION": routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION,
            "LOCAL_CHEAPEST_INSERTION": routing_enums_pb2.FirstSolutionStrategy.LOCAL_CHEAPEST_INSERTION,
            "GLOBAL_CHEAPEST_ARC": routing_enums_pb2.FirstSolutionStrategy.GLOBAL_CHEAPEST_ARC,
        }
        return strategies.get(
            selected,
            routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION,
        )

    def _local_search_metaheuristic(self):
        from ortools.constraint_solver import routing_enums_pb2

        strategies = {
            "GUIDED_LOCAL_SEARCH": routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH,
            "GREEDY_DESCENT": routing_enums_pb2.LocalSearchMetaheuristic.GREEDY_DESCENT,
            "SIMULATED_ANNEALING": routing_enums_pb2.LocalSearchMetaheuristic.SIMULATED_ANNEALING,
            "TABU_SEARCH": routing_enums_pb2.LocalSearchMetaheuristic.TABU_SEARCH,
            "AUTOMATIC": routing_enums_pb2.LocalSearchMetaheuristic.AUTOMATIC,
        }
        return strategies.get(
            self.settings.ortools_local_search_metaheuristic,
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH,
        )
