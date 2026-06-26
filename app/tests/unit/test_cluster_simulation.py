"""Pruebas del runner de simulacion cluster."""

import csv
import json
from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CURRENT_DIR = os.path.dirname(__file__)
APP_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from services.cluster_simulation import (
    GoogleDistanceMatrixClient,
    _duration_to_minutes,
    build_events,
    cancellation_time_from_row,
    generate_turnos,
    google_departure_reference,
    load_simulation_data,
    load_turnos_csv,
    parse_datetime_value,
    service_payload_from_row,
    summarize_google_matrix_estimates,
    validation_window_for_simulation_date,
    weekend_dates,
    write_summary_csv,
)
from services.google_matrix_estimator import (
    get_estimates,
    record_solve_estimate,
    reset_estimates,
    reset_estimation_context,
    set_estimation_context,
)

FIXTURES_DIR = Path(APP_DIR) / "tests" / "fixtures"


class ClusterSimulationTest(unittest.TestCase):
    """Valida generacion de turnos y eventos de simulacion."""

    def test_weekend_dates_are_real_friday_saturday_sunday(self):
        dates = [value.strftime("%Y-%m-%d") for value in weekend_dates("2026-04-01", "2026-04-20")]

        self.assertEqual(
            dates,
            [
                "2026-04-03",
                "2026-04-04",
                "2026-04-05",
                "2026-04-10",
                "2026-04-11",
                "2026-04-12",
                "2026-04-17",
                "2026-04-18",
                "2026-04-19",
            ],
        )

    def test_generate_turnos_builds_three_per_department_per_date(self):
        rows = generate_turnos("2026-04-01", "2026-04-20")

        self.assertEqual(len(rows), 54)
        first = rows[0]
        self.assertEqual(first["FECHA_INICIO_TURNO"], "2026-04-03 21:00:00")
        self.assertEqual(first["FECHA_FIN_TURNO"], "2026-04-04 04:00:00")
        cundinamarca_first_day = [
            row for row in rows
            if row["DEPARTAMENTO"] == "CUNDINAMARCA"
            and row["FECHA_INICIO_TURNO"] == "2026-04-03 21:00:00"
        ]
        antioquia_first_day = [
            row for row in rows
            if row["DEPARTAMENTO"] == "ANTIOQUIA"
            and row["FECHA_INICIO_TURNO"] == "2026-04-03 21:00:00"
        ]
        self.assertEqual(len(cundinamarca_first_day), 3)
        self.assertEqual(len(antioquia_first_day), 3)

    def test_service_payload_from_row_maps_cluster_csv_columns(self):
        payload = service_payload_from_row(
            {
                "NUMERO_CASO": "4751951",
                "NUMERO_AUTORIZACION": "6320025",
                "CIUDAD_ORIGEN": "MEDELLIN",
                "CIUDAD_DESTINO": "MEDELLIN",
                "DIRECCION_DE_DESTINO": "",
                "FECHA_SERVICIO": "2026-04-15 3:00:00",
                "CEDULA_ASEGURADO": "1",
                "ASEGURADO": "1",
                "CELULAR_ASEGURADO": "1",
                "CLV": "1",
                "TIPO_CIUDAD": "URBANO",
                "TIPO_FALLIDO": "",
                "ID_CITA_SERVICIO": "08pTN000008zYbGYAU",
                "SERVICIO": "CONDUCTOR ELEGIDO",
                "TIPO_SERVICIO": "PROGRAMADO",
                "DEPARTAMENTO": "ANTIOQUIA",
                "FECHA_CREACION": "14/03/2026 23:04:49",
                "ORIGEN_LAT": "6,222456",
                "ORIGEN_LON": "-75,564938",
                "ORIGEN_DIR": "Carrera 38 19-190",
                "DESTINO_LAT": "6,212972",
                "DESTINO_LON": "-75,607692",
                "DESTINO_DIR": "Carrera 84F #3c 39",
                "OBSERVACIONES_SF": "Procesado OK",
            },
            auth_prefix="SIM-",
        )

        self.assertEqual(payload["autorizacion"], "SIM-6320025")
        self.assertEqual(payload["caso"], "4751951")
        self.assertEqual(payload["tipo_servicio"], "CONDUCTOR ELEGIDO")
        self.assertEqual(payload["modalidad_servicio"], "PROGRAMADO")
        self.assertEqual(payload["fecha_servicio"], "2026-04-15 03:00:00")
        self.assertEqual(payload["fecha_creacion_servicio"], "2026-03-14 23:04:49")
        self.assertEqual(payload["fecha_recepcion_rb"], "2026-03-14 23:04:49")
        self.assertEqual(payload["lat_origen"], "6.222456")
        self.assertEqual(payload["lng_destino"], "-75.607692")
        self.assertEqual(payload["direccion_destino"], "Carrera 84F #3c 39")

    def test_build_events_adds_hourly_validations_and_orders_services_first_on_tie(self):
        payloads = [
            {
                "autorizacion": "SIM-1",
                "fecha_servicio": "2026-04-04 03:00:00",
            }
        ]
        events = build_events(payloads, "2026-04-03", "2026-04-03")
        validar_times = [
            event.event_time.strftime("%Y-%m-%d %H:%M:%S")
            for event in events
            if event.kind == "validar"
        ]

        self.assertEqual(
            validar_times,
            [
                "2026-04-03 21:00:00",
                "2026-04-03 22:00:00",
                "2026-04-03 23:00:00",
                "2026-04-04 00:00:00",
                "2026-04-04 01:00:00",
                "2026-04-04 02:00:00",
                "2026-04-04 03:00:00",
                "2026-04-04 04:00:00",
            ],
        )
        tied = [event for event in events if event.event_time == datetime(2026, 4, 4, 1, 0, 0)]
        self.assertEqual([event.kind for event in tied], ["servicio", "validar"])

    def test_parse_datetime_accepts_single_digit_hour(self):
        self.assertEqual(
            parse_datetime_value("2026-04-15 3:00:00").strftime("%Y-%m-%d %H:%M:%S"),
            "2026-04-15 03:00:00",
        )

    def test_service_payload_prefers_fecha_creacion_servicio(self):
        payload = service_payload_from_row(
            {
                "NUMERO_CASO": "1",
                "NUMERO_AUTORIZACION": "700",
                "FECHA_SERVICIO": "2026-06-06 23:00:00",
                "FECHA_CREACION": "2026-06-01 10:00:00",
                "FECHA_CREACION_SERVICIO": "2026-06-06 18:21:00",
            }
        )

        self.assertEqual(payload["fecha_creacion_servicio"], "2026-06-06 18:21:00")

    def test_service_payload_maps_standardized_simulation_csv_columns(self):
        payload = service_payload_from_row(
            {
                "NUMERO_CASO": "4823920",
                "NUMERO_AUTORIZACION": "6410166",
                "CIUDAD_ORIGEN": "BOGOTA",
                "CIUDAD_DESTINO": "BOGOTA",
                "FECHA_SERVICIO": "2026-06-06T23:00:00.000Z",
                "SERVICIO": "CONDUCTOR ELEGIDO",
                "TIPO_SERVICIO": "PROGRAMADO",
                "DEPARTAMENTO": "CUNDINAMARCA",
                "FECHA_CREACION": "2026-06-06 22:25:00.000000",
                "fecha_hora_programacion_servicio": "2026-06-06 23:00:00.000000",
                "placa_identificacion": "JVR207",
                "direccion_origen": "Cl. 85 #12 42",
                "direccion_destino": "Variante de Cota #Km. 1.5",
                "lat_origen": "4.6729587",
                "lon_origen": "-74.06366059999999",
                "lat_destino": "4.8086322",
                "lon_destino": "-74.0914847",
                "FECHA_HORA_CANCELACION_SERVICIO": "2026-06-06 19:32:54.815423",
            }
        )

        self.assertEqual(payload["autorizacion"], "6410166")
        self.assertEqual(payload["placa"], "JVR207")
        self.assertEqual(payload["fecha_servicio"], "2026-06-06 23:00:00")
        self.assertEqual(payload["fecha_creacion_servicio"], "2026-06-06 22:25:00")
        self.assertEqual(payload["lat_origen"], "4.6729587")
        self.assertEqual(payload["lng_destino"], "-74.0914847")
        self.assertEqual(payload["direccion_origen"], "Cl. 85 #12 42")

    def test_cancellation_time_from_row_is_optional(self):
        self.assertIsNone(cancellation_time_from_row({"FECHA_HORA_CANCELACION": ""}))
        self.assertEqual(
            cancellation_time_from_row({"FECHA_HORA_CANCELACION": "2026-06-06 21:00:00"}),
            datetime(2026, 6, 6, 21, 0, 0),
        )
        self.assertEqual(
            cancellation_time_from_row({"FECHA_HORA_CANCELACION_SERVICIO": "2026-06-06 21:00:00.123456"}),
            datetime(2026, 6, 6, 21, 0, 0, 123456),
        )

    def test_write_summary_csv_serializes_rejection_reason_counts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "summary.csv"
            write_summary_csv(
                {
                    "servicios_rechazados": 2,
                    "motivos_rechazo": {"No existe turno compatible": 2},
                },
                output_path,
            )
            content = output_path.read_text(encoding="utf-8")
            row = next(csv.DictReader(content.splitlines()))

        self.assertEqual(json.loads(row["motivos_rechazo"]), {"No existe turno compatible": 2})

    def test_load_turnos_csv_applies_prefix_and_normalizes_coordinates(self):
        rows = load_turnos_csv(FIXTURES_DIR / "simulation_dummy_turnos.csv", turno_prefix="RUN-")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ID_TURNO"], "RUN-TURNO-1")
        self.assertEqual(rows[0]["LATITUD_SERVICIO_ORIGEN"], "4.676300")

    def test_load_turnos_csv_accepts_cp1252_export(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "turnos_cp1252.csv"
            output_path.write_bytes(
                (
                    "ID_TURNO,CEDULA_CONDUCTOR,NOMBRE_CONDUCTOR,CELULAR_TECNICO,CORREO,PROVEEDOR,"
                    "DIRECCION_ORIGEN,LATITUD_SERVICIO_ORIGEN,LONGITUD_SERVICIO_ORIGEN,"
                    "FECHA_INICIO_TURNO,FECHA_FIN_TURNO,SERVICIO,TIPO_SERVICIO,DEPARTAMENTO\n"
                    "T-1,1,José Álvarez,300,jose@example.com,MYS,Bogotá,4.567,-74.100,"
                    "2026-06-06 21:00:00,2026-06-07 04:00:00,CONDUCTOR ELEGIDO,PROGRAMADO,CUNDINAMARCA\n"
                ).encode("cp1252")
            )

            rows = load_turnos_csv(output_path)

        self.assertEqual(rows[0]["NOMBRE_CONDUCTOR"], "José Álvarez")

    def test_load_simulation_data_loads_dummy_cancelation(self):
        data = load_simulation_data(
            FIXTURES_DIR / "simulation_dummy_services.csv",
            FIXTURES_DIR / "simulation_dummy_turnos.csv",
            "2026-06-06",
            "2026-06-07",
            "AUTH-",
            "TURN-",
        )

        self.assertEqual(len(data.payloads), 4)
        self.assertEqual(len(data.turnos), 2)
        self.assertIn("AUTH-700002", data.cancelaciones)

    def test_build_events_adds_ten_minute_validations_and_cancelations(self):
        payloads = [
            {
                "autorizacion": "SIM-1",
                "fecha_servicio": "2026-06-06 21:30:00",
                "fecha_creacion_servicio": "2026-06-06 21:00:00",
            }
        ]
        events = build_events(
            payloads,
            "2026-06-06",
            "2026-06-06",
            cancelaciones={"SIM-1": datetime(2026, 6, 6, 21, 10, 0)},
            validar_every_minutes=10,
            window_start=datetime(2026, 6, 6, 21, 0, 0),
            window_end=datetime(2026, 6, 6, 21, 20, 0),
        )

        self.assertEqual(
            [(event.kind, event.event_time.strftime("%H:%M")) for event in events],
            [
                ("servicio", "21:00"),
                ("validar", "21:00"),
                ("cancelacion", "21:10"),
                ("validar", "21:10"),
                ("validar", "21:20"),
            ],
        )

    def test_validation_window_for_simulation_date_starts_at_midnight_and_reaches_last_event(self):
        window_start, window_end = validation_window_for_simulation_date(
            "2026-06-06",
            [
                {
                    "fecha_servicio": "2026-06-07 04:15:00",
                }
            ],
            {"SIM-1": datetime(2026, 6, 6, 19, 30, 0)},
            datetime(2026, 6, 7, 3, 30, 0),
        )

        self.assertEqual(window_start, datetime(2026, 6, 6, 0, 0, 0))
        self.assertEqual(window_end, datetime(2026, 6, 7, 4, 15, 0))

    def test_summarize_google_matrix_estimates_without_free_tier(self):
        summary = summarize_google_matrix_estimates(
            [
                {"core_elements": 130, "full_elements": 160, "endpoint": "servicio"},
                {"core_elements": 20, "full_elements": 40, "endpoint": "validar"},
            ],
            price_per_1000_usd=5.0,
            max_elements_per_request=625,
        )

        self.assertEqual(summary["total_core_elements"], 150)
        self.assertEqual(summary["total_full_elements"], 200)
        self.assertEqual(summary["provider"], "google_distance_matrix_legacy")
        self.assertEqual(summary["sku"], "Distance Matrix Advanced")
        self.assertFalse(summary["free_tier_applied"])
        self.assertEqual(summary["estimated_full_cost_usd"], 1.0)
        self.assertEqual(summary["estimated_full_matrix_requests"], 1)

    def test_google_matrix_estimator_records_only_when_enabled(self):
        previous = os.environ.get("ESTIMATE_GOOGLE_MATRIX_COST")
        reset_estimates()
        try:
            os.environ["ESTIMATE_GOOGLE_MATRIX_COST"] = "false"
            record_solve_estimate(
                services_count=10,
                turns_count=3,
                manual_blocks_count=0,
                dynamic_services_count=10,
                locked_services_count=0,
                department="CUNDINAMARCA",
            )
            self.assertEqual(get_estimates(), [])

            os.environ["ESTIMATE_GOOGLE_MATRIX_COST"] = "true"
            token = set_estimation_context("servicio", "req-1", "AUTH-1")
            try:
                record_solve_estimate(
                    services_count=10,
                    turns_count=3,
                    manual_blocks_count=0,
                    dynamic_services_count=10,
                    locked_services_count=0,
                    department="CUNDINAMARCA",
                )
            finally:
                reset_estimation_context(token)

            records = get_estimates()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["core_elements"], 130)
            self.assertEqual(records[0]["endpoint"], "servicio")
        finally:
            reset_estimates()
            if previous is None:
                os.environ.pop("ESTIMATE_GOOGLE_MATRIX_COST", None)
            else:
                os.environ["ESTIMATE_GOOGLE_MATRIX_COST"] = previous

    def test_google_duration_and_departure_reference(self):
        self.assertEqual(_duration_to_minutes("120s"), 2.0)
        reference = google_departure_reference(
            "2026-06-01",
            now_value=datetime(2026, 6, 2, 10, 0, 0),
        )

        self.assertEqual(reference.strftime("%Y-%m-%d %H:%M:%S"), "2026-06-08 21:00:00")

    def test_google_distance_matrix_client_uses_duration_in_traffic(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(
                    {
                        "status": "OK",
                        "rows": [
                            {
                                "elements": [
                                    {
                                        "status": "OK",
                                        "duration": {"value": 600, "text": "10 mins"},
                                        "duration_in_traffic": {"value": 780, "text": "13 mins"},
                                        "distance": {"value": 3500, "text": "3.5 km"},
                                    }
                                ]
                            }
                        ],
                    }
                ).encode("utf-8")

        captured_urls = []

        def fake_urlopen(request, timeout):
            captured_urls.append(request.full_url)
            return FakeResponse()

        client = GoogleDistanceMatrixClient("test-key", datetime(2026, 6, 8, 21, 0, 0))
        with patch("services.cluster_simulation.urllib.request.urlopen", fake_urlopen):
            minutes = client.travel_minutes(4.6763, -74.0479, 4.69, -74.06)

        self.assertEqual(minutes, 13.0)
        self.assertIn("maps.googleapis.com/maps/api/distancematrix/json", captured_urls[0])
        self.assertIn("traffic_model=best_guess", captured_urls[0])


if __name__ == "__main__":
    unittest.main()
