"""Pruebas de modelos cargados desde Google Sheets."""

from datetime import datetime
import os
import sys
import unittest

CURRENT_DIR = os.path.dirname(__file__)
APP_DIR = os.path.abspath(os.path.join(CURRENT_DIR, "..", ".."))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from or_engine.models import ServicioPlan, TurnoPlan


class SheetModelsTest(unittest.TestCase):
    def test_servicio_reads_correos_header(self):
        servicio = ServicioPlan.from_sheet_row(
            {
                "AUTORIZACION": "AUTH-1",
                "FECHA_SERVICIO": "2026-05-06 10:00:00",
                "LATITUD_SERVICIO_ORIGEN": "4,7",
                "LONGITUD_SERVICIO_ORIGEN": "-74,051",
                "LATITUD_SERVICIO_DESTINO": "4,86",
                "LONGITUD_SERVICIO_DESTINO": "-74,05",
                "CORREOS": "tecnico@example.com",
            }
        )

        self.assertEqual(servicio.correos, "tecnico@example.com")

    def test_turno_reads_correo_header(self):
        turno = TurnoPlan.from_sheet_row(
            {
                "ID_TURNO": "1",
                "FECHA_INICIO_TURNO": datetime(2026, 5, 6, 8, 0).isoformat(),
                "FECHA_FIN_TURNO": datetime(2026, 5, 6, 18, 0).isoformat(),
                "LATITUD_SERVICIO_ORIGEN": "4,7",
                "LONGITUD_SERVICIO_ORIGEN": "-74,051",
                "CORREO": "turno@example.com",
            }
        )

        self.assertEqual(turno.correo, "turno@example.com")


if __name__ == "__main__":
    unittest.main()
