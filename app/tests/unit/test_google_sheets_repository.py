"""Pruebas de mapeo de columnas en el repositorio de Sheets."""

from datetime import timedelta
import os
import sys
import unittest
from types import SimpleNamespace

CURRENT_DIR = os.path.dirname(__file__)
APP_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from or_engine.models import Coordenadas, PreasignacionPlan, ServicioPlan, TurnoPlan
try:
    from services.google_sheets import GoogleSheetsRepository
except ImportError:
    class GoogleSheetsRepository:  # pragma: no cover - dependencia externa ausente
        pass
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
    ESTADO_ASIGNADO_FINAL,
    ESTADO_MANUAL,
    ESTADO_PREASIGNADO,
    ESTADO_URGENTE_GESTIONAR_MANUAL,
    PREASIGNACION_ACTIVA,
)
from utils.time_utils import now_bogota


class FakeSheetsRepository(GoogleSheetsRepository):
    def __init__(self):
        self.settings = SimpleNamespace(
            sheet_servicios="SERVICIOS",
            sheet_preasignaciones="PREASIGNACIONES",
            min_notice_minutes=60,
        )
        self.batch_updates = []
        self.turnos_all = []

    def worksheet(self, name):
        return SimpleNamespace(title=name)

    def _header_map(self, worksheet):
        return {
            "ESTADO_DEL_SERVICIO_OPERACION": 1,
            "ESTADO_DEL_SERVICIO_TECNICO": 2,
            "ID_TURNO": 3,
            "CEDULA_CONDUCTOR": 4,
            "NOMBRE_CONDUCTOR": 5,
            "CORREOS": 6,
        }

    def _batch_update_rows(self, worksheet, header_map, updates_by_row):
        self.batch_updates.append((worksheet.title, updates_by_row))

    def _call_with_retry(self, func, *args, **kwargs):
        return func(*args, **kwargs)

    def _invalidate_sheet_rows_cache(self, sheet_name):
        return None

    def list_turnos(self, include_expired=False):
        if include_expired:
            return self.turnos_all
        now = now_bogota()
        return [
            turno
            for turno in self.turnos_all
            if turno.fecha_fin_turno and turno.fecha_fin_turno >= now
        ]


@unittest.skipIf(
    not hasattr(GoogleSheetsRepository, "_build_servicio_row"),
    "GoogleSheetsRepository real no disponible en esta corrida de tests",
)
class GoogleSheetsRepositoryTest(unittest.TestCase):
    def test_build_servicio_row_includes_correos(self):
        repo = FakeSheetsRepository()
        row = repo._build_servicio_row(
            headers=["AUTORIZACION", "CORREOS", "ESTADO_DEL_SERVICIO_OPERACION"],
            payload={"autorizacion": "AUTH-1", "correos": "cliente@example.com"},
            estado_operacion="RECIBIDO",
            estado_tecnico="RECIBIDO",
            id_turno="",
            cedula_conductor="",
            nombre_conductor="",
        )

        self.assertEqual(row, ["AUTH-1", "cliente@example.com", "RECIBIDO"])

    def test_build_servicio_row_writes_analisis_to_fixed_ah_without_header(self):
        repo = FakeSheetsRepository()
        row = repo._build_servicio_row(
            headers=["AUTORIZACION", "CORREOS", "ESTADO_DEL_SERVICIO_OPERACION"],
            payload={"autorizacion": "AUTH-1", "correos": "cliente@example.com"},
            estado_operacion="RECHAZADO_RVE",
            estado_tecnico="RECHAZADO_RVE",
            id_turno="",
            cedula_conductor="",
            nombre_conductor="",
            analisis=ANALISIS_SIN_DISPONIBILIDAD_TURNOS,
        )

        self.assertEqual(len(row), 34)
        self.assertEqual(row[33], ANALISIS_SIN_DISPONIBILIDAD_TURNOS)

    def test_classify_rejection_analysis_prioritizes_invalid_coordinates(self):
        repo = FakeSheetsRepository()
        payload = {
            "fecha_servicio": (now_bogota() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "lat_origen": "0",
            "lng_origen": "-74.05",
            "lat_destino": "4.8",
            "lng_destino": "-74.06",
        }

        result = repo.classify_rejection_analysis(
            payload=payload,
            razon="No existe turno compatible para departamento, servicio y horario",
        )

        self.assertEqual(result, ANALISIS_DATOS_ERRONEOS)

    def test_classify_rejection_analysis_detects_emergency(self):
        repo = FakeSheetsRepository()
        payload = {
            "fecha_servicio": (now_bogota() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            "lat_origen": "4.7",
            "lng_origen": "-74.05",
            "lat_destino": "4.8",
            "lng_destino": "-74.06",
        }

        result = repo.classify_rejection_analysis(payload=payload, razon="otra razon")

        self.assertEqual(result, ANALISIS_SERVICIO_EMERGENCIA)

    def test_classify_rejection_analysis_detects_no_turn_availability(self):
        repo = FakeSheetsRepository()
        payload = {
            "fecha_servicio": (now_bogota() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "lat_origen": "4.7",
            "lng_origen": "-74.05",
            "lat_destino": "4.8",
            "lng_destino": "-74.06",
        }

        result = repo.classify_rejection_analysis(
            payload=payload,
            razon="No existe turno compatible para departamento, servicio y horario",
        )

        self.assertEqual(result, ANALISIS_SIN_DISPONIBILIDAD_TURNOS)

    def test_classify_rejection_analysis_detects_service_outside_turn_limits(self):
        repo = FakeSheetsRepository()
        payload = {
            "fecha_servicio": (now_bogota() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "lat_origen": "4.7",
            "lng_origen": "-74.05",
            "lat_destino": "4.8",
            "lng_destino": "-74.06",
        }

        result = repo.classify_rejection_analysis(
            payload=payload,
            razon="Servicio fuera de los limites del turno",
        )

        self.assertEqual(result, ANALISIS_SERVICIO_FUERA_LIMITES_TURNO)

    def test_classify_rejection_analysis_detects_max_radius(self):
        repo = FakeSheetsRepository()
        payload = {
            "fecha_servicio": (now_bogota() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "lat_origen": "4.7",
            "lng_origen": "-74.05",
            "lat_destino": "4.8",
            "lng_destino": "-74.06",
        }

        result = repo.classify_rejection_analysis(
            payload=payload,
            razon="Servicio fuera del radio maximo de 25 km",
        )

        self.assertEqual(result, ANALISIS_FUERA_RADIO_MAXIMO)

    def test_classify_rejection_analysis_uses_engine_analysis_code(self):
        repo = FakeSheetsRepository()
        payload = {
            "fecha_servicio": (now_bogota() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "lat_origen": "4.7",
            "lng_origen": "-74.05",
            "lat_destino": "4.8",
            "lng_destino": "-74.06",
        }

        result = repo.classify_rejection_analysis(
            payload=payload,
            razon="No existe insercion factible por solapamiento",
            analysis_code=ANALISIS_NO_LLEGA_TIEMPO,
        )

        self.assertEqual(result, ANALISIS_NO_LLEGA_TIEMPO)

    def test_classify_rejection_analysis_detects_insertion_not_feasible(self):
        repo = FakeSheetsRepository()
        payload = {
            "fecha_servicio": (now_bogota() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "lat_origen": "4.7",
            "lng_origen": "-74.05",
            "lat_destino": "4.8",
            "lng_destino": "-74.06",
        }

        result = repo.classify_rejection_analysis(
            payload=payload,
            razon="No existe insercion factible por solapamiento o preservacion de compromisos vigentes",
        )

        self.assertEqual(result, ANALISIS_INSERCION_NO_FACTIBLE)

    def test_classify_rejection_analysis_accepts_saturation_code(self):
        repo = FakeSheetsRepository()
        payload = {
            "fecha_servicio": (now_bogota() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "lat_origen": "4.7",
            "lng_origen": "-74.05",
            "lat_destino": "4.8",
            "lng_destino": "-74.06",
        }

        result = repo.classify_rejection_analysis(
            payload=payload,
            razon="Horario saturado",
            analysis_code=ANALISIS_SATURACION_TURNO,
        )

        self.assertEqual(result, ANALISIS_SATURACION_TURNO)

    def test_classify_rejection_analysis_fallback(self):
        repo = FakeSheetsRepository()
        payload = {
            "fecha_servicio": (now_bogota() + timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S"),
            "lat_origen": "4.7",
            "lng_origen": "-74.05",
            "lat_destino": "4.8",
            "lng_destino": "-74.06",
        }

        result = repo.classify_rejection_analysis(payload=payload, razon="solapamiento")

        self.assertEqual(result, ANALISIS_OTRO_RECHAZO)

    def test_lock_due_services_writes_turno_correo_to_servicio(self):
        repo = FakeSheetsRepository()
        now = now_bogota()
        servicio = ServicioPlan(
            autorizacion="AUTH-1",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now + timedelta(minutes=30),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_PREASIGNADO,
            row_index=2,
        )
        preasignacion = PreasignacionPlan(
            id_preasignacion="1",
            autorizacion="AUTH-1",
            id_turno="10",
            cedula_conductor="123",
            nombre_tecnico_preasignacion="Tecnico Uno",
            fecha_preasignacion=now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )
        turno = TurnoPlan(
            id_turno="10",
            cedula_conductor="123",
            nombre_conductor="Tecnico Uno",
            celular_tecnico="300",
            proveedor="Proveedor",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.7, -74.05),
            fecha_inicio_turno=now,
            fecha_fin_turno=now + timedelta(hours=8),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="tecnico@example.com",
        )

        repo.lock_due_services([servicio], [preasignacion], [turno])

        servicio_updates = [item for item in repo.batch_updates if item[0] == "SERVICIOS"][0][1]
        self.assertEqual(servicio_updates[2]["CORREOS"], "tecnico@example.com")
        self.assertEqual(servicio.correos, "tecnico@example.com")

    def test_lock_due_services_prefers_turno_with_correo_when_duplicate_turn_id_exists(self):
        repo = FakeSheetsRepository()
        now = now_bogota()
        servicio = ServicioPlan(
            autorizacion="AUTH-1",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now + timedelta(minutes=30),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_PREASIGNADO,
            row_index=2,
        )
        preasignacion = PreasignacionPlan(
            id_preasignacion="1",
            autorizacion="AUTH-1",
            id_turno="10",
            cedula_conductor="123",
            nombre_tecnico_preasignacion="Tecnico Uno",
            fecha_preasignacion=now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )
        turno_sin_correo = TurnoPlan(
            id_turno="10",
            cedula_conductor="123",
            nombre_conductor="Tecnico Uno",
            celular_tecnico="300",
            proveedor="Proveedor",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.7, -74.05),
            fecha_inicio_turno=now,
            fecha_fin_turno=now + timedelta(hours=8),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="",
        )
        turno_con_correo = TurnoPlan(
            id_turno="10",
            cedula_conductor="123",
            nombre_conductor="Tecnico Uno",
            celular_tecnico="300",
            proveedor="Proveedor",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.7, -74.05),
            fecha_inicio_turno=now,
            fecha_fin_turno=now + timedelta(hours=8),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="tecnico@example.com",
        )

        repo.lock_due_services([servicio], [preasignacion], [turno_sin_correo, turno_con_correo])

        servicio_updates = [item for item in repo.batch_updates if item[0] == "SERVICIOS"][0][1]
        self.assertEqual(servicio_updates[2]["CORREOS"], "tecnico@example.com")
        self.assertEqual(servicio.correos, "tecnico@example.com")

    def test_lock_due_services_repairs_missing_correo_for_final_service(self):
        repo = FakeSheetsRepository()
        now = now_bogota()
        servicio = ServicioPlan(
            autorizacion="AUTH-1",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now - timedelta(minutes=5),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_ASIGNADO_FINAL,
            estado_tecnico=ESTADO_ASIGNADO_FINAL,
            id_turno="10",
            cedula_conductor="123",
            nombre_conductor="Tecnico Uno",
            correos="",
            row_index=2,
        )
        preasignacion = PreasignacionPlan(
            id_preasignacion="1",
            autorizacion="AUTH-1",
            id_turno="10",
            cedula_conductor="123",
            nombre_tecnico_preasignacion="Tecnico Uno",
            fecha_preasignacion=now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )
        turno = TurnoPlan(
            id_turno="10",
            cedula_conductor="123",
            nombre_conductor="Tecnico Uno",
            celular_tecnico="300",
            proveedor="Proveedor",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.7, -74.05),
            fecha_inicio_turno=now - timedelta(hours=1),
            fecha_fin_turno=now + timedelta(hours=1),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="tecnico@example.com",
        )

        repo.lock_due_services([servicio], [preasignacion], [turno])

        servicio_updates = [item for item in repo.batch_updates if item[0] == "SERVICIOS"][0][1]
        self.assertEqual(servicio_updates[2]["CORREOS"], "tecnico@example.com")
        self.assertEqual(servicio.correos, "tecnico@example.com")

    def test_lock_due_services_uses_turno_identity_when_preassignment_is_stale(self):
        repo = FakeSheetsRepository()
        now = now_bogota()
        servicio = ServicioPlan(
            autorizacion="AUTH-1",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now - timedelta(minutes=5),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_ASIGNADO_FINAL,
            estado_tecnico=ESTADO_ASIGNADO_FINAL,
            id_turno="26",
            cedula_conductor="1023939548",
            nombre_conductor="Johan wilches",
            correos="",
            row_index=2,
        )
        preasignacion = PreasignacionPlan(
            id_preasignacion="1",
            autorizacion="AUTH-1",
            id_turno="26",
            cedula_conductor="1023939548",
            nombre_tecnico_preasignacion="Johan wilches",
            fecha_preasignacion=now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )
        turno = TurnoPlan(
            id_turno="26",
            cedula_conductor="1019136586",
            nombre_conductor="Fabián Andrés Ovalle reyes",
            celular_tecnico="300",
            proveedor="Proveedor",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.7, -74.05),
            fecha_inicio_turno=now - timedelta(hours=1),
            fecha_fin_turno=now + timedelta(hours=1),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="fabianmadara8@gmail.com",
        )

        repo.lock_due_services([servicio], [preasignacion], [turno])

        servicio_updates = [item for item in repo.batch_updates if item[0] == "SERVICIOS"][0][1]
        self.assertEqual(servicio_updates[2]["ID_TURNO"], "26")
        self.assertEqual(servicio_updates[2]["CEDULA_CONDUCTOR"], "1019136586")
        self.assertEqual(servicio_updates[2]["NOMBRE_CONDUCTOR"], "Fabián Andrés Ovalle reyes")
        self.assertEqual(servicio_updates[2]["CORREOS"], "fabianmadara8@gmail.com")

        preasignacion_updates = [
            item for item in repo.batch_updates if item[0] == "PREASIGNACIONES"
        ][0][1]
        self.assertEqual(preasignacion_updates[2]["CEDULA_CONDUCTOR"], "1019136586")
        self.assertEqual(
            preasignacion_updates[2]["NOMBRE_TECNICO_PREASIGNACION"],
            "Fabián Andrés Ovalle reyes",
        )

    def test_lock_due_services_clears_turn_id_when_exact_turn_does_not_exist(self):
        repo = FakeSheetsRepository()
        now = now_bogota()
        servicio = ServicioPlan(
            autorizacion="AUTH-1",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="CHIA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now - timedelta(minutes=5),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_ASIGNADO_FINAL,
            estado_tecnico=ESTADO_ASIGNADO_FINAL,
            id_turno="84",
            cedula_conductor="1015428842",
            nombre_conductor="Oscar Acosta",
            correos="",
            row_index=2,
        )
        preasignacion = PreasignacionPlan(
            id_preasignacion="100",
            autorizacion="AUTH-1",
            id_turno="84",
            cedula_conductor="1015428842",
            nombre_tecnico_preasignacion="Oscar Acosta",
            fecha_preasignacion=now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )
        turno_mismo_tecnico_otro_dia = TurnoPlan(
            id_turno="53",
            cedula_conductor="1015428842",
            nombre_conductor="Oscar Acosta",
            celular_tecnico="300",
            proveedor="Proveedor",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.7, -74.05),
            fecha_inicio_turno=now + timedelta(days=1),
            fecha_fin_turno=now + timedelta(days=1, hours=8),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="oscacosta.9@gmail.com",
        )

        result = repo.lock_due_services(
            [servicio],
            [preasignacion],
            [turno_mismo_tecnico_otro_dia],
        )

        servicio_updates = [item for item in repo.batch_updates if item[0] == "SERVICIOS"][0][1]
        self.assertEqual(servicio_updates[2]["ID_TURNO"], "")
        self.assertEqual(servicio.id_turno, "")

        preasignacion_updates = [
            item for item in repo.batch_updates if item[0] == "PREASIGNACIONES"
        ][0][1]
        self.assertEqual(preasignacion_updates[2]["ID_TURNO"], "")
        self.assertEqual(preasignacion.id_turno, "")
        self.assertEqual(
            result["turnos_no_encontrados"],
            [
                {
                    "autorizacion": "AUTH-1",
                    "id_turno": "84",
                    "departamento": "CUNDINAMARCA",
                }
            ],
        )

    def test_lock_due_services_preserves_expired_turn_id_when_turn_exists_in_sheet(self):
        repo = FakeSheetsRepository()
        now = now_bogota()
        servicio = ServicioPlan(
            autorizacion="AUTH-EXPIRED",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now - timedelta(minutes=10),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_ASIGNADO_FINAL,
            estado_tecnico=ESTADO_ASIGNADO_FINAL,
            id_turno="81",
            cedula_conductor="1010239338",
            nombre_conductor="Cristian Cardona",
            correos="",
            row_index=2,
        )
        preasignacion = PreasignacionPlan(
            id_preasignacion="110",
            autorizacion="AUTH-EXPIRED",
            id_turno="81",
            cedula_conductor="1010239338",
            nombre_tecnico_preasignacion="Cristian Cardona",
            fecha_preasignacion=now - timedelta(hours=1),
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )
        turno_expirado = TurnoPlan(
            id_turno="81",
            cedula_conductor="1010239338",
            nombre_conductor="Cristian Cardona",
            celular_tecnico="300",
            proveedor="Proveedor",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.7, -74.05),
            fecha_inicio_turno=now - timedelta(hours=7),
            fecha_fin_turno=now - timedelta(minutes=5),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="cristian@example.com",
        )
        repo.turnos_all = [turno_expirado]

        result = repo.lock_due_services([servicio], [preasignacion], [])

        self.assertEqual(result["turnos_no_encontrados"], [])
        self.assertEqual(servicio.id_turno, "81")
        self.assertEqual(preasignacion.id_turno, "81")
        servicio_updates = [item for item in repo.batch_updates if item[0] == "SERVICIOS"][0][1]
        self.assertEqual(servicio_updates[2]["ID_TURNO"], "81")
        self.assertEqual(servicio_updates[2]["CORREOS"], "cristian@example.com")

    def test_lock_due_services_does_not_freeze_manual_services(self):
        repo = FakeSheetsRepository()
        now = now_bogota()
        servicio = ServicioPlan(
            autorizacion="AUTH-MANUAL",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now + timedelta(minutes=30),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_MANUAL,
            estado_tecnico=ESTADO_MANUAL,
            id_turno="26",
            row_index=2,
        )
        preasignacion = PreasignacionPlan(
            id_preasignacion="1",
            autorizacion="AUTH-MANUAL",
            id_turno="26",
            cedula_conductor="1019136586",
            nombre_tecnico_preasignacion="Fabián Andrés Ovalle reyes",
            fecha_preasignacion=now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )

        result = repo.lock_due_services([servicio], [preasignacion], [])

        self.assertEqual(result["cantidad"], 0)
        self.assertEqual(repo.batch_updates, [])

    def test_finalize_manual_mode_preassignments_assigns_and_marks_missing_preassignment_urgent(self):
        repo = FakeSheetsRepository()
        now = now_bogota()
        assigned = ServicioPlan(
            autorizacion="AUTH-ASSIGNED",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now + timedelta(hours=2),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_PREASIGNADO,
            row_index=2,
        )
        missing = ServicioPlan(
            autorizacion="AUTH-MISSING",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now + timedelta(hours=3),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_PREASIGNADO,
            row_index=3,
        )
        future = ServicioPlan(
            autorizacion="AUTH-FUTURE",
            caso="CASO",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=now + timedelta(hours=10),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.7, -74.05),
            destino=Coordenadas(4.8, -74.05),
            estado_operacion=ESTADO_PREASIGNADO,
            row_index=4,
        )
        preasignacion = PreasignacionPlan(
            id_preasignacion="1",
            autorizacion=assigned.autorizacion,
            id_turno="10",
            cedula_conductor="123",
            nombre_tecnico_preasignacion="Tecnico Uno",
            fecha_preasignacion=now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )
        turno = TurnoPlan(
            id_turno="10",
            cedula_conductor="123",
            nombre_conductor="Tecnico Uno",
            celular_tecnico="300",
            proveedor="Proveedor",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.7, -74.05),
            fecha_inicio_turno=now,
            fecha_fin_turno=now + timedelta(hours=8),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            correo="tecnico@example.com",
        )

        result = repo.finalize_manual_mode_preassignments(
            [assigned, missing, future],
            [preasignacion],
            [turno],
        )

        servicios_updates = [item for item in repo.batch_updates if item[0] == "SERVICIOS"][0][1]
        self.assertEqual(
            servicios_updates[2]["ESTADO_DEL_SERVICIO_OPERACION"],
            ESTADO_ASIGNADO_FINAL,
        )
        self.assertEqual(servicios_updates[2]["ID_TURNO"], "10")
        self.assertEqual(
            servicios_updates[3]["ESTADO_DEL_SERVICIO_OPERACION"],
            ESTADO_URGENTE_GESTIONAR_MANUAL,
        )
        self.assertNotIn(4, servicios_updates)

        preasignaciones_updates = [
            item for item in repo.batch_updates if item[0] == "PREASIGNACIONES"
        ][0][1]
        self.assertEqual(preasignaciones_updates[2]["ESTADO_PREASIGNACION"], "CONGELADA")
        self.assertEqual(result["asignados"], ["AUTH-ASSIGNED"])
        self.assertEqual(result["urgentes"], ["AUTH-MISSING"])


if __name__ == "__main__":
    unittest.main()
