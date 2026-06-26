"""Orquestacion principal de servicios RVE."""

from __future__ import annotations

import logging
import inspect
import time
from dataclasses import dataclass
from datetime import timedelta
from itertools import combinations
from typing import Dict, List, Optional

from gspread.exceptions import APIError

from or_engine.engine import DecisionEngine
from or_engine.models import ServicioPlan, TurnoPlan
from services.google_sheets import DispatchSnapshot, GoogleSheetsRepository
from services.google_matrix_estimator import reset_estimation_context, set_estimation_context
from services.lock_service import SheetLockService
from services.notification_service import RejectionNotificationService
from utils.config import get_settings
from utils.constants import (
    ESTADO_ASIGNADO_FINAL,
    ESTADO_CANCELADO,
    ESTADO_COMPLETADO,
    ESTADO_MANUAL,
    ESTADO_PREASIGNADO,
    ESTADO_RECIBIDO,
    ESTADO_RECHAZADO_RVE,
    ESTADO_URGENTE_GESTIONAR_MANUAL,
    ESTADOS_ACEPTADOS,
    ESTADOS_MANUALES,
    ESTADOS_TERMINALES,
    NOMBRE_CONDUCTOR_PENDIENTE,
    PREASIGNACION_CONGELADA,
    TIPO_ENRUTAMIENTO_AUTOMATICO,
    TIPO_ENRUTAMIENTO_MANUAL,
)
from utils.time_utils import now_bogota

logger = logging.getLogger(__name__)
RECOVERY_MAX_MANUAL_MARKS = 4
RECOVERY_MAX_EVALUATED_SETS = 80
RECOVERY_WRITE_RESERVE_SECONDS = 20.0


@dataclass
class DispatchOutcome:
    """Resultado final expuesto por el servicio de orquestacion."""

    autorizacion: str
    decision: str
    razon: str
    id_turno: str | None = None
    cedula_conductor: str | None = None
    nombre_conductor: str | None = None


class DispatchTimeoutError(RuntimeError):
    """Se dispara cuando el request excede el presupuesto de tiempo."""


class DispatchRetryRequiredError(RuntimeError):
    """Se dispara cuando no se pudo confirmar el estado final en Sheets."""


class DispatchService:
    """Orquesta lectura, decision OR y persistencia."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.repository = GoogleSheetsRepository()
        self.lock_service = SheetLockService(self.repository)
        self.notification_service = RejectionNotificationService()
        self.engine = self._build_decision_engine()

    def _build_decision_engine(self):
        engine_type = self.settings.decision_engine
        if engine_type == "legacy":
            return DecisionEngine()
        if engine_type == "gemini":
            try:
                from or_engine.gemini_routing_engine import GeminiRoutingEngine

                logger.info("dispatch.engine.gemini_loaded")
                return GeminiRoutingEngine()
            except Exception:
                logger.exception("dispatch.engine.gemini_unavailable fallback=ortools")
        try:
            from or_engine.routing_engine import RoutingDecisionEngine
        except ImportError:
            logger.exception("dispatch.engine.ortools_unavailable fallback=legacy")
            return DecisionEngine()
        return RoutingDecisionEngine()

    def procesar_nuevo_servicio(self, payload: dict, request_id: str = "-") -> DispatchOutcome:
        """Procesa un servicio nuevo bajo lock global."""

        autorizacion = payload.get("autorizacion")
        logger.info(
            "dispatch.start request_id=%s autorizacion=%s",
            request_id,
            autorizacion,
        )
        started_at = time.monotonic()
        deadline_at = started_at + self.settings.service_timeout_seconds
        deadline_token = None
        estimation_token = set_estimation_context("servicio", request_id, str(autorizacion or ""))
        servicio_confirmado: ServicioPlan | None = None
        servicio_escrito = False
        try:
            if hasattr(self.repository, "push_request_deadline"):
                deadline_token = self.repository.push_request_deadline(deadline_at)
            with self.lock_service.locked(max_attempts=2):
                logger.info(
                    "dispatch.lock_acquired request_id=%s autorizacion=%s",
                    request_id,
                    payload.get("autorizacion"),
                )
                snapshot = self._timed(
                    "load_snapshot",
                    request_id,
                    self._load_snapshot,
                )
                self._timed(
                    "reconcile_terminal_services",
                    request_id,
                    self.repository.reconcile_terminal_services,
                    snapshot.servicios,
                    snapshot.preasignaciones,
                )
                servicios = snapshot.servicios
                existente = snapshot.servicios_by_auth.get(payload["autorizacion"])
                if existente and existente.estado_operacion in ESTADOS_ACEPTADOS:
                    turno_actual = self._get_current_turn_from_snapshot(snapshot, existente.autorizacion)
                    return self._accepted_outcome(
                        autorizacion=existente.autorizacion,
                        razon=f"Servicio ya procesado en estado {existente.estado_operacion}",
                        estado_servicio=existente.estado_operacion,
                        turno_actual=turno_actual,
                        servicio_actual=existente,
                    )
                if existente and existente.estado_operacion in ESTADOS_MANUALES:
                    return DispatchOutcome(
                        autorizacion=existente.autorizacion,
                        decision="ACEPTAR",
                        razon=f"Servicio ya procesado en estado {existente.estado_operacion}",
                        id_turno=existente.id_turno or None,
                        cedula_conductor=existente.cedula_conductor or None,
                        nombre_conductor=existente.nombre_conductor or None,
                    )

                if existente and existente.estado_operacion in ESTADOS_TERMINALES:
                    outcome = DispatchOutcome(
                        autorizacion=existente.autorizacion,
                        decision="RECHAZAR",
                        razon=f"Servicio ya procesado en estado {existente.estado_operacion}",
                    )
                    self._notify_rejection(outcome, request_id)
                    return outcome
                routing_mode = self.repository.get_routing_mode()
                if routing_mode == TIPO_ENRUTAMIENTO_MANUAL:
                    return self._procesar_servicio_en_modo_manual(
                        payload=payload,
                        request_id=request_id,
                        snapshot=snapshot,
                        existente=existente,
                        started_at=started_at,
                    )
                servicio_confirmado = existente
                servicio = existente if existente and existente.estado_operacion == ESTADO_RECIBIDO else None
                if servicio is None:
                    next_row_index = snapshot.next_servicio_row_index
                    self._timed(
                        "append_servicio_recibido",
                        request_id,
                        self.repository.append_servicio_recibido,
                        payload,
                    )
                    servicio_escrito = True
                    self._ensure_budget(started_at, "append_servicio_recibido")
                    servicio = self.repository.build_servicio_from_payload(payload, next_row_index)
                    snapshot.next_servicio_row_index += 1
                    snapshot.servicios.append(servicio)
                    snapshot.servicios_by_auth[servicio.autorizacion] = servicio
                    servicios = snapshot.servicios
                    servicio_confirmado = servicio
                    logger.info("Servicio %s escrito/confirmado en Sheets", servicio.autorizacion)
                else:
                    logger.info(
                        "Servicio %s reanudado desde estado RECIBIDO",
                        servicio.autorizacion,
                    )
                    servicios = servicios
                    servicio_confirmado = servicio
                turnos = snapshot.turnos
                preasignaciones = snapshot.preasignaciones
                logger.info(
                    "dispatch.snapshot request_id=%s autorizacion=%s servicios=%s turnos=%s preasignaciones=%s",
                    request_id,
                    servicio.autorizacion,
                    len(servicios),
                    len(turnos),
                    len(preasignaciones),
                )

                outcome = self._timed(
                    "engine.decidir",
                    request_id,
                    self.engine.decidir,
                    servicio,
                    servicios,
                    turnos,
                    preasignaciones,
                )
                if outcome.decision == "RECHAZAR":
                    analisis_rechazo = self.repository.classify_rejection_analysis(
                        payload=payload,
                        servicio=servicio,
                        razon=outcome.razon,
                        analysis_code=outcome.analysis_code,
                    )
                    persisted = self._timed(
                        "update_servicio_estado_rechazado",
                        request_id,
                        self.repository.update_servicio_estado,
                        servicio.autorizacion,
                        ESTADO_RECHAZADO_RVE,
                        "",
                        "",
                        "",
                        "",
                        analisis=analisis_rechazo,
                    )
                    if not persisted and servicio.row_index:
                        self.repository.update_servicio_estado_by_row(
                            servicio.row_index,
                            ESTADO_RECHAZADO_RVE,
                            cedula_conductor="",
                            nombre_conductor="",
                            id_turno="",
                            correos="",
                            analisis=analisis_rechazo,
                        )
                    logger.info(
                        "dispatch.rejected request_id=%s autorizacion=%s razon=%s",
                        request_id,
                        servicio.autorizacion,
                        outcome.razon,
                    )
                    outcome = DispatchOutcome(
                        autorizacion=servicio.autorizacion,
                        decision=outcome.decision,
                        razon=outcome.razon,
                    )
                    self._notify_rejection(outcome, request_id)
                    return outcome

                if servicio.autorizacion not in outcome.assignments:
                    raise RuntimeError(
                        f"Decision aceptada sin turno asignado para {servicio.autorizacion}"
                    )

                servicios_map: Dict[str, ServicioPlan] = snapshot.servicios_by_auth
                self._timed(
                    "apply_dynamic_assignments",
                    request_id,
                    self.repository.apply_dynamic_assignments,
                    new_service_auth=servicio.autorizacion,
                    assignments=outcome.assignments,
                    existing_services=servicios_map,
                    current_preasignaciones=preasignaciones,
                )
                self._ensure_budget(started_at, "apply_dynamic_assignments")
                if servicio.estado_operacion != ESTADO_ASIGNADO_FINAL:
                    servicio.estado_operacion = ESTADO_PREASIGNADO
                    servicio.estado_tecnico = ESTADO_PREASIGNADO
                    servicio.id_turno = ""
                    servicio.cedula_conductor = ""
                    servicio.nombre_conductor = NOMBRE_CONDUCTOR_PENDIENTE
                logger.info(
                    "dispatch.accepted request_id=%s autorizacion=%s asignaciones=%s",
                    request_id,
                    servicio.autorizacion,
                    len(outcome.assignments),
                )
                turno = outcome.assignments[servicio.autorizacion]
                return self._accepted_outcome(
                    autorizacion=servicio.autorizacion,
                    razon=outcome.razon,
                    estado_servicio=servicio.estado_operacion,
                    turno_actual=turno,
                    servicio_actual=servicio,
                )
        except Exception as exc:
            logger.exception(
                "dispatch.failure request_id=%s autorizacion=%s error_type=%s error=%s",
                request_id,
                autorizacion,
                type(exc).__name__,
                str(exc),
            )
            return self._recover_or_reject(
                payload,
                request_id,
                exc,
                known_service=servicio_confirmado,
                servicio_escrito=servicio_escrito,
            )
        finally:
            reset_estimation_context(estimation_token)
            if deadline_token is not None and hasattr(self.repository, "pop_request_deadline"):
                self.repository.pop_request_deadline(deadline_token)

    def _procesar_servicio_en_modo_manual(
        self,
        payload: dict,
        request_id: str,
        snapshot: DispatchSnapshot,
        existente: ServicioPlan | None,
        started_at: float,
    ) -> DispatchOutcome:
        """Acepta o rechaza por cobertura horaria sin ejecutar ORTools."""

        row_index = existente.row_index if existente and existente.row_index else snapshot.next_servicio_row_index
        servicio = existente or self.repository.build_servicio_from_payload(payload, row_index)
        decision, razon = self._evaluar_cobertura_manual(servicio, snapshot.turnos)
        if decision == "RECHAZAR":
            analisis_rechazo = self.repository.classify_rejection_analysis(
                payload=payload,
                servicio=servicio,
                razon=razon,
            )
            if existente and existente.row_index:
                persisted = self.repository.update_servicio_estado_by_row(
                    existente.row_index,
                    ESTADO_RECHAZADO_RVE,
                    cedula_conductor="",
                    nombre_conductor="",
                    id_turno="",
                    correos="",
                    analisis=analisis_rechazo,
                )
            else:
                persisted = self.repository.ensure_servicio_rechazado(
                    payload,
                    razon,
                    skip_lookup=True,
                    analisis=analisis_rechazo,
                )
            logger.warning(
                "dispatch.manual.rejected request_id=%s autorizacion=%s persisted=%s razon=%s",
                request_id,
                payload.get("autorizacion"),
                persisted,
                razon,
            )
            outcome = DispatchOutcome(
                autorizacion=payload.get("autorizacion"),
                decision="RECHAZAR",
                razon=razon,
            )
            self._notify_rejection(outcome, request_id)
            return outcome

        if existente and existente.row_index:
            self.repository.update_servicio_estado_by_row(
                existente.row_index,
                ESTADO_MANUAL,
                cedula_conductor="",
                nombre_conductor="",
                id_turno="",
                correos="",
            )
        else:
            self._timed(
                "append_servicio_manual",
                request_id,
                self.repository.append_servicio_manual,
                payload,
            )
            self._ensure_budget(started_at, "append_servicio_manual")
        logger.warning(
            "dispatch.manual.accepted request_id=%s autorizacion=%s",
            request_id,
            payload.get("autorizacion"),
        )
        return DispatchOutcome(
            autorizacion=payload.get("autorizacion"),
            decision="ACEPTAR",
            razon=razon,
            id_turno=None,
            cedula_conductor=None,
            nombre_conductor=None,
        )

    def _evaluar_cobertura_manual(
        self,
        servicio: ServicioPlan,
        turnos: List[TurnoPlan],
    ) -> tuple[str, str]:
        now_value = now_bogota()
        if not servicio.fecha_servicio:
            return "RECHAZAR", "FECHA_SERVICIO invalida"
        min_allowed = now_value + timedelta(minutes=self.settings.min_notice_minutes)
        max_allowed = now_value + timedelta(days=self.settings.horizon_days)
        if servicio.fecha_servicio < min_allowed:
            return (
                "RECHAZAR",
                f"FECHA_SERVICIO debe ser al menos {self.settings.min_notice_minutes} minutos despues de ahora",
            )
        if servicio.fecha_servicio > max_allowed:
            return (
                "RECHAZAR",
                f"FECHA_SERVICIO excede el horizonte de {self.settings.horizon_days} dias",
            )
        if not self._turnos_compatibles_modo_manual(servicio, turnos, now_value):
            return "RECHAZAR", "No existe turno compatible para departamento, servicio y horario"
        return "ACEPTAR", "Servicio aceptado en modo MANUAL; requiere gestion operativa manual"

    def _turnos_compatibles_modo_manual(
        self,
        servicio: ServicioPlan,
        turnos: List[TurnoPlan],
        now_value,
    ) -> List[TurnoPlan]:
        compatibles: List[TurnoPlan] = []
        servicio_nombre = servicio.servicio.strip().upper()
        tipo_servicio = servicio.tipo_servicio.strip().upper()
        departamento = servicio.departamento.strip().upper()
        for turno in turnos:
            if not turno.fecha_inicio_turno or not turno.fecha_fin_turno or not servicio.fecha_servicio:
                continue
            if turno.fecha_fin_turno < now_value:
                continue
            if turno.departamento.strip().upper() != departamento:
                continue
            if turno.servicio.strip().upper() != servicio_nombre:
                continue
            if turno.tipo_servicio.strip().upper() != tipo_servicio:
                continue
            if not (turno.fecha_inicio_turno <= servicio.fecha_servicio <= turno.fecha_fin_turno):
                continue
            compatibles.append(turno)
        return compatibles

    def cancelar_servicio(self, autorizacion: str, request_id: str = "-") -> dict:
        """Cancela un servicio sin destruir informacion historica."""

        logger.info("dispatch.cancel.start request_id=%s autorizacion=%s", request_id, autorizacion)
        with self.lock_service.locked():
            snapshot = self._load_snapshot()
            self._timed(
                "reconcile_terminal_services",
                request_id,
                self.repository.reconcile_terminal_services,
                snapshot.servicios,
                snapshot.preasignaciones,
            )
            self._timed(
                "lock_due_services",
                request_id,
                self.repository.lock_due_services,
                snapshot.servicios,
                snapshot.preasignaciones,
                snapshot.turnos,
            )
            servicio = self.repository.get_servicio(autorizacion)
            if not servicio:
                return {"resultado": "NO_ENCONTRADO", "estado": ESTADO_CANCELADO}
            if servicio.estado_operacion == ESTADO_CANCELADO:
                return {"resultado": "OK", "estado": ESTADO_CANCELADO}
            self._timed(
                "marcar_servicio_cancelado",
                request_id,
                self.repository.marcar_servicio_cancelado,
                autorizacion,
            )
            return {"resultado": "OK", "estado": ESTADO_CANCELADO}

    def bloquear_servicios_proximos(self, request_id: str = "-") -> dict:
        """Congela servicios que entraron a la ventana de asignacion final."""

        logger.info("dispatch.validar.start request_id=%s", request_id)
        started_at = time.monotonic()
        deadline_at = started_at + self.settings.service_timeout_seconds
        deadline_token = None
        estimation_token = set_estimation_context("validar", request_id)
        try:
            if hasattr(self.repository, "push_request_deadline"):
                deadline_token = self.repository.push_request_deadline(deadline_at)
            with self.lock_service.locked():
                snapshot = self._load_snapshot()
                self._ensure_budget(started_at, "validar.load_snapshot")
                self._timed(
                    "reconcile_terminal_services",
                    request_id,
                    self.repository.reconcile_terminal_services,
                    snapshot.servicios,
                    snapshot.preasignaciones,
                )
                self._ensure_budget(started_at, "validar.reconcile_terminal_services")
                routing_mode = self.repository.get_routing_mode()
                if routing_mode == TIPO_ENRUTAMIENTO_MANUAL:
                    manual_result = self._timed(
                        "finalize_manual_mode_preassignments",
                        request_id,
                        self.repository.finalize_manual_mode_preassignments,
                        snapshot.servicios,
                        snapshot.preasignaciones,
                        snapshot.turnos,
                    )
                    self._notify_manual_mode_urgent_services(manual_result, request_id)
                    return {
                        "resultado": "OK",
                        "tipo_enrutamiento": TIPO_ENRUTAMIENTO_MANUAL,
                        "cantidad": manual_result.get("asignados_final", 0),
                        "manual_mode": manual_result,
                        "factibilidad": {"omitida": True, "motivo": "TIPO_ENRUTAMIENTO=MANUAL"},
                    }
                lock_result = self._timed(
                    "lock_due_services",
                    request_id,
                    self.repository.lock_due_services,
                    snapshot.servicios,
                    snapshot.preasignaciones,
                    snapshot.turnos,
                )
                self._notify_missing_turns(lock_result.get("turnos_no_encontrados", []), request_id)
                self._ensure_budget(started_at, "validar.lock_due_services")
                recovery_result = self._timed(
                    "recover_department_feasibility",
                    request_id,
                    self._recover_department_feasibility,
                    snapshot,
                    deadline_at,
                )
                return {
                    "resultado": "OK",
                    "tipo_enrutamiento": TIPO_ENRUTAMIENTO_AUTOMATICO,
                    "cantidad": lock_result.get("cantidad", 0),
                    "lock_due_services": lock_result,
                    "factibilidad": recovery_result,
                }
        finally:
            reset_estimation_context(estimation_token)
            if deadline_token is not None and hasattr(self.repository, "pop_request_deadline"):
                self.repository.pop_request_deadline(deadline_token)

    def _recover_department_feasibility(
        self,
        snapshot: DispatchSnapshot,
        deadline_at: float | None = None,
    ) -> dict:
        """Recupera factibilidad con un set minimo y limite duro de cambios."""

        results = []
        for department in self._recovery_departments(snapshot):
            result = self._recover_single_department(snapshot, department, deadline_at)
            results.append(result)
            logger.warning(
                "dispatch.validar.feasibility departamento=%s factible=%s marked=%s dynamic=%s locked=%s",
                result["departamento"],
                result["factible"],
                ",".join(result["marcados"]),
                result["dynamic"],
                result["locked"],
            )
        return {"departamentos": results}

    def _recovery_departments(self, snapshot: DispatchSnapshot) -> List[str]:
        """Departamentos con servicios vigentes que requieren validar factibilidad."""

        now_value = now_bogota()
        departments: List[str] = []
        seen = set()
        for servicio in snapshot.servicios:
            if servicio.estado_operacion not in {ESTADO_PREASIGNADO, ESTADO_ASIGNADO_FINAL}:
                continue
            department = servicio.departamento.strip().upper()
            if not department or department in seen:
                continue
            if self.engine._is_historical_service(servicio, now_value):
                continue
            departments.append(department)
            seen.add(department)
        return departments

    def _recover_single_department(
        self,
        snapshot: DispatchSnapshot,
        department: str,
        deadline_at: float | None = None,
    ) -> dict:
        normalized_department = department.strip().upper()
        state = self._build_department_state(snapshot, normalized_department)
        base_result = self._recovery_result(normalized_department, state, [], True)
        if not state["dynamic_services"] and not any(state["locked_by_turn"].values()):
            return base_result
        solution = self._department_solution(state, deadline_at)
        if solution is not None:
            sync_auths = {servicio.autorizacion for servicio in state["orphan_services"]}
            if state["orphan_services"]:
                if self._remaining_recovery_budget_seconds(deadline_at) <= RECOVERY_WRITE_RESERVE_SECONDS:
                    result = self._recovery_result(normalized_department, state, [], False)
                    result.update(
                        {
                            "recovery_aborted": True,
                            "abort_reason": "timeout_before_orphan_rescue",
                            "candidatos_recientes": [
                                servicio.autorizacion
                                for servicio in self._ordered_recovery_candidates(state)[:RECOVERY_MAX_MANUAL_MARKS + 1]
                            ],
                        }
                    )
                    self._notify_critical_feasibility(result)
                    return result
            if self._solution_changes_assignments(solution, state, sync_auths):
                self._apply_recovery_solution(solution, snapshot, sync_service_auths=sync_auths)
                refreshed_state = self._build_department_state(snapshot, normalized_department)
                feasible_after = self._is_department_state_feasible(refreshed_state, deadline_at)
                if not feasible_after:
                    result = self._recovery_result(
                        normalized_department,
                        refreshed_state,
                        [],
                        False,
                        rescued=[servicio.autorizacion for servicio in state["orphan_services"]],
                    )
                    result.update({"recovery_aborted": True, "abort_reason": "persisted_solution_not_feasible"})
                    self._notify_critical_feasibility(result)
                    return result
                return self._recovery_result(
                    normalized_department,
                    refreshed_state,
                    [],
                    True,
                    rescued=[servicio.autorizacion for servicio in state["orphan_services"]],
                )
            return base_result

        recovery_set, evaluated_sets, recovery_solution = self._find_minimal_recovery_set(
            state,
            max_size=RECOVERY_MAX_MANUAL_MARKS,
            deadline_at=deadline_at,
        )
        if not recovery_set:
            result = self._recovery_result(normalized_department, state, [], False)
            result.update(
                {
                    "recovery_aborted": True,
                    "max_manual_marks": RECOVERY_MAX_MANUAL_MARKS,
                    "max_evaluated_sets": RECOVERY_MAX_EVALUATED_SETS,
                    "evaluated_sets": evaluated_sets,
                    "candidatos_recientes": [
                        servicio.autorizacion
                        for servicio in self._ordered_recovery_candidates(state)[:RECOVERY_MAX_MANUAL_MARKS + 1]
                    ],
                }
            )
            self._notify_critical_feasibility(result)
            return result

        if self._remaining_recovery_budget_seconds(deadline_at) <= RECOVERY_WRITE_RESERVE_SECONDS:
            result = self._recovery_result(normalized_department, state, [], False)
            result.update(
                {
                    "recovery_aborted": True,
                    "max_manual_marks": RECOVERY_MAX_MANUAL_MARKS,
                    "max_evaluated_sets": RECOVERY_MAX_EVALUATED_SETS,
                    "evaluated_sets": evaluated_sets,
                    "abort_reason": "timeout_before_write",
                    "candidatos_recientes": [
                        servicio.autorizacion
                        for servicio in self._ordered_recovery_candidates(state)[:RECOVERY_MAX_MANUAL_MARKS + 1]
                    ],
                }
            )
            self._notify_critical_feasibility(result)
            return result

        for servicio in recovery_set:
            self._mark_service_for_manual_recovery(servicio, state)
        if recovery_solution is not None:
            self._apply_recovery_solution(
                recovery_solution,
                snapshot,
                sync_service_auths={
                    servicio.autorizacion
                    for servicio in state["orphan_services"]
                    if servicio.autorizacion not in {marked.autorizacion for marked in recovery_set}
                },
            )

        refreshed_state = self._build_department_state(snapshot, normalized_department)
        feasible_after = self._is_department_state_feasible(refreshed_state, deadline_at)
        if not feasible_after:
            result = self._recovery_result(
                normalized_department,
                refreshed_state,
                [servicio.autorizacion for servicio in recovery_set],
                False,
                evaluated_sets=evaluated_sets,
                rescued=[
                    servicio.autorizacion
                    for servicio in state["orphan_services"]
                    if servicio.autorizacion not in {marked.autorizacion for marked in recovery_set}
                ],
            )
            result.update({"recovery_aborted": True, "abort_reason": "post_recovery_still_infeasible"})
            self._notify_critical_feasibility(result)
            return result
        return self._recovery_result(
            normalized_department,
            refreshed_state,
            [servicio.autorizacion for servicio in recovery_set],
            True,
            evaluated_sets=evaluated_sets,
            rescued=[
                servicio.autorizacion
                for servicio in state["orphan_services"]
                if servicio.autorizacion not in {marked.autorizacion for marked in recovery_set}
            ],
        )

    def _build_department_state(self, snapshot: DispatchSnapshot, department: str) -> dict:
        now_value = now_bogota()
        turnos = [
            turno
            for turno in snapshot.turnos
            if turno.departamento.strip().upper() == department
        ]
        turn_ids = {turno.id_turno for turno in turnos}
        assignments_by_auth = self.engine._select_current_assignments(snapshot.preasignaciones)
        locked_by_turn: Dict[str, List[ServicioPlan]] = {turno.id_turno: [] for turno in turnos}
        dynamic_services: List[ServicioPlan] = []
        current_turn_by_auth: Dict[str, str] = {}
        manual_services: List[ServicioPlan] = []
        orphan_services: List[ServicioPlan] = []
        orphan_turn_ids: set[str] = set()
        accepted_without_preassignment: List[str] = []
        normal_services_by_turn: Dict[str, List[ServicioPlan]] = {turno.id_turno: [] for turno in turnos}
        locked_recovery_services: List[ServicioPlan] = []

        for servicio in snapshot.servicios:
            if servicio.departamento.strip().upper() != department:
                continue
            if servicio.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio.estado_operacion == ESTADO_URGENTE_GESTIONAR_MANUAL:
                continue
            if servicio.estado_operacion == ESTADO_MANUAL:
                manual_services.append(servicio)
                continue
            if self.engine._is_historical_service(servicio, now_value):
                continue
            preasignacion = assignments_by_auth.get(servicio.autorizacion)
            if servicio.estado_operacion in ESTADOS_ACEPTADOS and not preasignacion:
                accepted_without_preassignment.append(servicio.autorizacion)
                continue
            if not preasignacion:
                continue
            if not preasignacion.id_turno:
                if servicio.estado_operacion in ESTADOS_ACEPTADOS:
                    dynamic_services.append(servicio)
                    orphan_services.append(servicio)
                    orphan_turn_ids.add(servicio.id_turno or "SIN_ID_TURNO")
                continue
            if preasignacion.id_turno not in turn_ids:
                if servicio.estado_operacion in ESTADOS_ACEPTADOS:
                    current_turn_by_auth[servicio.autorizacion] = preasignacion.id_turno
                    dynamic_services.append(servicio)
                    orphan_services.append(servicio)
                    orphan_turn_ids.add(preasignacion.id_turno)
                continue

            current_turn_by_auth[servicio.autorizacion] = preasignacion.id_turno
            normal_services_by_turn.setdefault(preasignacion.id_turno, []).append(servicio)
            if self.engine._is_locked(servicio, preasignacion, now_value):
                locked_by_turn.setdefault(preasignacion.id_turno, []).append(servicio)
                locked_recovery_services.append(servicio)
            else:
                dynamic_services.append(servicio)

        manual_blocks_by_turn = {}
        if hasattr(self.engine, "_build_manual_blocks_by_turn"):
            manual_blocks_by_turn = self.engine._build_manual_blocks_by_turn(
                manual_services=manual_services,
                turnos=turnos,
                normal_services_by_turn=normal_services_by_turn,
            )
        return {
            "department": department,
            "now_value": now_value,
            "turnos": turnos,
            "turnos_by_id": {turno.id_turno: turno for turno in turnos},
            "locked_by_turn": locked_by_turn,
            "dynamic_services": dynamic_services,
            "orphan_services": orphan_services,
            "orphan_turn_ids": sorted(orphan_turn_ids),
            "accepted_without_preassignment": accepted_without_preassignment,
            "current_turn_by_auth": current_turn_by_auth,
            "manual_blocks_by_turn": manual_blocks_by_turn,
            "manual_services": manual_services,
            "normal_services_by_turn": normal_services_by_turn,
            "assignments_by_auth": assignments_by_auth,
            "locked_recovery_services": locked_recovery_services,
        }

    def _is_department_state_feasible(self, state: dict, deadline_at: float | None = None) -> bool:
        if not state["dynamic_services"] and not any(state["locked_by_turn"].values()):
            return True
        return self._department_solution(state, deadline_at) is not None

    def _department_solution(self, state: dict, deadline_at: float | None = None):
        if not self._has_recovery_solver_budget(deadline_at):
            return None
        return self._solve_department_state(
            dynamic_services=state["dynamic_services"],
            state=state,
        )

    def _recovery_result(
        self,
        department: str,
        state: dict,
        marked: List[str],
        feasible: bool,
        evaluated_sets: int = 0,
        rescued: Optional[List[str]] = None,
    ) -> dict:
        return {
            "departamento": department,
            "factible": feasible,
            "marcados": marked,
            "rescatados": rescued or [],
            "dynamic": len(state["dynamic_services"]),
            "orphan": len(state.get("orphan_services", [])),
            "orphan_turn_ids": state.get("orphan_turn_ids", []),
            "locked": sum(len(items) for items in state["locked_by_turn"].values()),
            "manual_blocks": sum(len(items) for items in state["manual_blocks_by_turn"].values()),
            "recovery_aborted": False,
            "max_manual_marks": RECOVERY_MAX_MANUAL_MARKS,
            "max_evaluated_sets": RECOVERY_MAX_EVALUATED_SETS,
            "evaluated_sets": evaluated_sets,
        }

    def _find_minimal_recovery_set(
        self,
        state: dict,
        max_size: int,
        deadline_at: float | None = None,
    ) -> tuple[List[ServicioPlan], int, object | None]:
        candidates = self._ordered_recovery_candidates(state)
        evaluated_sets = 0
        for size in range(1, max_size + 1):
            for candidate_set in combinations(candidates, size):
                if evaluated_sets >= RECOVERY_MAX_EVALUATED_SETS or not self._has_recovery_solver_budget(deadline_at):
                    return [], evaluated_sets, None
                evaluated_sets += 1
                candidate_auths = {candidate.autorizacion for candidate in candidate_set}
                remaining = [
                    servicio
                    for servicio in state["dynamic_services"]
                    if servicio.autorizacion not in candidate_auths
                ]
                solution = self._solve_department_state(
                    remaining,
                    state,
                    excluded_auths=candidate_auths,
                    manual_candidate_services=list(candidate_set),
                )
                if solution is not None:
                    return list(candidate_set), evaluated_sets, solution
        return [], evaluated_sets, None

    def _remaining_recovery_budget_seconds(self, deadline_at: float | None) -> float:
        if deadline_at is None:
            return float("inf")
        return deadline_at - time.monotonic()

    def _has_recovery_solver_budget(self, deadline_at: float | None) -> bool:
        if deadline_at is None:
            return True
        max_solver_seconds = max(
            1.0,
            float(self.settings.ortools_local_search_seconds) * 2,
        )
        return self._remaining_recovery_budget_seconds(deadline_at) > (
            max_solver_seconds + RECOVERY_WRITE_RESERVE_SECONDS
        )

    def _solve_department_state(
        self,
        dynamic_services: List[ServicioPlan],
        state: dict,
        excluded_auths: Optional[set[str]] = None,
        manual_candidate_services: Optional[List[ServicioPlan]] = None,
    ):
        excluded_auths = excluded_auths or set()
        locked_by_turn = {
            turno_id: [
                servicio
                for servicio in servicios
                if servicio.autorizacion not in excluded_auths
            ]
            for turno_id, servicios in state["locked_by_turn"].items()
        }
        manual_blocks_by_turn = state["manual_blocks_by_turn"]
        if manual_candidate_services and hasattr(self.engine, "_build_manual_blocks_by_turn"):
            manual_auths = {servicio.autorizacion for servicio in manual_candidate_services}
            normal_services_by_turn = {
                turno_id: [
                    servicio
                    for servicio in servicios
                    if servicio.autorizacion not in manual_auths
                ]
                for turno_id, servicios in state["normal_services_by_turn"].items()
            }
            manual_blocks_by_turn = self.engine._build_manual_blocks_by_turn(
                manual_services=[
                    *state.get("manual_services", []),
                ],
                turnos=state["turnos"],
                normal_services_by_turn=normal_services_by_turn,
            )
        parameters = inspect.signature(self.engine._solve_dynamic_assignment).parameters
        kwargs = {
            "dynamic_services": dynamic_services,
            "locked_by_turn": locked_by_turn,
            "turnos": state["turnos"],
            "current_turn_by_auth": {
                servicio.autorizacion: state["current_turn_by_auth"][servicio.autorizacion]
                for servicio in dynamic_services
                if servicio.autorizacion in state["current_turn_by_auth"]
            },
            "now_value": state["now_value"],
        }
        if "manual_blocks_by_turn" in parameters:
            kwargs["manual_blocks_by_turn"] = manual_blocks_by_turn
        return self.engine._solve_dynamic_assignment(**kwargs)

    def _solution_changes_assignments(
        self,
        solution,
        state: dict,
        sync_service_auths: Optional[set[str]] = None,
    ) -> bool:
        assignments = getattr(solution, "assignments", None) or {}
        if not assignments:
            return False
        sync_service_auths = sync_service_auths or set()
        for autorizacion, turno in assignments.items():
            if autorizacion in sync_service_auths:
                return True
            if state["current_turn_by_auth"].get(autorizacion) != turno.id_turno:
                return True
        return False

    def _apply_recovery_solution(
        self,
        solution,
        snapshot: DispatchSnapshot,
        sync_service_auths: Optional[set[str]] = None,
    ) -> None:
        assignments = getattr(solution, "assignments", None)
        if not assignments:
            return
        self.repository.apply_dynamic_assignments(
            new_service_auth="",
            assignments=assignments,
            existing_services=snapshot.servicios_by_auth,
            current_preasignaciones=snapshot.preasignaciones,
            sync_service_auths=sync_service_auths or set(),
        )

    def _find_recovery_candidate(self, state: dict) -> Optional[ServicioPlan]:
        candidates = self._ordered_recovery_candidates(state)
        return candidates[0] if candidates else None

    def _ordered_recovery_candidates(self, state: dict) -> List[ServicioPlan]:
        dynamic_auths = {servicio.autorizacion for servicio in state["dynamic_services"]}
        candidates_by_auth: Dict[str, ServicioPlan] = {}
        for servicio in [*state["dynamic_services"], *state.get("locked_recovery_services", [])]:
            candidates_by_auth.setdefault(servicio.autorizacion, servicio)
        return sorted(
            candidates_by_auth.values(),
            key=lambda servicio: self._recovery_candidate_sort_key(servicio, dynamic_auths),
            reverse=True,
        )

    def _recovery_candidate_sort_key(self, servicio: ServicioPlan, dynamic_auths: set[str]) -> tuple:
        if servicio.fecha_creacion_servicio:
            creation_key = servicio.fecha_creacion_servicio.timestamp()
        elif servicio.fecha_servicio:
            creation_key = servicio.fecha_servicio.timestamp()
        else:
            creation_key = 0.0
        return (servicio.autorizacion in dynamic_auths, creation_key, servicio.row_index or 0)

    def _mark_service_for_manual_recovery(self, servicio: ServicioPlan, state: dict) -> None:
        preasignacion = state["assignments_by_auth"].get(servicio.autorizacion)
        turno = state["turnos_by_id"].get(preasignacion.id_turno) if preasignacion else None
        id_turno = servicio.id_turno or (preasignacion.id_turno if preasignacion else "")
        cedula = (
            servicio.cedula_conductor
            or (turno.cedula_conductor if turno else "")
            or (preasignacion.cedula_conductor if preasignacion else "")
        )
        nombre = (
            servicio.nombre_conductor
            or (turno.nombre_conductor if turno else "")
            or (preasignacion.nombre_tecnico_preasignacion if preasignacion else "")
        )
        correo = servicio.correos or (turno.correo if turno else "")

        if servicio.row_index:
            self.repository.update_servicio_estado_by_row(
                servicio.row_index,
                ESTADO_URGENTE_GESTIONAR_MANUAL,
                cedula_conductor=cedula or None,
                nombre_conductor=nombre or None,
                id_turno=id_turno or None,
                correos=correo or None,
            )
        else:
            self.repository.update_servicio_estado(
                servicio.autorizacion,
                ESTADO_URGENTE_GESTIONAR_MANUAL,
                cedula_conductor=cedula or None,
                nombre_conductor=nombre or None,
                id_turno=id_turno or None,
                correos=correo or None,
            )
        servicio.estado_operacion = ESTADO_URGENTE_GESTIONAR_MANUAL
        servicio.estado_tecnico = ESTADO_URGENTE_GESTIONAR_MANUAL
        if id_turno:
            servicio.id_turno = id_turno
        if cedula:
            servicio.cedula_conductor = cedula
        if nombre:
            servicio.nombre_conductor = nombre
        if correo:
            servicio.correos = correo
        if id_turno:
            self.repository.upsert_manual_preasignacion(
                servicio.autorizacion,
                id_turno=id_turno,
                cedula_conductor=cedula,
                nombre_conductor=nombre,
                estado_preasignacion=PREASIGNACION_CONGELADA,
            )
            if preasignacion:
                preasignacion.id_turno = id_turno
                preasignacion.cedula_conductor = cedula
                preasignacion.nombre_tecnico_preasignacion = nombre
                preasignacion.estado_preasignacion = PREASIGNACION_CONGELADA
        logger.warning(
            "dispatch.validar.recovery.marked autorizacion=%s estado=%s departamento=%s",
            servicio.autorizacion,
            ESTADO_URGENTE_GESTIONAR_MANUAL,
            state["department"],
        )

    def _notify_critical_feasibility(self, result: dict) -> None:
        logger.error(
            "dispatch.validar.recovery.aborted department=%s max_manual_marks=%s evaluated_sets=%s dynamic=%s locked=%s manual_blocks=%s candidates=%s",
            result["departamento"],
            result["max_manual_marks"],
            result["evaluated_sets"],
            result["dynamic"],
            result["locked"],
            result["manual_blocks"],
            ",".join(result.get("candidatos_recientes", [])),
        )
        text = (
            "🚨🚨🚨 *URGENTE: SISTEMA RVE INFACIBLE* 🚨🚨🚨\n"
            f"Departamento: {result['departamento']}\n"
            f"Servicios dinamicos: {result['dynamic']}\n"
            f"Servicios bloqueados/finales: {result['locked']}\n"
            f"Reservas manuales blandas: {result['manual_blocks']}\n"
            f"Sets evaluados: {result['evaluated_sets']}\n"
            f"Limite de sets evaluados: {result['max_evaluated_sets']}\n"
            f"Limite automatico: maximo {result['max_manual_marks']} servicios a URGENTE_GESTIONAR_MANUAL\n"
            f"Motivo de aborto: {result.get('abort_reason', 'requiere_intervencion_manual')}\n"
            "No se hicieron cambios automaticos porque la recuperacion requiere intervencion manual.\n"
            f"Candidatos recientes: {', '.join(result.get('candidatos_recientes', [])) or 'N/A'}"
        )
        try:
            self.notification_service.notify_critical_feasibility(text)
        except Exception:
            logger.exception(
                "dispatch.validar.recovery.alert_failed department=%s",
                result["departamento"],
            )

    def _notify_missing_turns(self, missing_turns: List[dict], request_id: str = "-") -> None:
        if not missing_turns:
            return
        lines = [
            "🚨🚨 *RVE: TURNO ASIGNADO NO EXISTE EN TURNOS_TECNICOS* 🚨🚨",
            "Se limpio el ID_TURNO en SERVICIOS y PREASIGNACIONES para que entre a refactibilizacion.",
            f"Request ID: {request_id}",
        ]
        for item in missing_turns[:10]:
            lines.append(
                "- Autorizacion: "
                f"{item.get('autorizacion', '')} | "
                f"Turno faltante: {item.get('id_turno', '')} | "
                f"Departamento: {item.get('departamento', '')}"
            )
        if len(missing_turns) > 10:
            lines.append(f"- Otros casos omitidos: {len(missing_turns) - 10}")
        try:
            self.notification_service.notify_critical_feasibility("\n".join(lines), request_id)
        except Exception:
            logger.exception(
                "dispatch.validar.missing_turns.alert_failed count=%s",
                len(missing_turns),
            )

    def _notify_manual_mode_urgent_services(self, manual_result: dict, request_id: str = "-") -> None:
        urgentes = manual_result.get("urgentes") or []
        if not urgentes:
            return
        text = (
            "🚨🚨 *RVE MODO MANUAL: SERVICIOS SIN PREASIGNACION VALIDA* 🚨🚨\n"
            "Se marcaron como URGENTE_GESTIONAR_MANUAL durante /validar.\n"
            f"Request ID: {request_id}\n"
            f"Cantidad: {len(urgentes)}\n"
            f"Autorizaciones: {', '.join(urgentes[:20])}"
        )
        if len(urgentes) > 20:
            text += f"\nOtros omitidos: {len(urgentes) - 20}"
        try:
            self.notification_service.notify_critical_feasibility(text, request_id)
        except Exception:
            logger.exception(
                "dispatch.validar.manual_mode.alert_failed count=%s",
                len(urgentes),
            )

    def configurar_tipo_enrutamiento(self, tipo_enrutamiento: str, request_id: str = "-") -> dict:
        """Cambia el modo de enrutamiento bajo lock."""

        logger.warning(
            "dispatch.config.routing_mode.start request_id=%s mode=%s",
            request_id,
            tipo_enrutamiento,
        )
        with self.lock_service.locked():
            mode = self.repository.set_routing_mode(tipo_enrutamiento)
            return {"resultado": "OK", "tipo_enrutamiento": mode}

    def completar_servicio(self, autorizacion: str, request_id: str = "-") -> dict:
        """Marca un servicio como completado."""

        logger.info("dispatch.complete.start request_id=%s autorizacion=%s", request_id, autorizacion)
        with self.lock_service.locked():
            snapshot = self._load_snapshot()
            self._timed(
                "reconcile_terminal_services",
                request_id,
                self.repository.reconcile_terminal_services,
                snapshot.servicios,
                snapshot.preasignaciones,
            )
            servicio = self.repository.get_servicio(autorizacion)
            if not servicio:
                return {"resultado": "NO_ENCONTRADO", "estado": ESTADO_COMPLETADO}
            if servicio.estado_operacion == ESTADO_COMPLETADO:
                return {"resultado": "OK", "estado": ESTADO_COMPLETADO}
            self._timed(
                "marcar_servicio_completado",
                request_id,
                self.repository.marcar_servicio_completado,
                autorizacion,
            )
            return {"resultado": "OK", "estado": ESTADO_COMPLETADO}

    def _get_current_turn(self, autorizacion: str) -> TurnoPlan | None:
        """Resuelve el turno vigente desde la ultima preasignacion del servicio."""

        turnos_by_id = {turno.id_turno: turno for turno in self.repository.list_turnos()}
        vigente = None
        for preasignacion in self.repository.list_preasignaciones():
            if preasignacion.autorizacion != autorizacion:
                continue
            if vigente is None or (
                preasignacion.row_index
                and vigente.row_index
                and preasignacion.row_index > vigente.row_index
            ):
                vigente = preasignacion
        if not vigente:
            return None
        return turnos_by_id.get(vigente.id_turno)

    def _get_current_turn_from_snapshot(
        self,
        snapshot: DispatchSnapshot,
        autorizacion: str,
    ) -> TurnoPlan | None:
        vigente = snapshot.preasignacion_vigente_by_auth.get(autorizacion)
        if not vigente:
            return None
        return snapshot.turnos_by_id.get(vigente.id_turno)

    def _load_snapshot(self) -> DispatchSnapshot:
        """Carga un snapshot de negocio si el repositorio lo soporta."""

        if hasattr(self.repository, "load_dispatch_snapshot"):
            return self.repository.load_dispatch_snapshot()
        servicios = self.repository.list_servicios()
        preasignaciones = self.repository.list_preasignaciones()
        turnos = self.repository.list_turnos()
        return DispatchSnapshot(
            servicios=servicios,
            preasignaciones=preasignaciones,
            turnos=turnos,
            servicios_by_auth={servicio.autorizacion: servicio for servicio in servicios},
            turnos_by_id={turno.id_turno: turno for turno in turnos},
            preasignacion_vigente_by_auth={},
            next_servicio_row_index=(max((servicio.row_index or 1) for servicio in servicios) if servicios else 1) + 1,
        )

    def _recover_or_reject(
        self,
        payload: dict,
        request_id: str,
        exc: Exception,
        known_service: ServicioPlan | None,
        servicio_escrito: bool,
    ) -> DispatchOutcome:
        """Recupera el estado persistido y fuerza rechazo por fila si la escritura ya existe."""

        existente = known_service

        if existente and existente.estado_operacion in {*ESTADOS_ACEPTADOS, *ESTADOS_MANUALES}:
            turno_actual = None
            try:
                turno_actual = self._get_current_turn(existente.autorizacion)
            except Exception:
                logger.exception(
                    "dispatch.recover.turn_lookup_error request_id=%s autorizacion=%s",
                    request_id,
                    payload.get("autorizacion"),
                )
            return self._accepted_outcome(
                autorizacion=existente.autorizacion,
                razon=f"Servicio recuperado tras fallo operativo en estado {existente.estado_operacion}",
                estado_servicio=existente.estado_operacion,
                turno_actual=turno_actual,
                servicio_actual=existente,
            )

        if existente and existente.estado_operacion in ESTADOS_TERMINALES:
            outcome = DispatchOutcome(
                autorizacion=existente.autorizacion,
                decision="RECHAZAR",
                razon=f"Servicio recuperado tras fallo operativo en estado {existente.estado_operacion}",
            )
            self._notify_rejection(outcome, request_id)
            return outcome

        razon = self._failure_reason(exc)
        analisis_rechazo = self.repository.classify_rejection_analysis(
            payload=payload,
            servicio=existente or known_service,
            razon=razon,
        )
        if existente and existente.row_index:
            try:
                recovery_deadline_token = None
                if hasattr(self.repository, "push_request_deadline"):
                    recovery_deadline_token = self.repository.push_request_deadline(
                        time.monotonic() + 15.0
                    )
                self.repository.update_servicio_estado_by_row(
                    existente.row_index,
                    ESTADO_RECHAZADO_RVE,
                    cedula_conductor="",
                    nombre_conductor="",
                    id_turno="",
                    correos="",
                    analisis=analisis_rechazo,
                )
                logger.warning(
                    "dispatch.recover.rejected_by_row request_id=%s autorizacion=%s row_index=%s razon=%s",
                    request_id,
                    existente.autorizacion,
                    existente.row_index,
                    razon,
                )
            except Exception:
                logger.exception(
                    "dispatch.recover.rejected_by_row_error request_id=%s autorizacion=%s row_index=%s",
                    request_id,
                    existente.autorizacion,
                    existente.row_index,
                )
            finally:
                if (
                    'recovery_deadline_token' in locals()
                    and recovery_deadline_token is not None
                    and hasattr(self.repository, "pop_request_deadline")
                ):
                    self.repository.pop_request_deadline(recovery_deadline_token)
        else:
            logger.warning(
                "dispatch.recover.no_row_to_reject request_id=%s autorizacion=%s razon=%s",
                request_id,
                payload.get("autorizacion"),
                razon,
            )
            try:
                persisted = self.repository.ensure_servicio_rechazado(
                    payload,
                    razon,
                    analisis=analisis_rechazo,
                )
                logger.warning(
                    "dispatch.recover.ensure_rechazado request_id=%s autorizacion=%s persisted=%s razon=%s",
                    request_id,
                    payload.get("autorizacion"),
                    persisted,
                    razon,
                )
            except Exception as persist_exc:
                logger.exception(
                    "dispatch.recover.ensure_rechazado_error request_id=%s autorizacion=%s error_type=%s error=%s",
                    request_id,
                    payload.get("autorizacion"),
                    type(persist_exc).__name__,
                    str(persist_exc),
                )
        outcome = DispatchOutcome(
            autorizacion=payload["autorizacion"],
            decision="RECHAZAR",
            razon=razon,
        )
        self._notify_rejection(outcome, request_id)
        return outcome

    def _failure_reason(self, exc: Exception) -> str:
        """Normaliza errores operativos a una razon de rechazo legible."""

        if isinstance(exc, DispatchTimeoutError):
            return "Rechazado por exceder el tiempo maximo de procesamiento"
        if isinstance(exc, TimeoutError):
            return "Rechazado por exceder el tiempo maximo de procesamiento"
        if isinstance(exc, APIError):
            return "Rechazado por indisponibilidad temporal de Google Sheets"
        if self._is_lock_busy_error(exc):
            return "Rechazado porque el sistema esta ocupado procesando otro servicio"
        return "Rechazado por fallo operativo interno"

    def _is_lock_busy_error(self, exc: Exception) -> bool:
        """Identifica el rechazo funcional por CONFIG ocupada."""

        return isinstance(exc, RuntimeError) and "Sistema ocupado" in str(exc)

    def _accepted_outcome(
        self,
        autorizacion: str,
        razon: str,
        estado_servicio: str,
        turno_actual: TurnoPlan | None,
        servicio_actual: ServicioPlan | None = None,
    ) -> DispatchOutcome:
        """Construye la respuesta al cliente segun si la asignacion ya esta congelada."""

        if estado_servicio == ESTADO_ASIGNADO_FINAL and turno_actual:
            return DispatchOutcome(
                autorizacion=autorizacion,
                decision="ACEPTAR",
                razon=razon,
                id_turno=turno_actual.id_turno,
                cedula_conductor=turno_actual.cedula_conductor,
                nombre_conductor=turno_actual.nombre_conductor,
            )

        if estado_servicio == ESTADO_ASIGNADO_FINAL and servicio_actual:
            return DispatchOutcome(
                autorizacion=autorizacion,
                decision="ACEPTAR",
                razon=razon,
                id_turno=servicio_actual.id_turno,
                cedula_conductor=servicio_actual.cedula_conductor,
                nombre_conductor=servicio_actual.nombre_conductor,
            )

        if estado_servicio in ESTADOS_ACEPTADOS:
            return DispatchOutcome(
                autorizacion=autorizacion,
                decision="ACEPTAR",
                razon=razon,
                id_turno="",
                cedula_conductor="",
                nombre_conductor=NOMBRE_CONDUCTOR_PENDIENTE,
            )

        return DispatchOutcome(
            autorizacion=autorizacion,
            decision="ACEPTAR",
            razon=razon,
        )

    def _notify_rejection(self, outcome: DispatchOutcome, request_id: str) -> None:
        """Agenda una notificacion externa para rechazos sin bloquear el request."""

        if outcome.decision != "RECHAZAR":
            return
        try:
            self.notification_service.notify_rejection(
                autorizacion=outcome.autorizacion,
                razon=outcome.razon,
                request_id=request_id,
            )
        except Exception:
            logger.exception(
                "dispatch.rejection_notification.schedule_failed request_id=%s autorizacion=%s",
                request_id,
                outcome.autorizacion,
            )

    def _ensure_budget(self, started_at: float, stage: str) -> None:
        """Corta el procesamiento si excede el presupuesto maximo."""

        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds > self.settings.service_timeout_seconds:
            raise DispatchTimeoutError(
                f"Tiempo maximo excedido en etapa {stage} ({elapsed_seconds:.1f}s)"
            )

    def _timed(self, stage: str, request_id: str, func, *args, **kwargs):
        """Ejecuta una etapa registrando su duracion."""

        start = time.monotonic()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "dispatch.stage request_id=%s stage=%s duration_ms=%s",
                request_id,
                stage,
                elapsed_ms,
            )
