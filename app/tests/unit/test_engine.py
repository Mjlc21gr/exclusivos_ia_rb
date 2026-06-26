"""Pruebas unitarias del motor OR."""

import os
import sys
import unittest
from datetime import timedelta

CURRENT_DIR = os.path.dirname(__file__)
APP_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from or_engine.engine import DecisionEngine
from or_engine.models import Coordenadas, PreasignacionPlan, ServicioPlan, TurnoPlan
from utils.config import get_settings
from utils.constants import ESTADO_PREASIGNADO, PREASIGNACION_ACTIVA
from utils.geo import haversine_km, travel_minutes
from utils.time_utils import now_bogota


class DecisionEngineTest(unittest.TestCase):
    """Cobertura basica de restricciones duras."""

    def setUp(self) -> None:
        self.previous_pre_shift = os.environ.pop("PRE_SHIFT_TRAVEL_MINUTES", None)
        get_settings.cache_clear()
        self.engine = DecisionEngine()
        self.now = now_bogota()
        self.accept_minutes = self.engine.settings.min_notice_minutes + 30
        self.reject_minutes = max(1, self.engine.settings.min_notice_minutes - 30)
        self.turno = TurnoPlan(
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
        )

    def tearDown(self) -> None:
        if self.previous_pre_shift is not None:
            os.environ["PRE_SHIFT_TRAVEL_MINUTES"] = self.previous_pre_shift
        get_settings.cache_clear()

    def make_service(self, auth: str, minutes_from_now: int, lat: float = 4.71, lng: float = -74.06):
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

    def point_north_for_travel(self, base: Coordenadas, minutes: float) -> Coordenadas:
        distance_km = (minutes / 60) * self.engine.settings.average_speed_kmh
        return Coordenadas(base.lat + (distance_km / 111.195), base.lng)

    def make_turn_at(self, turn_id: str, start_time, end_time, base: Coordenadas = None) -> TurnoPlan:
        return TurnoPlan(
            id_turno=turn_id,
            cedula_conductor=f"100-{turn_id}",
            nombre_conductor=f"Tecnico {turn_id}",
            celular_tecnico="3000000000",
            proveedor="MYS",
            direccion_origen="Base",
            punto_inicio=base or Coordenadas(4.70, -74.05),
            fecha_inicio_turno=start_time,
            fecha_fin_turno=end_time,
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
        )

    def test_default_pre_shift_travel_is_40_minutes(self):
        self.assertEqual(get_settings().pre_shift_travel_minutes, 40)

    def test_reject_if_less_than_one_hour_notice(self):
        outcome = self.engine.decidir(self.make_service("S1", self.reject_minutes), [], [self.turno], [])
        self.assertEqual(outcome.decision, "RECHAZAR")

    def test_accept_feasible_service(self):
        service = self.make_service("S2", self.accept_minutes)
        outcome = self.engine.decidir(service, [], [self.turno], [])
        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertIn("S2", outcome.assignments)

    def test_reject_when_existing_assignment_makes_route_infeasible(self):
        existing = self.make_service("S3", self.accept_minutes)
        existing.estado_operacion = ESTADO_PREASIGNADO
        preasignacion = PreasignacionPlan(
            id_preasignacion="1",
            autorizacion="S3",
            id_turno="1",
            cedula_conductor="100",
            nombre_tecnico_preasignacion="Tecnico 1",
            fecha_preasignacion=self.now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=2,
        )
        incoming = self.make_service("S4", self.accept_minutes + 1, lat=4.72, lng=-74.07)
        outcome = self.engine.decidir(incoming, [existing], [self.turno], [preasignacion])
        self.assertEqual(outcome.decision, "RECHAZAR")

    def test_accepts_out_of_order_arrivals_by_reordering_route(self):
        existing = self.make_service("S5", 300, lat=4.72, lng=-74.09)
        existing.estado_operacion = ESTADO_PREASIGNADO
        preasignacion = PreasignacionPlan(
            id_preasignacion="2",
            autorizacion="S5",
            id_turno="1",
            cedula_conductor="100",
            nombre_tecnico_preasignacion="Tecnico 1",
            fecha_preasignacion=self.now,
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=3,
        )
        incoming = self.make_service("S6", self.accept_minutes, lat=4.71, lng=-74.06)
        outcome = self.engine.decidir(incoming, [existing], [self.turno], [preasignacion])
        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["S6"].id_turno, "1")
        self.assertEqual(outcome.assignments["S5"].id_turno, "1")

    def test_allows_pre_shift_travel_but_service_starts_inside_turn(self):
        service_time = self.now + timedelta(minutes=self.accept_minutes)
        turno = TurnoPlan(
            id_turno="2",
            cedula_conductor="200",
            nombre_conductor="Tecnico 2",
            celular_tecnico="3000000001",
            proveedor="MYS",
            direccion_origen="Base Sur",
            punto_inicio=Coordenadas(4.6297, -74.1468),
            fecha_inicio_turno=service_time,
            fecha_fin_turno=service_time + timedelta(hours=4),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
        )
        service = ServicioPlan(
            autorizacion="S7",
            caso="CASO-S7",
            ciudad_origen="BOGOTA",
            ciudad_destino="BOGOTA",
            direccion_origen="Origen",
            direccion_destino="Destino",
            fecha_servicio=service_time,
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
            origen=Coordenadas(4.70, -74.10),
            destino=Coordenadas(4.71, -74.09),
        )

        outcome = self.engine.decidir(service, [], [turno], [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["S7"].id_turno, "2")

    def test_uses_40_minutes_only_for_pre_shift_travel(self):
        service_time = self.now + timedelta(minutes=self.accept_minutes)
        base = Coordenadas(4.70, -74.05)
        origin = self.point_north_for_travel(base, 38)
        destination = Coordenadas(origin.lat + 0.001, origin.lng)
        turno = self.make_turn_at("PRE40", service_time, service_time + timedelta(hours=2), base)
        service = self.make_service("S10", self.accept_minutes, lat=origin.lat, lng=origin.lng)
        service.destino = destination

        self.assertGreater(travel_minutes(haversine_km(base.lat, base.lng, origin.lat, origin.lng)), 35)

        outcome = self.engine.decidir(service, [], [turno], [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["S10"].id_turno, "PRE40")

    def test_allows_arrival_up_to_buffer_minutes_late(self):
        service_time = self.now + timedelta(minutes=self.accept_minutes)
        base = Coordenadas(4.70, -74.05)
        origin = self.point_north_for_travel(base, self.engine.settings.pre_shift_travel_minutes + 4)
        turno = self.make_turn_at("LATE5", service_time, service_time + timedelta(hours=3), base)
        service = self.make_service("S11", self.accept_minutes, lat=origin.lat, lng=origin.lng)
        service.destino = Coordenadas(origin.lat + 0.001, origin.lng)

        outcome = self.engine.decidir(service, [], [turno], [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["S11"].id_turno, "LATE5")

    def test_rejects_arrival_after_buffer_minutes(self):
        service_time = self.now + timedelta(minutes=self.accept_minutes)
        base = Coordenadas(4.70, -74.05)
        origin = self.point_north_for_travel(base, self.engine.settings.pre_shift_travel_minutes + 7)
        turno = self.make_turn_at("LATE7", service_time, service_time + timedelta(hours=3), base)
        service = self.make_service("S12", self.accept_minutes, lat=origin.lat, lng=origin.lng)
        service.destino = Coordenadas(origin.lat + 0.001, origin.lng)

        outcome = self.engine.decidir(service, [], [turno], [])

        self.assertEqual(outcome.decision, "RECHAZAR")
        self.assertIn("llegada tardia", outcome.razon)

    def test_buffer_minutes_are_not_added_to_service_duration(self):
        service_time = self.now + timedelta(minutes=self.accept_minutes)
        base = Coordenadas(4.70, -74.05)
        turno = self.make_turn_at(
            "NO_BUFFER_DURATION",
            self.now + timedelta(hours=1),
            service_time + timedelta(minutes=self.engine.settings.onsite_minutes + 1),
            base,
        )
        service = self.make_service("S13", self.accept_minutes, lat=base.lat, lng=base.lng)
        service.destino = Coordenadas(base.lat, base.lng)

        outcome = self.engine.decidir(service, [], [turno], [])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["S13"].id_turno, "NO_BUFFER_DURATION")

    def test_ignores_historical_assigned_services_when_planning_future_turn(self):
        past_service = self.make_service("S8", -120)
        past_service.estado_operacion = ESTADO_PREASIGNADO
        past_preasignacion = PreasignacionPlan(
            id_preasignacion="3",
            autorizacion="S8",
            id_turno="OLD",
            cedula_conductor="100",
            nombre_tecnico_preasignacion="Tecnico anterior",
            fecha_preasignacion=self.now - timedelta(hours=4),
            estado_preasignacion=PREASIGNACION_ACTIVA,
            orden_en_ruta=1,
            row_index=4,
        )
        future_turn = TurnoPlan(
            id_turno="FUTURE",
            cedula_conductor="300",
            nombre_conductor="Tecnico futuro",
            celular_tecnico="3000000002",
            proveedor="MYS",
            direccion_origen="Base",
            punto_inicio=Coordenadas(4.70, -74.05),
            fecha_inicio_turno=self.now + timedelta(hours=2),
            fecha_fin_turno=self.now + timedelta(hours=9),
            servicio="CONDUCTOR ELEGIDO",
            tipo_servicio="PROGRAMADO",
            departamento="CUNDINAMARCA",
        )
        incoming = self.make_service("S9", 180)

        outcome = self.engine.decidir(incoming, [past_service, incoming], [future_turn], [past_preasignacion])

        self.assertEqual(outcome.decision, "ACEPTAR")
        self.assertEqual(outcome.assignments["S9"].id_turno, "FUTURE")


if __name__ == "__main__":
    unittest.main()
