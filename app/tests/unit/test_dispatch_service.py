"""Pruebas unitarias del servicio de orquestacion."""

import os
import sys
import time
import unittest
from contextlib import contextmanager
from datetime import timedelta
from types import ModuleType, SimpleNamespace

CURRENT_DIR = os.path.dirname(__file__)
APP_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

if "gspread" not in sys.modules:
    gspread_module = ModuleType("gspread")
    gspread_exceptions = ModuleType("gspread.exceptions")

    class APIError(Exception):
        pass

    gspread_exceptions.APIError = APIError
    gspread_module.exceptions = gspread_exceptions
    sys.modules["gspread"] = gspread_module
    sys.modules["gspread.exceptions"] = gspread_exceptions

if "services.google_sheets" not in sys.modules:
    google_sheets_module = ModuleType("services.google_sheets")

    class DispatchSnapshot:  # pragma: no cover - stub de importacion
        def __init__(
            self,
            servicios,
            preasignaciones,
            turnos,
            servicios_by_auth,
            turnos_by_id,
            preasignacion_vigente_by_auth,
            next_servicio_row_index,
        ):
            self.servicios = servicios
            self.preasignaciones = preasignaciones
            self.turnos = turnos
            self.servicios_by_auth = servicios_by_auth
            self.turnos_by_id = turnos_by_id
            self.preasignacion_vigente_by_auth = preasignacion_vigente_by_auth
            self.next_servicio_row_index = next_servicio_row_index

    class GoogleSheetsRepository:  # pragma: no cover - stub de importacion
        pass

    google_sheets_module.DispatchSnapshot = DispatchSnapshot
    google_sheets_module.GoogleSheetsRepository = GoogleSheetsRepository
    sys.modules["services.google_sheets"] = google_sheets_module

if "services.lock_service" not in sys.modules:
    lock_service_module = ModuleType("services.lock_service")

    class SheetLockService:  # pragma: no cover - stub de importacion
        def __init__(self, repository):
            self.repository = repository

        @contextmanager
        def locked(self):
            yield

    lock_service_module.SheetLockService = SheetLockService
    sys.modules["services.lock_service"] = lock_service_module

from or_engine.models import Coordenadas, DecisionOutcome, PreasignacionPlan, ServicioPlan, TurnoPlan
from services.dispatch_service import DispatchRetryRequiredError, DispatchService
from utils.constants import (
    ANALISIS_NO_LLEGA_TIEMPO,
    ANALISIS_SIN_DISPONIBILIDAD_TURNOS,
    ESTADO_ASIGNADO_FINAL,
    ESTADO_MANUAL,
    ESTADO_PREASIGNADO,
    ESTADO_RECIBIDO,
    ESTADO_URGENTE_GESTIONAR_MANUAL,
    NOMBRE_CONDUCTOR_PENDIENTE,
    PREASIGNACION_ACTIVA,
    PREASIGNACION_CONGELADA,
    TIPO_ENRUTAMIENTO_AUTOMATICO,
    TIPO_ENRUTAMIENTO_MANUAL,
)
from utils.time_utils import now_bogota


class FakeLockService:
    """Lock vacio para pruebas unitarias."""

    @contextmanager
    def locked(self, max_attempts=None):
        yield


class BusyLockService:
    """Lock ocupado que conserva el numero de intentos solicitado."""

    def __init__(self):
        self.max_attempts = None

    @contextmanager
    def locked(self, max_attempts=None):
        self.max_attempts = max_attempts
        raise RuntimeError("Sistema ocupado, intente mas tarde")
        yield


class FakeRepository:
    """Repositorio minimo configurable para pruebas del dispatch."""

    def __init__(
        self,
        servicios_sequences,
        servicio_lookup=None,
        ensure_rechazado=False,
        freeze_on_post_accept=False,
        routing_mode=TIPO_ENRUTAMIENTO_AUTOMATICO,
        turnos=None,
        preasignaciones=None,
    ):
        self._servicios_sequences = list(servicios_sequences)
        self._list_index = 0
        self._servicio_lookup = servicio_lookup
        self._ensure_rechazado = ensure_rechazado
        self._freeze_on_post_accept = freeze_on_post_accept
        self._routing_mode = routing_mode
        self._turnos = turnos or []
        self._preasignaciones = preasignaciones or []
        self._lock_due_calls = 0
        self.append_calls = 0
        self.append_manual_calls = 0
        self.ensure_rechazado_calls = []
        self.list_servicios_calls = 0
        self.updated_services = []
        self.updated_rows = []
        self.dynamic_assignments_calls = 0
        self.dynamic_assignments_payloads = []
        self.cancelled_preasignaciones = []
        self.manual_preasignacion_updates = []
        self.manual_finalize_calls = 0
        self.manual_finalize_result = {
            "resultado": "OK",
            "asignados_final": 0,
            "urgente_gestionar_manual": 0,
            "asignados": [],
            "urgentes": [],
            "lookahead_hours": 8,
        }

    def lock_due_services(self, *args, **kwargs):
        self._lock_due_calls += 1
        if self._freeze_on_post_accept and self._lock_due_calls >= 2 and len(args) >= 2:
            servicios = args[0]
            for servicio in servicios:
                servicio.estado_operacion = ESTADO_ASIGNADO_FINAL
                servicio.estado_tecnico = ESTADO_ASIGNADO_FINAL
        return {"resultado": "OK", "cantidad": 0}

    def reconcile_terminal_services(self, servicios, preasignaciones):
        return 0

    def list_servicios(self):
        self.list_servicios_calls += 1
        if self._list_index < len(self._servicios_sequences):
            result = self._servicios_sequences[self._list_index]
            self._list_index += 1
            return result
        return self._servicios_sequences[-1] if self._servicios_sequences else []

    def find_servicio_in_snapshot(self, autorizacion, servicios):
        for servicio in servicios:
            if servicio.autorizacion == autorizacion:
                return servicio
        return None

    def build_servicio_from_payload(self, payload, row_index):
        from utils.time_utils import parse_datetime

        return ServicioPlan(
            autorizacion=payload["autorizacion"],
            caso=payload.get("caso", f"CASO-{payload['autorizacion']}"),
            ciudad_origen=payload.get("ciudad_origen", "BOGOTA"),
            ciudad_destino=payload.get("ciudad_destino", "BOGOTA"),
            direccion_origen=payload.get("direccion_origen", "Origen"),
            direccion_destino=payload.get("direccion_destino", "Destino"),
            fecha_servicio=parse_datetime(payload.get("fecha_servicio")) or (
                self._servicios_sequences[-1][0].fecha_servicio
                if self._servicios_sequences and self._servicios_sequences[-1]
                else now_bogota()
            ),
            servicio=payload.get("tipo_servicio", "CONDUCTOR ELEGIDO"),
            tipo_servicio=payload.get("modalidad_servicio", "PROGRAMADO"),
            departamento=payload.get("departamento", "CUNDINAMARCA"),
            origen=Coordenadas(4.71, -74.06),
            destino=Coordenadas(4.72, -74.05),
            estado_operacion=ESTADO_RECIBIDO,
            estado_tecnico=ESTADO_RECIBIDO,
            row_index=row_index,
        )

    def append_servicio_recibido(self, payload):
        self.append_calls += 1

    def append_servicio_manual(self, payload):
        self.append_manual_calls += 1

    def list_turnos(self):
        return self._turnos

    def list_preasignaciones(self):
        return self._preasignaciones

    def get_routing_mode(self):
        return self._routing_mode

    def set_routing_mode(self, mode):
        self._routing_mode = mode
        return mode

    def finalize_manual_mode_preassignments(self, *args, **kwargs):
        self.manual_finalize_calls += 1
        return self.manual_finalize_result

    def apply_dynamic_assignments(self, **kwargs):
        self.dynamic_assignments_calls += 1
        self.dynamic_assignments_payloads.append(kwargs)
        assignments = kwargs.get("assignments", {})
        existing_services = kwargs.get("existing_services", {})
        sync_service_auths = kwargs.get("sync_service_auths", set())
        current_preasignaciones = kwargs.get("current_preasignaciones") or []
        for preasignacion in current_preasignaciones:
            turno = assignments.get(preasignacion.autorizacion)
            if turno:
                preasignacion.id_turno = turno.id_turno
                preasignacion.cedula_conductor = turno.cedula_conductor
                preasignacion.nombre_tecnico_preasignacion = turno.nombre_conductor
        for autorizacion in sync_service_auths:
            turno = assignments.get(autorizacion)
            servicio = existing_services.get(autorizacion)
            if turno and servicio:
                servicio.id_turno = turno.id_turno
                servicio.cedula_conductor = turno.cedula_conductor
                servicio.nombre_conductor = turno.nombre_conductor
                servicio.correos = turno.correo

    def update_servicio_estado(self, *args, **kwargs):
        self.updated_services.append({"args": args, "kwargs": kwargs})
        return True

    def update_servicio_estado_by_row(
        self,
        row_index,
        nuevo_estado,
        cedula_conductor="",
        nombre_conductor="",
        id_turno="",
        correos="",
        analisis=None,
    ):
        self.updated_rows.append(
            {
                "row_index": row_index,
                "nuevo_estado": nuevo_estado,
                "cedula_conductor": cedula_conductor,
                "nombre_conductor": nombre_conductor,
                "id_turno": id_turno,
                "correos": correos,
                "analisis": analisis,
            }
        )
        return True

    def upsert_manual_preasignacion(
        self,
        autorizacion,
        id_turno,
        cedula_conductor,
        nombre_conductor,
        estado_preasignacion=PREASIGNACION_CONGELADA,
        orden_en_ruta=1,
    ):
        self.manual_preasignacion_updates.append(
            {
                "autorizacion": autorizacion,
                "id_turno": id_turno,
                "cedula_conductor": cedula_conductor,
                "nombre_conductor": nombre_conductor,
                "estado_preasignacion": estado_preasignacion,
                "orden_en_ruta": orden_en_ruta,
            }
        )
        return True

    def get_servicio(self, autorizacion):
        if callable(self._servicio_lookup):
            return self._servicio_lookup(autorizacion)
        return self._servicio_lookup

    def ensure_servicio_rechazado(self, payload, razon, skip_lookup=False, analisis=None):
        self.ensure_rechazado_calls.append(
            {
                "payload": payload,
                "razon": razon,
                "skip_lookup": skip_lookup,
                "analisis": analisis,
            }
        )
        return self._ensure_rechazado

    def classify_rejection_analysis(self, payload=None, servicio=None, razon="", analysis_code=""):
        if analysis_code:
            return analysis_code
        if "No existe turno compatible" in str(razon):
            return ANALISIS_SIN_DISPONIBILIDAD_TURNOS
        return "OTRO RECHAZO"

    def cancelar_preasignacion(self, autorizacion):
        self.cancelled_preasignaciones.append(autorizacion)

    def push_request_deadline(self, deadline_at):
        return object()

    def pop_request_deadline(self, token):
        return None


class FakeEngine:
    """Motor OR controlado para pruebas."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def decidir(self, *args, **kwargs):
        self.calls += 1
        return self.outcome


class FakeRecoveryEngine:
    """Motor minimo para validar recuperacion de factibilidad sin OR-Tools."""

    def _select_current_assignments(self, preasignaciones):
        return {preasignacion.autorizacion: preasignacion for preasignacion in preasignaciones}

    def _is_historical_service(self, servicio, now_value):
        return False

    def _is_locked(self, servicio, preasignacion, now_value):
        return False

    def _build_manual_blocks_by_turn(self, manual_services, turnos, normal_services_by_turn):
        return {}

    def _solve_dynamic_assignment(
        self,
        dynamic_services,
        locked_by_turn,
        turnos,
        current_turn_by_auth,
        now_value,
        manual_blocks_by_turn=None,
    ):
        if any(servicio.autorizacion.startswith("AUTH-BREAK") for servicio in dynamic_services):
            return None
        return object()


class FakeTurnRemovalRecoveryEngine(FakeRecoveryEngine):
    """Motor controlado para rescate de servicios con turno eliminado."""

    def __init__(self, unfit_auths=None):
        self.unfit_auths = set(unfit_auths or [])

    def _is_locked(self, servicio, preasignacion, now_value):
        return servicio.estado_operacion == ESTADO_ASIGNADO_FINAL

    def _solve_dynamic_assignment(
        self,
        dynamic_services,
        locked_by_turn,
        turnos,
        current_turn_by_auth,
        now_value,
        manual_blocks_by_turn=None,
    ):
        if not dynamic_services:
            return SimpleNamespace(assignments={})
        if not turnos:
            return None
        if any(servicio.autorizacion in self.unfit_auths for servicio in dynamic_services):
            return None
        turno = turnos[0]
        return SimpleNamespace(
            assignments={servicio.autorizacion: turno for servicio in dynamic_services}
        )


class FakeLockedBreakerRecoveryEngine(FakeRecoveryEngine):
    """Motor que solo es infactible por un servicio bloqueado/final."""

    def _is_locked(self, servicio, preasignacion, now_value):
        return servicio.estado_operacion == ESTADO_ASIGNADO_FINAL

    def _solve_dynamic_assignment(
        self,
        dynamic_services,
        locked_by_turn,
        turnos,
        current_turn_by_auth,
        now_value,
        manual_blocks_by_turn=None,
    ):
        locked_auths = {
            servicio.autorizacion
            for servicios_turno in locked_by_turn.values()
            for servicio in servicios_turno
        }
        if "AUTH-FINAL-BREAK" in locked_auths:
            return None
        return SimpleNamespace(assignments={})


class FakeMoveRecoveryEngine(FakeRecoveryEngine):
    """Motor que reubica servicios dinamicos al ultimo turno disponible."""

    def _solve_dynamic_assignment(
        self,
        dynamic_services,
        locked_by_turn,
        turnos,
        current_turn_by_auth,
        now_value,
        manual_blocks_by_turn=None,
    ):
        if not dynamic_services:
            return SimpleNamespace(assignments={})
        turno = turnos[-1]
        return SimpleNamespace(
            assignments={servicio.autorizacion: turno for servicio in dynamic_services}
        )


class FakeNotificationService:
    def __init__(self):
        self.rejections = []
        self.critical_feasibility = []

    def notify_rejection(self, autorizacion, razon, request_id="-"):
        self.rejections.append(
            {
                "autorizacion": autorizacion,
                "razon": razon,
                "request_id": request_id,
            }
        )

    def notify_critical_feasibility(self, text, request_id="-"):
        self.critical_feasibility.append(
            {
                "text": text,
                "request_id": request_id,
            }
        )


class DispatchServiceTest(unittest.TestCase):
    """Valida reintentos e idempotencia del dispatch."""

    def setUp(self):
        self.now = now_bogota()

    def make_service(self, auth: str, minutes_from_now: int, row_index: int = 2):
        return ServicioPlan(
            autorizacion=auth,
            caso=f"CASO-{auth}",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=self.now + timedelta(minutes=minutes_from_now),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.71, -74.06),
            destino=Coordenadas(4.72, -74.05),
            estado_operacion=ESTADO_RECIBIDO,
            estado_tecnico=ESTADO_RECIBIDO,
            row_index=row_index,
        )

    def make_turno(self):
        return TurnoPlan(
            id_turno="101",
            cedula_conductor="8001001",
            nombre_conductor="CARLOS CORDOBA",
            celular_tecnico="3001000001",
            proveedor="MYS",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.70, -74.05),
            fecha_inicio_turno=self.now,
            fecha_fin_turno=self.now + timedelta(hours=12),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="carlos@example.com",
        )

    def build_service(self):
        service = DispatchService()
        service.lock_service = FakeLockService()
        service.notification_service = FakeNotificationService()
        return service

    def make_snapshot(self, servicios, preasignaciones, turnos):
        return sys.modules["services.google_sheets"].DispatchSnapshot(
            servicios=servicios,
            preasignaciones=preasignaciones,
            turnos=turnos,
            servicios_by_auth={servicio.autorizacion: servicio for servicio in servicios},
            turnos_by_id={turno.id_turno: turno for turno in turnos},
            preasignacion_vigente_by_auth={
                preasignacion.autorizacion: preasignacion for preasignacion in preasignaciones
            },
            next_servicio_row_index=max((servicio.row_index or 1 for servicio in servicios), default=1) + 1,
        )

    def test_reuses_received_service_without_appending_duplicate(self):
        servicio = self.make_service("AUTH-1", 120)
        turno = self.make_turno()
        repo = FakeRepository(servicios_sequences=[[servicio]])

        service = self.build_service()
        service.repository = repo
        service.engine = FakeEngine(
            DecisionOutcome(
                autorizacion=servicio.autorizacion,
                decision="ACEPTAR",
                razon="ok",
                assignments={servicio.autorizacion: turno},
            )
        )

        outcome = service.procesar_nuevo_servicio({"autorizacion": servicio.autorizacion}, "req-1")

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.id_turno, "")
        self.assertEqual(outcome.cedula_conductor, "")
        self.assertEqual(outcome.nombre_conductor, NOMBRE_CONDUCTOR_PENDIENTE)
        self.assertEqual(repo.append_calls, 0)
        self.assertEqual(repo.dynamic_assignments_calls, 1)

    def test_returns_preassigned_data_when_freeze_is_not_run_in_create_path(self):
        servicio = self.make_service("AUTH-3", 30)
        turno = self.make_turno()
        repo = FakeRepository(servicios_sequences=[[servicio]], freeze_on_post_accept=True)

        service = self.build_service()
        service.repository = repo
        service.engine = FakeEngine(
            DecisionOutcome(
                autorizacion=servicio.autorizacion,
                decision="ACEPTAR",
                razon="ok",
                assignments={servicio.autorizacion: turno},
            )
        )

        outcome = service.procesar_nuevo_servicio({"autorizacion": servicio.autorizacion}, "req-3")

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.id_turno, "")
        self.assertEqual(outcome.cedula_conductor, "")
        self.assertEqual(outcome.nombre_conductor, NOMBRE_CONDUCTOR_PENDIENTE)

    def test_manual_mode_accepts_when_compatible_turn_exists_without_running_engine(self):
        turno = self.make_turno()
        repo = FakeRepository(
            servicios_sequences=[[]],
            routing_mode=TIPO_ENRUTAMIENTO_MANUAL,
            turnos=[turno],
        )
        engine = FakeEngine(
            DecisionOutcome(
                autorizacion="AUTH-MANUAL",
                decision="RECHAZAR",
                razon="engine should not run",
            )
        )
        service = self.build_service()
        service.repository = repo
        service.engine = engine

        outcome = service.procesar_nuevo_servicio(
            {
                "autorizacion": "AUTH-MANUAL",
                "fecha_servicio": (self.now + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
                "departamento": "CUNDINAMARCA",
                "tipo_servicio": "CONDUCTOR ELEGIDO",
                "modalidad_servicio": "PROGRAMADO",
            },
            "req-manual",
        )

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertIn("modo MANUAL", outcome.razon)
        self.assertIsNone(outcome.id_turno)
        self.assertEqual(repo.append_manual_calls, 1)
        self.assertEqual(repo.dynamic_assignments_calls, 0)
        self.assertEqual(engine.calls, 0)

    def test_manual_mode_rejects_when_no_compatible_turn_exists(self):
        repo = FakeRepository(
            servicios_sequences=[[]],
            routing_mode=TIPO_ENRUTAMIENTO_MANUAL,
            turnos=[],
        )
        notifications = FakeNotificationService()
        service = self.build_service()
        service.repository = repo
        service.notification_service = notifications
        service.engine = FakeEngine(
            DecisionOutcome(
                autorizacion="AUTH-MANUAL-NO-TURN",
                decision="ACEPTAR",
                razon="engine should not run",
            )
        )

        outcome = service.procesar_nuevo_servicio(
            {
                "autorizacion": "AUTH-MANUAL-NO-TURN",
                "fecha_servicio": (self.now + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
                "departamento": "CUNDINAMARCA",
                "tipo_servicio": "CONDUCTOR ELEGIDO",
                "modalidad_servicio": "PROGRAMADO",
            },
            "req-manual-no-turn",
        )

        self.assertEqual(outcome.decision, "RECHAZAR")
        self.assertEqual(repo.append_manual_calls, 0)
        self.assertEqual(len(repo.ensure_rechazado_calls), 1)
        self.assertEqual(
            repo.ensure_rechazado_calls[0]["analisis"],
            ANALISIS_SIN_DISPONIBILIDAD_TURNOS,
        )
        self.assertEqual(notifications.rejections[0]["autorizacion"], "AUTH-MANUAL-NO-TURN")

    def test_validar_manual_mode_finalizes_preassignments_without_recovery(self):
        repo = FakeRepository(
            servicios_sequences=[[]],
            routing_mode=TIPO_ENRUTAMIENTO_MANUAL,
        )
        repo.manual_finalize_result = {
            "resultado": "OK",
            "asignados_final": 2,
            "urgente_gestionar_manual": 1,
            "asignados": ["AUTH-1", "AUTH-2"],
            "urgentes": ["AUTH-3"],
            "lookahead_hours": 8,
        }
        notifications = FakeNotificationService()
        service = self.build_service()
        service.repository = repo
        service.notification_service = notifications
        service.engine = FakeRecoveryEngine()

        result = service.bloquear_servicios_proximos("req-validar-manual")

        self.assertEqual(result["tipo_enrutamiento"], TIPO_ENRUTAMIENTO_MANUAL)
        self.assertEqual(result["cantidad"], 2)
        self.assertTrue(result["factibilidad"]["omitida"])
        self.assertEqual(repo.manual_finalize_calls, 1)
        self.assertEqual(repo._lock_due_calls, 0)
        self.assertEqual(len(notifications.critical_feasibility), 1)
        self.assertIn("AUTH-3", notifications.critical_feasibility[0]["text"])

    def test_persists_defensive_rejection_when_acceptance_cannot_be_completed(self):
        repo = FakeRepository(servicios_sequences=[[], []], servicio_lookup=None, ensure_rechazado=False)

        service = self.build_service()
        service.repository = repo
        service.engine = FakeEngine(
            DecisionOutcome(
                autorizacion="AUTH-2",
                decision="ACEPTAR",
                razon="ok",
                assignments={},
            )
        )

        outcome = service.procesar_nuevo_servicio({"autorizacion": "AUTH-2"}, "req-2")
        self.assertEqual(outcome.decision, "RECHAZAR")

    def test_notifies_all_creation_rejections(self):
        servicio = self.make_service("AUTH-4", 120)
        repo = FakeRepository(servicios_sequences=[[servicio]])
        notifications = FakeNotificationService()

        service = self.build_service()
        service.repository = repo
        service.notification_service = notifications
        service.engine = FakeEngine(
            DecisionOutcome(
                autorizacion=servicio.autorizacion,
                decision="RECHAZAR",
                razon="No existe turno compatible para departamento, servicio y horario",
                assignments={},
            )
        )

        outcome = service.procesar_nuevo_servicio({"autorizacion": servicio.autorizacion}, "req-4")

        self.assertEqual(outcome.decision, "RECHAZAR")
        self.assertEqual(
            notifications.rejections,
            [
                {
                    "autorizacion": "AUTH-4",
                    "razon": "No existe turno compatible para departamento, servicio y horario",
                    "request_id": "req-4",
                }
            ],
        )
        self.assertEqual(
            repo.updated_services[0]["kwargs"]["analisis"],
            ANALISIS_SIN_DISPONIBILIDAD_TURNOS,
        )

    def test_persists_engine_analysis_code_on_creation_rejection(self):
        servicio = self.make_service("AUTH-NO-LLEGA", 120)
        repo = FakeRepository(servicios_sequences=[[servicio]])

        service = self.build_service()
        service.repository = repo
        service.engine = FakeEngine(
            DecisionOutcome(
                autorizacion=servicio.autorizacion,
                decision="RECHAZAR",
                razon="Servicio no alcanza a llegar a tiempo desde la ruta vigente",
                assignments={},
                analysis_code=ANALISIS_NO_LLEGA_TIEMPO,
            )
        )

        outcome = service.procesar_nuevo_servicio({"autorizacion": servicio.autorizacion}, "req-no-llega")

        self.assertEqual(outcome.decision, "RECHAZAR")
        self.assertEqual(
            repo.updated_services[0]["kwargs"]["analisis"],
            ANALISIS_NO_LLEGA_TIEMPO,
        )

    def test_rejects_and_persists_when_config_lock_is_busy(self):
        repo = FakeRepository(servicios_sequences=[], ensure_rechazado=True)
        notifications = FakeNotificationService()
        busy_lock = BusyLockService()

        service = self.build_service()
        service.repository = repo
        service.lock_service = busy_lock
        service.notification_service = notifications
        service.engine = FakeEngine(
            DecisionOutcome(
                autorizacion="AUTH-BUSY",
                decision="ACEPTAR",
                razon="should not run",
                assignments={},
            )
        )

        outcome = service.procesar_nuevo_servicio({"autorizacion": "AUTH-BUSY"}, "req-busy")

        self.assertEqual(busy_lock.max_attempts, 2)
        self.assertEqual(outcome.autorizacion, "AUTH-BUSY")
        self.assertEqual(outcome.decision, "RECHAZAR")
        self.assertIn("sistema esta ocupado", outcome.razon)
        self.assertEqual(len(repo.ensure_rechazado_calls), 1)
        self.assertEqual(repo.ensure_rechazado_calls[0]["payload"]["autorizacion"], "AUTH-BUSY")
        self.assertEqual(repo.list_servicios_calls, 0)
        self.assertEqual(service.engine.calls, 0)
        self.assertEqual(
            notifications.rejections,
            [
                {
                    "autorizacion": "AUTH-BUSY",
                    "razon": "Rechazado porque el sistema esta ocupado procesando otro servicio",
                    "request_id": "req-busy",
                }
            ],
        )

    def test_validar_marks_latest_dynamic_service_until_department_is_feasible(self):
        turno = self.make_turno()
        stable = self.make_service("AUTH-STABLE", 120, row_index=7)
        stable.estado_operacion = ESTADO_PREASIGNADO
        stable.estado_tecnico = ESTADO_PREASIGNADO
        stable.fecha_creacion_servicio = self.now - timedelta(minutes=20)
        breaker = self.make_service("AUTH-BREAK", 180, row_index=8)
        breaker.estado_operacion = ESTADO_PREASIGNADO
        breaker.estado_tecnico = ESTADO_PREASIGNADO
        breaker.fecha_creacion_servicio = self.now - timedelta(minutes=5)

        preasignaciones = [
            PreasignacionPlan(
                id_preasignacion="P1",
                autorizacion=stable.autorizacion,
                id_turno=turno.id_turno,
                cedula_conductor=turno.cedula_conductor,
                nombre_tecnico_preasignacion=turno.nombre_conductor,
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_ACTIVA,
                orden_en_ruta=1,
            ),
            PreasignacionPlan(
                id_preasignacion="P2",
                autorizacion=breaker.autorizacion,
                id_turno=turno.id_turno,
                cedula_conductor=turno.cedula_conductor,
                nombre_tecnico_preasignacion=turno.nombre_conductor,
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_ACTIVA,
                orden_en_ruta=2,
            ),
        ]
        snapshot = sys.modules["services.google_sheets"].DispatchSnapshot(
            servicios=[stable, breaker],
            preasignaciones=preasignaciones,
            turnos=[turno],
            servicios_by_auth={stable.autorizacion: stable, breaker.autorizacion: breaker},
            turnos_by_id={turno.id_turno: turno},
            preasignacion_vigente_by_auth={
                stable.autorizacion: preasignaciones[0],
                breaker.autorizacion: preasignaciones[1],
            },
            next_servicio_row_index=9,
        )

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        cundinamarca = result["departamentos"][0]
        self.assertTrue(cundinamarca["factible"])
        self.assertEqual(cundinamarca["marcados"], ["AUTH-BREAK"])
        self.assertEqual(breaker.estado_operacion, ESTADO_URGENTE_GESTIONAR_MANUAL)
        self.assertEqual(repo.updated_rows[0]["row_index"], 8)
        self.assertEqual(repo.updated_rows[0]["nuevo_estado"], ESTADO_URGENTE_GESTIONAR_MANUAL)
        self.assertEqual(repo.updated_rows[0]["id_turno"], turno.id_turno)
        self.assertEqual(repo.updated_rows[0]["nombre_conductor"], turno.nombre_conductor)
        self.assertEqual(repo.updated_rows[0]["correos"], turno.correo)
        self.assertEqual(
            repo.manual_preasignacion_updates,
            [
                {
                    "autorizacion": "AUTH-BREAK",
                    "id_turno": turno.id_turno,
                    "cedula_conductor": turno.cedula_conductor,
                    "nombre_conductor": turno.nombre_conductor,
                    "estado_preasignacion": PREASIGNACION_CONGELADA,
                    "orden_en_ruta": 1,
                }
            ],
        )

    def test_validar_recovers_departments_from_active_preassigned_and_final_services(self):
        valle_turno = self.make_turno()
        valle_turno.id_turno = "VALLE-1"
        valle_turno.departamento = "VALLE"
        valle_servicio = self.make_service("AUTH-VALLE", 120, row_index=7)
        valle_servicio.departamento = "VALLE"
        valle_servicio.estado_operacion = ESTADO_PREASIGNADO
        valle_servicio.estado_tecnico = ESTADO_PREASIGNADO
        cundinamarca_recibido = self.make_service("AUTH-RECIBIDO", 180, row_index=8)
        preasignacion = PreasignacionPlan(
            id_preasignacion="P1",
            autorizacion=valle_servicio.autorizacion,
            id_turno=valle_turno.id_turno,
            cedula_conductor=valle_turno.cedula_conductor,
            nombre_tecnico_preasignacion=valle_turno.nombre_conductor,
            fecha_preasignacion=self.now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
        )
        snapshot = self.make_snapshot(
            [valle_servicio, cundinamarca_recibido],
            [preasignacion],
            [valle_turno],
        )

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        self.assertEqual(
            [department["departamento"] for department in result["departamentos"]],
            ["VALLE"],
        )
        self.assertTrue(result["departamentos"][0]["factible"])

    def test_validar_marks_multiple_recent_services_until_department_is_feasible(self):
        turno = self.make_turno()
        stable = self.make_service("AUTH-STABLE", 120, row_index=7)
        stable.estado_operacion = ESTADO_PREASIGNADO
        stable.estado_tecnico = ESTADO_PREASIGNADO
        stable.fecha_creacion_servicio = self.now - timedelta(minutes=30)
        breaker_old = self.make_service("AUTH-BREAK-OLD", 180, row_index=8)
        breaker_old.estado_operacion = ESTADO_PREASIGNADO
        breaker_old.estado_tecnico = ESTADO_PREASIGNADO
        breaker_old.fecha_creacion_servicio = self.now - timedelta(minutes=20)
        breaker_new = self.make_service("AUTH-BREAK-NEW", 240, row_index=9)
        breaker_new.estado_operacion = ESTADO_PREASIGNADO
        breaker_new.estado_tecnico = ESTADO_PREASIGNADO
        breaker_new.fecha_creacion_servicio = self.now - timedelta(minutes=5)

        servicios = [stable, breaker_old, breaker_new]
        preasignaciones = [
            PreasignacionPlan(
                id_preasignacion=f"P{index}",
                autorizacion=servicio.autorizacion,
                id_turno=turno.id_turno,
                cedula_conductor=turno.cedula_conductor,
                nombre_tecnico_preasignacion=turno.nombre_conductor,
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_ACTIVA,
                orden_en_ruta=index,
            )
            for index, servicio in enumerate(servicios, start=1)
        ]
        snapshot = sys.modules["services.google_sheets"].DispatchSnapshot(
            servicios=servicios,
            preasignaciones=preasignaciones,
            turnos=[turno],
            servicios_by_auth={servicio.autorizacion: servicio for servicio in servicios},
            turnos_by_id={turno.id_turno: turno},
            preasignacion_vigente_by_auth={
                preasignacion.autorizacion: preasignacion for preasignacion in preasignaciones
            },
            next_servicio_row_index=10,
        )

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        cundinamarca = result["departamentos"][0]
        self.assertTrue(cundinamarca["factible"])
        self.assertEqual(cundinamarca["marcados"], ["AUTH-BREAK-NEW", "AUTH-BREAK-OLD"])
        self.assertEqual(
            [update["row_index"] for update in repo.updated_rows],
            [9, 8],
        )

    def test_validar_rescues_orphan_preassigned_and_final_services_when_turn_was_removed(self):
        turno_rescate = self.make_turno()
        preassigned = self.make_service("AUTH-ORPHAN-PRE", 120, row_index=7)
        preassigned.estado_operacion = ESTADO_PREASIGNADO
        preassigned.estado_tecnico = ESTADO_PREASIGNADO
        preassigned.fecha_creacion_servicio = self.now - timedelta(minutes=20)
        final = self.make_service("AUTH-ORPHAN-FINAL", 180, row_index=8)
        final.estado_operacion = ESTADO_ASIGNADO_FINAL
        final.estado_tecnico = ESTADO_ASIGNADO_FINAL
        final.fecha_creacion_servicio = self.now - timedelta(minutes=10)
        preasignaciones = [
            PreasignacionPlan(
                id_preasignacion="P1",
                autorizacion=preassigned.autorizacion,
                id_turno="TURN-REMOVED",
                cedula_conductor="OLD",
                nombre_tecnico_preasignacion="TECNICO RETIRADO",
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_ACTIVA,
                orden_en_ruta=1,
            ),
            PreasignacionPlan(
                id_preasignacion="P2",
                autorizacion=final.autorizacion,
                id_turno="TURN-REMOVED",
                cedula_conductor="OLD",
                nombre_tecnico_preasignacion="TECNICO RETIRADO",
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_ACTIVA,
                orden_en_ruta=2,
            ),
        ]
        snapshot = self.make_snapshot([preassigned, final], preasignaciones, [turno_rescate])

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeTurnRemovalRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        cundinamarca = result["departamentos"][0]
        self.assertTrue(cundinamarca["factible"])
        self.assertEqual(cundinamarca["marcados"], [])
        self.assertCountEqual(
            cundinamarca["rescatados"],
            ["AUTH-ORPHAN-PRE", "AUTH-ORPHAN-FINAL"],
        )
        self.assertEqual(repo.updated_rows, [])
        self.assertEqual(repo.dynamic_assignments_calls, 1)
        assignments = repo.dynamic_assignments_payloads[0]["assignments"]
        self.assertEqual(assignments["AUTH-ORPHAN-PRE"].id_turno, turno_rescate.id_turno)
        self.assertEqual(assignments["AUTH-ORPHAN-FINAL"].id_turno, turno_rescate.id_turno)
        self.assertEqual(
            repo.dynamic_assignments_payloads[0]["sync_service_auths"],
            {"AUTH-ORPHAN-PRE", "AUTH-ORPHAN-FINAL"},
        )
        self.assertEqual(final.estado_operacion, ESTADO_ASIGNADO_FINAL)
        self.assertEqual(preasignaciones[1].id_turno, turno_rescate.id_turno)
        self.assertEqual(final.id_turno, turno_rescate.id_turno)
        self.assertEqual(final.nombre_conductor, turno_rescate.nombre_conductor)
        self.assertEqual(final.cedula_conductor, turno_rescate.cedula_conductor)
        self.assertEqual(final.correos, turno_rescate.correo)

    def test_validar_rescues_orphan_service_after_missing_turn_id_was_cleared(self):
        turno_rescate = self.make_turno()
        final = self.make_service("AUTH-ORPHAN-CLEARED", 180, row_index=8)
        final.estado_operacion = ESTADO_ASIGNADO_FINAL
        final.estado_tecnico = ESTADO_ASIGNADO_FINAL
        final.id_turno = ""
        final.fecha_creacion_servicio = self.now - timedelta(minutes=10)
        preasignacion = PreasignacionPlan(
            id_preasignacion="P1",
            autorizacion=final.autorizacion,
            id_turno="",
            cedula_conductor="1015428842",
            nombre_tecnico_preasignacion="Oscar Acosta",
            fecha_preasignacion=self.now,
            estado_preasignacion=PREASIGNACION_CONGELADA,
            orden_en_ruta=1,
        )
        snapshot = self.make_snapshot([final], [preasignacion], [turno_rescate])

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeTurnRemovalRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        cundinamarca = result["departamentos"][0]
        self.assertTrue(cundinamarca["factible"])
        self.assertEqual(cundinamarca["marcados"], [])
        self.assertEqual(cundinamarca["rescatados"], ["AUTH-ORPHAN-CLEARED"])
        self.assertEqual(repo.dynamic_assignments_calls, 1)
        self.assertEqual(
            repo.dynamic_assignments_payloads[0]["sync_service_auths"],
            {"AUTH-ORPHAN-CLEARED"},
        )
        self.assertEqual(final.id_turno, turno_rescate.id_turno)
        self.assertEqual(preasignacion.id_turno, turno_rescate.id_turno)

    def test_validar_marks_orphan_final_manual_only_when_it_cannot_be_rescued(self):
        final = self.make_service("AUTH-ORPHAN-FINAL", 180, row_index=8)
        final.estado_operacion = ESTADO_ASIGNADO_FINAL
        final.estado_tecnico = ESTADO_ASIGNADO_FINAL
        final.fecha_creacion_servicio = self.now - timedelta(minutes=10)
        preasignacion = PreasignacionPlan(
            id_preasignacion="P1",
            autorizacion=final.autorizacion,
            id_turno="TURN-REMOVED",
            cedula_conductor="OLD",
            nombre_tecnico_preasignacion="TECNICO RETIRADO",
            fecha_preasignacion=self.now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
        )
        snapshot = self.make_snapshot([final], [preasignacion], [])

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeTurnRemovalRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        cundinamarca = result["departamentos"][0]
        self.assertTrue(cundinamarca["factible"])
        self.assertEqual(cundinamarca["marcados"], ["AUTH-ORPHAN-FINAL"])
        self.assertEqual(repo.dynamic_assignments_calls, 0)
        self.assertEqual(repo.updated_rows[0]["nuevo_estado"], ESTADO_URGENTE_GESTIONAR_MANUAL)
        self.assertEqual(repo.updated_rows[0]["id_turno"], "TURN-REMOVED")
        self.assertEqual(repo.manual_preasignacion_updates[0]["id_turno"], "TURN-REMOVED")
        self.assertEqual(
            repo.manual_preasignacion_updates[0]["estado_preasignacion"],
            PREASIGNACION_CONGELADA,
        )

    def test_validar_notifies_when_lock_due_services_reports_missing_turns(self):
        notifications = FakeNotificationService()
        service = self.build_service()
        service.notification_service = notifications

        service._notify_missing_turns(
            [
                {
                    "autorizacion": "6405575",
                    "id_turno": "84",
                    "departamento": "CUNDINAMARCA",
                }
            ],
            "req-missing-turn",
        )

        self.assertEqual(len(notifications.critical_feasibility), 1)
        self.assertIn("TURNO ASIGNADO NO EXISTE", notifications.critical_feasibility[0]["text"])
        self.assertIn("6405575", notifications.critical_feasibility[0]["text"])
        self.assertEqual(notifications.critical_feasibility[0]["request_id"], "req-missing-turn")

    def test_validar_rescues_remaining_orphans_and_marks_minimal_unfit_set(self):
        turno_rescate = self.make_turno()
        servicios = []
        preasignaciones = []
        for index in range(5):
            auth = f"AUTH-ORPHAN-{index}"
            servicio = self.make_service(auth, 120 + index * 20, row_index=7 + index)
            servicio.estado_operacion = ESTADO_ASIGNADO_FINAL
            servicio.estado_tecnico = ESTADO_ASIGNADO_FINAL
            servicio.fecha_creacion_servicio = self.now - timedelta(minutes=20 - index)
            servicios.append(servicio)
            preasignaciones.append(
                PreasignacionPlan(
                    id_preasignacion=f"P{index}",
                    autorizacion=auth,
                    id_turno="TURN-REMOVED",
                    cedula_conductor="OLD",
                    nombre_tecnico_preasignacion="TECNICO RETIRADO",
                    fecha_preasignacion=self.now,
                    estado_preasignacion=PREASIGNACION_ACTIVA,
                    orden_en_ruta=index + 1,
                )
            )
        snapshot = self.make_snapshot(servicios, preasignaciones, [turno_rescate])

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeTurnRemovalRecoveryEngine(
            unfit_auths={"AUTH-ORPHAN-3", "AUTH-ORPHAN-4"}
        )

        result = service._recover_department_feasibility(snapshot)

        cundinamarca = result["departamentos"][0]
        self.assertTrue(cundinamarca["factible"])
        self.assertCountEqual(cundinamarca["marcados"], ["AUTH-ORPHAN-3", "AUTH-ORPHAN-4"])
        self.assertEqual(repo.dynamic_assignments_calls, 1)
        assignments = repo.dynamic_assignments_payloads[0]["assignments"]
        self.assertCountEqual(
            assignments.keys(),
            ["AUTH-ORPHAN-0", "AUTH-ORPHAN-1", "AUTH-ORPHAN-2"],
        )
        self.assertEqual(
            repo.dynamic_assignments_payloads[0]["sync_service_auths"],
            {"AUTH-ORPHAN-0", "AUTH-ORPHAN-1", "AUTH-ORPHAN-2"},
        )
        self.assertEqual(
            [row["nuevo_estado"] for row in repo.updated_rows],
            [ESTADO_URGENTE_GESTIONAR_MANUAL, ESTADO_URGENTE_GESTIONAR_MANUAL],
        )
        self.assertCountEqual(
            [row["autorizacion"] for row in repo.manual_preasignacion_updates],
            ["AUTH-ORPHAN-3", "AUTH-ORPHAN-4"],
        )

    def test_validar_can_mark_locked_final_service_when_it_breaks_feasibility(self):
        turno = self.make_turno()
        final = self.make_service("AUTH-FINAL-BREAK", 120, row_index=7)
        final.estado_operacion = ESTADO_ASIGNADO_FINAL
        final.estado_tecnico = ESTADO_ASIGNADO_FINAL
        final.fecha_creacion_servicio = self.now - timedelta(minutes=5)
        preasignacion = PreasignacionPlan(
            id_preasignacion="P1",
            autorizacion=final.autorizacion,
            id_turno=turno.id_turno,
            cedula_conductor=turno.cedula_conductor,
            nombre_tecnico_preasignacion=turno.nombre_conductor,
            fecha_preasignacion=self.now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
        )
        snapshot = self.make_snapshot([final], [preasignacion], [turno])

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeLockedBreakerRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        cundinamarca = result["departamentos"][0]
        self.assertTrue(cundinamarca["factible"])
        self.assertEqual(cundinamarca["marcados"], ["AUTH-FINAL-BREAK"])
        self.assertEqual(final.estado_operacion, ESTADO_URGENTE_GESTIONAR_MANUAL)
        self.assertEqual(repo.updated_rows[0]["nuevo_estado"], ESTADO_URGENTE_GESTIONAR_MANUAL)
        self.assertEqual(repo.manual_preasignacion_updates[0]["autorizacion"], "AUTH-FINAL-BREAK")
        self.assertEqual(
            repo.manual_preasignacion_updates[0]["estado_preasignacion"],
            PREASIGNACION_CONGELADA,
        )

    def test_validar_persists_feasible_reoptimization_when_assignments_change(self):
        turno_actual = self.make_turno()
        turno_nuevo = TurnoPlan(
            id_turno="202",
            cedula_conductor="8002002",
            nombre_conductor="LAURA LOPEZ",
            celular_tecnico="3002000002",
            proveedor="MYS",
            direccion_origen="Base 2",
            punto_inicio=Coordenadas(4.72, -74.04),
            fecha_inicio_turno=self.now,
            fecha_fin_turno=self.now + timedelta(hours=12),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="laura@example.com",
        )
        servicio = self.make_service("AUTH-MOVE", 180, row_index=7)
        servicio.estado_operacion = ESTADO_PREASIGNADO
        servicio.estado_tecnico = ESTADO_PREASIGNADO
        preasignacion = PreasignacionPlan(
            id_preasignacion="P1",
            autorizacion=servicio.autorizacion,
            id_turno=turno_actual.id_turno,
            cedula_conductor=turno_actual.cedula_conductor,
            nombre_tecnico_preasignacion=turno_actual.nombre_conductor,
            fecha_preasignacion=self.now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
        )
        snapshot = self.make_snapshot([servicio], [preasignacion], [turno_actual, turno_nuevo])

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeMoveRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        cundinamarca = result["departamentos"][0]
        self.assertTrue(cundinamarca["factible"])
        self.assertEqual(cundinamarca["marcados"], [])
        self.assertEqual(repo.dynamic_assignments_calls, 1)
        assignments = repo.dynamic_assignments_payloads[0]["assignments"]
        self.assertEqual(assignments["AUTH-MOVE"].id_turno, turno_nuevo.id_turno)
        self.assertEqual(preasignacion.id_turno, turno_nuevo.id_turno)

    def test_validar_does_not_move_final_service_when_its_turn_still_exists(self):
        turno_existente = self.make_turno()
        dynamic = self.make_service("AUTH-DYNAMIC", 180, row_index=8)
        dynamic.estado_operacion = ESTADO_PREASIGNADO
        dynamic.estado_tecnico = ESTADO_PREASIGNADO
        final = self.make_service("AUTH-FINAL-VALID", 120, row_index=7)
        final.estado_operacion = ESTADO_ASIGNADO_FINAL
        final.estado_tecnico = ESTADO_ASIGNADO_FINAL
        preasignaciones = [
            PreasignacionPlan(
                id_preasignacion="P1",
                autorizacion=final.autorizacion,
                id_turno=turno_existente.id_turno,
                cedula_conductor=turno_existente.cedula_conductor,
                nombre_tecnico_preasignacion=turno_existente.nombre_conductor,
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_ACTIVA,
                orden_en_ruta=1,
            ),
            PreasignacionPlan(
                id_preasignacion="P2",
                autorizacion=dynamic.autorizacion,
                id_turno="TURN-REMOVED",
                cedula_conductor="OLD",
                nombre_tecnico_preasignacion="TECNICO RETIRADO",
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_ACTIVA,
                orden_en_ruta=2,
            ),
        ]
        snapshot = self.make_snapshot([final, dynamic], preasignaciones, [turno_existente])

        repo = FakeRepository(servicios_sequences=[])
        service = self.build_service()
        service.repository = repo
        service.engine = FakeTurnRemovalRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        self.assertTrue(result["departamentos"][0]["factible"])
        assignments = repo.dynamic_assignments_payloads[0]["assignments"]
        self.assertIn("AUTH-DYNAMIC", assignments)
        self.assertNotIn("AUTH-FINAL-VALID", assignments)
        self.assertEqual(preasignaciones[0].id_turno, turno_existente.id_turno)

    def test_validar_aborts_recovery_when_minimal_set_exceeds_safety_limit(self):
        turno = self.make_turno()
        stable = self.make_service("AUTH-STABLE", 120, row_index=7)
        stable.estado_operacion = ESTADO_PREASIGNADO
        stable.estado_tecnico = ESTADO_PREASIGNADO
        stable.fecha_creacion_servicio = self.now - timedelta(minutes=60)

        breakers = []
        for index in range(5):
            breaker = self.make_service(f"AUTH-BREAK-{index}", 180 + index * 20, row_index=8 + index)
            breaker.estado_operacion = ESTADO_PREASIGNADO
            breaker.estado_tecnico = ESTADO_PREASIGNADO
            breaker.fecha_creacion_servicio = self.now - timedelta(minutes=5 + index)
            breakers.append(breaker)

        servicios = [stable, *breakers]
        preasignaciones = [
            PreasignacionPlan(
                id_preasignacion=f"P{index}",
                autorizacion=servicio.autorizacion,
                id_turno=turno.id_turno,
                cedula_conductor=turno.cedula_conductor,
                nombre_tecnico_preasignacion=turno.nombre_conductor,
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_ACTIVA,
                orden_en_ruta=index,
            )
            for index, servicio in enumerate(servicios, start=1)
        ]
        snapshot = sys.modules["services.google_sheets"].DispatchSnapshot(
            servicios=servicios,
            preasignaciones=preasignaciones,
            turnos=[turno],
            servicios_by_auth={servicio.autorizacion: servicio for servicio in servicios},
            turnos_by_id={turno.id_turno: turno},
            preasignacion_vigente_by_auth={
                preasignacion.autorizacion: preasignacion for preasignacion in preasignaciones
            },
            next_servicio_row_index=14,
        )

        repo = FakeRepository(servicios_sequences=[])
        notifications = FakeNotificationService()
        service = self.build_service()
        service.repository = repo
        service.notification_service = notifications
        service.engine = FakeRecoveryEngine()

        result = service._recover_department_feasibility(snapshot)

        cundinamarca = result["departamentos"][0]
        self.assertFalse(cundinamarca["factible"])
        self.assertTrue(cundinamarca["recovery_aborted"])
        self.assertEqual(cundinamarca["marcados"], [])
        self.assertEqual(repo.updated_rows, [])
        self.assertEqual(len(notifications.critical_feasibility), 1)
        self.assertIn("URGENTE: SISTEMA RVE INFACIBLE", notifications.critical_feasibility[0]["text"])

    def test_validar_aborts_recovery_when_deadline_budget_is_exhausted(self):
        turno = self.make_turno()
        breaker = self.make_service("AUTH-BREAK", 180, row_index=8)
        breaker.estado_operacion = ESTADO_PREASIGNADO
        breaker.estado_tecnico = ESTADO_PREASIGNADO
        breaker.fecha_creacion_servicio = self.now - timedelta(minutes=5)

        preasignacion = PreasignacionPlan(
            id_preasignacion="P1",
            autorizacion=breaker.autorizacion,
            id_turno=turno.id_turno,
            cedula_conductor=turno.cedula_conductor,
            nombre_tecnico_preasignacion=turno.nombre_conductor,
            fecha_preasignacion=self.now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
        )
        snapshot = sys.modules["services.google_sheets"].DispatchSnapshot(
            servicios=[breaker],
            preasignaciones=[preasignacion],
            turnos=[turno],
            servicios_by_auth={breaker.autorizacion: breaker},
            turnos_by_id={turno.id_turno: turno},
            preasignacion_vigente_by_auth={preasignacion.autorizacion: preasignacion},
            next_servicio_row_index=9,
        )

        repo = FakeRepository(servicios_sequences=[])
        notifications = FakeNotificationService()
        service = self.build_service()
        service.repository = repo
        service.notification_service = notifications
        service.engine = FakeRecoveryEngine()

        result = service._recover_department_feasibility(
            snapshot,
            deadline_at=time.monotonic() + 1.0,
        )

        cundinamarca = result["departamentos"][0]
        self.assertFalse(cundinamarca["factible"])
        self.assertTrue(cundinamarca["recovery_aborted"])
        self.assertEqual(cundinamarca["evaluated_sets"], 0)
        self.assertEqual(repo.updated_rows, [])
        self.assertEqual(len(notifications.critical_feasibility), 1)


if __name__ == "__main__":
    unittest.main()
