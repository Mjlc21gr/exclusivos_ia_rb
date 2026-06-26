"""Pruebas unitarias del motor OR-Tools."""

import importlib.util
import os
import sys
import unittest
from datetime import timedelta

CURRENT_DIR = os.path.dirname(__file__)
APP_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from or_engine.models import Coordenadas, PreasignacionPlan, ServicioPlan, TurnoPlan
from utils.constants import (
    ESTADO_ASIGNADO_FINAL,
    ESTADO_MANUAL,
    ESTADO_PREASIGNADO,
    ESTADO_URGENTE_GESTIONAR_MANUAL,
    PREASIGNACION_ACTIVA,
    PREASIGNACION_CONGELADA,
)
from utils.time_utils import now_bogota


@unittest.skipUnless(importlib.util.find_spec("ortools"), "ortools no esta instalado")
class RoutingDecisionEngineTest(unittest.TestCase):
    """Cobertura basica de la interfaz del motor OR-Tools."""

    def setUp(self) -> None:
        self.previous_pre_shift = os.environ.pop("PRE_SHIFT_TRAVEL_MINUTES", None)
        self.previous_min_notice = os.environ.get("MIN_NOTICE_MINUTES")
        os.environ["MIN_NOTICE_MINUTES"] = "60"
        from utils.config import get_settings

        get_settings.cache_clear()
        from or_engine.routing_engine import RoutingDecisionEngine

        self.engine = RoutingDecisionEngine()
        self.now = now_bogota()
        self.turnos = [
            TurnoPlan(
                id_turno="1",
                cedula_conductor="100",
                nombre_conductor="Tecnico 1",
                celular_tecnico="3000000000",
                proveedor="MYS",
                direccion_origen="Base",
                punto_inicio=Coordenadas(4.70, -74.05),
                fecha_inicio_turno=self.now,
                fecha_fin_turno=self.now + timedelta(hours=8),
                servicio="CONDUCTOR ELEGIDO",
                tipo_servicio="PROGRAMADO",
                departamento="CUNDINAMARCA",
            ),
            TurnoPlan(
                id_turno="2",
                cedula_conductor="200",
                nombre_conductor="Tecnico 2",
                celular_tecnico="3000000001",
                proveedor="MYS",
                direccion_origen="Base Norte",
                punto_inicio=Coordenadas(4.76, -74.03),
                fecha_inicio_turno=self.now,
                fecha_fin_turno=self.now + timedelta(hours=8),
                servicio="CONDUCTOR ELEGIDO",
                tipo_servicio="PROGRAMADO",
                departamento="CUNDINAMARCA",
            ),
        ]

    def tearDown(self) -> None:
        from utils.config import get_settings

        if self.previous_pre_shift is not None:
            os.environ["PRE_SHIFT_TRAVEL_MINUTES"] = self.previous_pre_shift
        if self.previous_min_notice is None:
            os.environ.pop("MIN_NOTICE_MINUTES", None)
        else:
            os.environ["MIN_NOTICE_MINUTES"] = self.previous_min_notice
        get_settings.cache_clear()

    def make_service(self, auth: str, minutes_from_now: int, lat: float, lng: float):
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
            origen=Coordenadas(lat, lng),
            destino=Coordenadas(lat + 0.01, lng + 0.01),
        )

    def make_antioquia_turn(self):
        return TurnoPlan(
            id_turno="ANT-1",
            cedula_conductor="300",
            nombre_conductor="Tecnico Antioquia",
            celular_tecnico="3000000002",
            proveedor="MYS",
            direccion_origen="Base Medellin",
            punto_inicio=Coordenadas(6.25, -75.58),
            fecha_inicio_turno=self.now,
            fecha_fin_turno=self.now + timedelta(hours=8),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="ANTIOQUIA",
        )

    def test_accepts_feasible_service_with_same_contract(self):
        service = self.make_service("R1", 120, 4.71, -74.06)
        outcome = self.engine.decidir(service, [], self.turnos, [])
        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertIn("R1", outcome.assignments)

    def test_department_scope_uses_normalized_values(self):
        service = self.make_service("R1-NORM", 120, 4.71, -74.06)
        service.departamento = "Cundinamarca "
        self.turnos[0].departamento = " CUNDINAMARCA"
        self.turnos[1].departamento = "cundinamarca"

        outcome = self.engine.decidir(service, [], self.turnos, [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertIn("R1-NORM", outcome.assignments)

    def test_empty_workset_is_feasible_even_without_turns(self):
        solution = self.engine._solve_dynamic_assignment(
            dynamic_services=[],
            locked_by_turn={},
            turnos=[],
            current_turn_by_auth={},
            now_value=self.now,
            manual_blocks_by_turn={},
        )

        self.assertIsNotNone(solution)
        self.assertEqual(solution.assignments, {})

    def test_dynamic_service_without_turns_remains_infeasible(self):
        service = self.make_service("R1-NO-TURNS", 120, 4.71, -74.06)

        solution = self.engine._solve_dynamic_assignment(
            dynamic_services=[service],
            locked_by_turn={},
            turnos=[],
            current_turn_by_auth={},
            now_value=self.now,
            manual_blocks_by_turn={},
        )

        self.assertIsNone(solution)

    def test_assigns_existing_dynamic_and_incoming_service(self):
        existing = self.make_service("R2", 180, 4.72, -74.07)
        existing.estado_operacion = ESTADO_PREASIGNADO
        preasignacion = PreasignacionPlan(
            id_preasignacion="1",
            autorizacion="R2",
            id_turno="1",
            cedula_conductor="100",
            nombre_tecnico_preasignacion="Tecnico 1",
            fecha_preasignacion=self.now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )
        incoming = self.make_service("R3", 240, 4.77, -74.04)

        outcome = self.engine.decidir(incoming, [existing], self.turnos, [preasignacion])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertIn("R2", outcome.assignments)
        self.assertIn("R3", outcome.assignments)

    def test_uses_buffer_as_arrival_window_not_service_duration(self):
        service_time = self.now + timedelta(hours=2)
        base = Coordenadas(4.70, -74.05)
        distance_km = ((self.engine.settings.pre_shift_travel_minutes + 2) / 60) * self.engine.settings.average_speed_kmh
        origin = Coordenadas(base.lat + (distance_km / 111.195), base.lng)
        turno = TurnoPlan(
            id_turno="3",
            cedula_conductor="300",
            nombre_conductor="Tecnico 3",
            celular_tecnico="3000000002",
            proveedor="MYS",
            direccion_origen="Base",
            punto_inicio=base,
            fecha_inicio_turno=service_time,
            fecha_fin_turno=service_time + timedelta(minutes=40),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
        )
        service = self.make_service("R4", 120, origin.lat, origin.lng)
        service.destino = Coordenadas(origin.lat, origin.lng)

        outcome = self.engine.decidir(service, [], [turno], [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["R4"].id_turno, "3")

    def test_manual_service_blocks_only_declared_turn(self):
        manual = self.make_service("MANUAL-1", 180, 4.70, -74.05)
        manual.estado_operacion = ESTADO_MANUAL
        manual.id_turno = "1"
        incoming = self.make_service("R5", 180, 4.71, -74.06)

        outcome = self.engine.decidir(incoming, [manual], self.turnos, [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["R5"].id_turno, "2")

    def test_manual_service_does_not_make_only_candidate_turn_infeasible(self):
        manual = self.make_service("MANUAL-2", 180, 4.70, -74.05)
        manual.estado_operacion = ESTADO_MANUAL
        manual.id_turno = "1"
        incoming = self.make_service("R6", 180, 4.71, -74.06)

        outcome = self.engine.decidir(incoming, [manual], [self.turnos[0]], [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["R6"].id_turno, "1")

    def test_manual_service_prefers_other_turn_for_following_travel(self):
        manual = self.make_service("MANUAL-FIXED", 120, 4.70, -74.05)
        manual.destino = Coordenadas(4.70, -74.05)
        manual.estado_operacion = ESTADO_MANUAL
        manual.id_turno = "1"
        incoming = self.make_service("R-AFTER-MANUAL", 132, 4.85, -74.05)

        outcome = self.engine.decidir(incoming, [manual], self.turnos, [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["R-AFTER-MANUAL"].id_turno, "2")

    def test_urgent_manual_service_is_ignored_by_automatic_routing(self):
        urgent = self.make_service("URGENT-OUT", 180, 4.70, -74.05)
        urgent.estado_operacion = ESTADO_URGENTE_GESTIONAR_MANUAL
        urgent.id_turno = "1"
        incoming = self.make_service("R-AFTER-URGENT", 180, 4.71, -74.06)

        outcome = self.engine.decidir(incoming, [urgent], self.turnos, [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["R-AFTER-URGENT"].id_turno, "1")

    def test_manual_service_without_turn_is_ignored(self):
        manual = self.make_service("MANUAL-3", 180, 4.70, -74.05)
        manual.estado_operacion = ESTADO_MANUAL
        incoming = self.make_service("R7", 180, 4.71, -74.06)

        outcome = self.engine.decidir(incoming, [manual], [self.turnos[0]], [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["R7"].id_turno, "1")

    def test_manual_block_can_be_zero_after_preserving_real_service(self):
        existing = self.make_service("R8", 180, 4.70, -74.05)
        existing.estado_operacion = ESTADO_PREASIGNADO
        manual = self.make_service("MANUAL-4", 180, 4.70, -74.05)
        manual.estado_operacion = ESTADO_MANUAL
        manual.id_turno = "1"

        blocks = self.engine._build_manual_blocks_by_turn(
            manual_services=[manual],
            turnos=[self.turnos[0]],
            normal_services_by_turn={"1": [existing]},
        )

        self.assertEqual(blocks, {})

    def test_manual_block_uses_twenty_minutes_when_coordinates_are_missing(self):
        manual = self.make_service("MANUAL-5", 180, 0.0, 0.0)
        manual.destino = Coordenadas(0.0, 0.0)
        manual.estado_operacion = ESTADO_MANUAL
        manual.id_turno = "1"

        blocks = self.engine._build_manual_blocks_by_turn(
            manual_services=[manual],
            turnos=[self.turnos[0]],
            normal_services_by_turn={"1": []},
        )

        block = blocks["1"][0]
        self.assertEqual(block.start, manual.fecha_servicio - timedelta(minutes=20))
        self.assertEqual(
            block.end,
            manual.fecha_servicio + timedelta(minutes=self.engine.settings.onsite_minutes + 20),
        )

    def test_infeasible_other_department_does_not_reject_incoming_service(self):
        cundinamarca_locked = self.make_service("CUN-LOCKED", 120, 4.70, -74.05)
        cundinamarca_locked.estado_operacion = ESTADO_ASIGNADO_FINAL
        cundinamarca_locked.id_turno = "1"
        cundinamarca_dynamic = self.make_service("CUN-DYN", 120, 4.76, -74.03)
        cundinamarca_dynamic.estado_operacion = ESTADO_PREASIGNADO
        incoming = ServicioPlan(
            autorizacion="ANT-IN",
            caso="CASO-ANT-IN",
            ciudad_origen="MEDELLIN",
            ciudad_destino="MEDELLIN",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=self.now + timedelta(minutes=180),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="ANTIOQUIA",
            origen=Coordenadas(6.26, -75.58),
            destino=Coordenadas(6.25, -75.57),
        )
        preasignaciones = [
            PreasignacionPlan(
                id_preasignacion="C1",
                autorizacion="CUN-LOCKED",
                id_turno="1",
                cedula_conductor="100",
                nombre_tecnico_preasignacion="Tecnico 1",
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_CONGELADA,
                orden_en_ruta=1,
            ),
            PreasignacionPlan(
                id_preasignacion="C2",
                autorizacion="CUN-DYN",
                id_turno="1",
                cedula_conductor="100",
                nombre_tecnico_preasignacion="Tecnico 1",
                fecha_preasignacion=self.now,
                estado_preasignacion=PREASIGNACION_ACTIVA,
                orden_en_ruta=2,
            ),
        ]

        outcome = self.engine.decidir(
            incoming,
            [cundinamarca_locked, cundinamarca_dynamic],
            [*self.turnos, self.make_antioquia_turn()],
            preasignaciones,
        )

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["ANT-IN"].id_turno, "ANT-1")

    def test_solver_fallback_finds_feasible_late_insertion(self):
        start = self.now + timedelta(hours=1)
        turnos = [
            TurnoPlan(
                id_turno=str(index),
                cedula_conductor=f"10{index}",
                nombre_conductor=f"Tecnico {index}",
                celular_tecnico="3000000000",
                proveedor="MYS",
                direccion_origen="Base",
                punto_inicio=base,
                fecha_inicio_turno=start,
                fecha_fin_turno=start + timedelta(hours=6, minutes=30),
                servicio="CONDUCTOR ELEGIDO",
                tipo_servicio="PROGRAMADO",
                departamento="CUNDINAMARCA",
            )
            for index, base in [
                (31, Coordenadas(4.5698, -74.103)),
                (32, Coordenadas(4.5698, -74.103)),
                (33, Coordenadas(4.7343, -74.0961)),
                (34, Coordenadas(4.7343, -74.0961)),
                (35, Coordenadas(4.7343, -74.0961)),
            ]
        ]

        def service(auth, minutes, origin, destination):
            return ServicioPlan(
                autorizacion=auth,
                caso=f"CASO-{auth}",
                ciudad_origen="BOGOTA",
                ciudad_destino="BOGOTA",
                direccion_origen="Origen",
                direccion_destino="Destino",
                fecha_servicio=start + timedelta(minutes=minutes),
                servicio="CONDUCTOR ELEGIDO",
                tipo_servicio="PROGRAMADO",
                departamento="CUNDINAMARCA",
                origen=Coordenadas(*origin),
                destino=Coordenadas(*destination),
            )

        locked_by_turn = {
            "31": [service("6402561", 0, (4.694803, -74.037558), (4.700613, -74.087782))],
            "32": [
                service("6402581", 0, (4.663415, -74.010568), (4.658207, -74.121289)),
                service("6402318", 120, (4.668861, -74.139577), (4.664169, -74.112902)),
            ],
            "33": [
                service("6402593", 0, (4.73429, -74.056204), (4.704043, -74.029629)),
                service("6401961", 120, (4.895679, -74.057214), (4.743263, -74.054947)),
            ],
            "34": [service("6402374", 0, (4.80928, -74.092094), (4.722072, -74.132604))],
            "35": [service("6402592", 60, (4.810963, -74.093064), (4.736542, -74.052678))],
        }
        dynamic_services = [
            service("6402022", 210, (4.924484, -73.996421), (4.877515, -74.04361)),
            service("6402330", 210, (4.74444, -74.266967), (4.716932, -74.109131)),
            service("6402378", 260, (4.620141, -74.130935), (4.618031, -74.080467)),
            service("6402771", 180, (4.718645, -74.059191), (4.869743, -74.06136)),
            service("6402785", 300, (4.820351, -74.035454), (4.727002, -74.071255)),
            service("6402828", 300, (4.652759, -74.153163), (4.693536, -74.079936)),
            service("6402827", 300, (4.685255, -74.056152), (4.642338, -74.059446)),
        ]
        current_turn_by_auth = {
            "6402022": "35",
            "6402330": "34",
            "6402378": "32",
            "6402771": "31",
            "6402785": "31",
            "6402828": "34",
        }

        solution = self.engine._solve_dynamic_assignment(
            dynamic_services=dynamic_services,
            locked_by_turn=locked_by_turn,
            turnos=turnos,
            current_turn_by_auth=current_turn_by_auth,
            now_value=self.now,
            manual_blocks_by_turn={},
        )

        self.assertIsNotNone(solution)
        self.assertEqual(solution.assignments["6402827"].id_turno, "31")


if __name__ == "__main__":
    unittest.main()
