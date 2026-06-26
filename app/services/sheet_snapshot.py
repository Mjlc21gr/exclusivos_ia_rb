"""Consulta un servicio puntual en Google Sheets y devuelve JSON."""

from __future__ import annotations

import json
import sys

from services.google_sheets import GoogleSheetsRepository


def main(autorizacion: str) -> None:
    """Imprime un snapshot del servicio y sus preasignaciones."""

    repository = GoogleSheetsRepository()
    servicio = repository.get_servicio(autorizacion)
    preasignaciones = [
        item for item in repository.list_preasignaciones() if item.autorizacion == autorizacion
    ]
    payload = {
        "autorizacion": autorizacion,
        "servicio_estado": servicio.estado_operacion if servicio else "NO_ENCONTRADO",
        "cedula_conductor": servicio.cedula_conductor if servicio else "",
        "nombre_conductor": servicio.nombre_conductor if servicio else "",
        "preasignaciones": [
            {
                "id_turno": item.id_turno,
                "estado_preasignacion": item.estado_preasignacion,
                "orden_en_ruta": item.orden_en_ruta,
            }
            for item in preasignaciones
        ],
    }
    print(json.dumps(payload, ensure_ascii=True))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Uso: python services/sheet_snapshot.py <autorizacion>")
    main(sys.argv[1])
