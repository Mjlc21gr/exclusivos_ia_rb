"""Modelos internos del motor OR."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from utils.parsing import parse_coordinate
from utils.time_utils import parse_datetime


@dataclass(frozen=True)
class Coordenadas:
    """Representa un punto geografico."""

    lat: float
    lng: float


@dataclass
class ServicioPlan:
    """Representa un servicio cargado desde Sheets o API."""

    autorizacion: str
    caso: str
    ciudad_origen: str
    ciudad_destino: str
    direccion_origen: str
    direccion_destino: str
    fecha_servicio: Optional[datetime]
    servicio: str
    tipo_servicio: str
    departamento: str
    origen: Coordenadas
    destino: Coordenadas
    estado_operacion: str = ""
    estado_tecnico: str = ""
    id_turno: str = ""
    cedula_conductor: str = ""
    nombre_conductor: str = ""
    correos: str = ""
    row_index: Optional[int] = None
    id_turno_preasignado: Optional[str] = None
    fecha_creacion_servicio: Optional[datetime] = None

    @classmethod
    def from_sheet_row(cls, row: dict) -> "ServicioPlan":
        """Construye un servicio desde una fila de Sheets."""

        return cls(
            autorizacion=row.get("AUTORIZACION", ""),
            caso=row.get("CASO", ""),
            ciudad_origen=row.get("CIUDAD_ORIGEN", ""),
            ciudad_destino=row.get("CIUDAD_DESTINO", ""),
            direccion_origen=row.get("DIRECCION_ORIGEN", ""),
            direccion_destino=row.get("DIRECCION_DE_DESTINO", ""),
            fecha_servicio=parse_datetime(row.get("FECHA_SERVICIO")),
            servicio=row.get("SERVICIO", ""),
            tipo_servicio=row.get("TIPO_SERVICIO", ""),
            departamento=row.get("DEPARTAMENTO", ""),
            origen=Coordenadas(
                lat=parse_coordinate(row.get("LATITUD_SERVICIO_ORIGEN")),
                lng=parse_coordinate(row.get("LONGITUD_SERVICIO_ORIGEN")),
            ),
            destino=Coordenadas(
                lat=parse_coordinate(row.get("LATITUD_SERVICIO_DESTINO")),
                lng=parse_coordinate(row.get("LONGITUD_SERVICIO_DESTINO")),
            ),
            estado_operacion=row.get("ESTADO_DEL_SERVICIO_OPERACION", ""),
            estado_tecnico=row.get("ESTADO_DEL_SERVICIO_TECNICO", ""),
            id_turno=row.get("ID_TURNO", ""),
            cedula_conductor=row.get("CEDULA_CONDUCTOR", ""),
            nombre_conductor=row.get("NOMBRE_CONDUCTOR", ""),
            correos=row.get("CORREOS", ""),
            row_index=row.get("_row"),
            fecha_creacion_servicio=parse_datetime(row.get("FECHA_CREACION_SERVICIO")),
        )


@dataclass
class TurnoPlan:
    """Representa un turno utilizable por el motor."""

    id_turno: str
    cedula_conductor: str
    nombre_conductor: str
    celular_tecnico: str
    proveedor: str
    direccion_origen: str
    punto_inicio: Coordenadas
    fecha_inicio_turno: Optional[datetime]
    fecha_fin_turno: Optional[datetime]
    servicio: str
    tipo_servicio: str
    departamento: str
    correo: str = ""

    @classmethod
    def from_sheet_row(cls, row: dict) -> "TurnoPlan":
        """Construye un turno desde Sheets."""

        return cls(
            id_turno=row.get("ID_TURNO", ""),
            cedula_conductor=row.get("CEDULA_CONDUCTOR", ""),
            nombre_conductor=row.get("NOMBRE_CONDUCTOR", ""),
            celular_tecnico=row.get("CELULAR_TECNICO", ""),
            proveedor=row.get("PROVEEDOR", ""),
            direccion_origen=row.get("DIRECCION_ORIGEN", ""),
            punto_inicio=Coordenadas(
                lat=parse_coordinate(row.get("LATITUD_SERVICIO_ORIGEN")),
                lng=parse_coordinate(row.get("LONGITUD_SERVICIO_ORIGEN")),
            ),
            fecha_inicio_turno=parse_datetime(row.get("FECHA_INICIO_TURNO")),
            fecha_fin_turno=parse_datetime(row.get("FECHA_FIN_TURNO")),
            servicio=row.get("SERVICIO", ""),
            tipo_servicio=row.get("TIPO_SERVICIO", ""),
            departamento=row.get("DEPARTAMENTO", ""),
            correo=row.get("CORREO", ""),
        )


@dataclass
class PreasignacionPlan:
    """Representa una preasignacion existente en Sheets."""

    id_preasignacion: str
    autorizacion: str
    id_turno: str
    cedula_conductor: str
    nombre_tecnico_preasignacion: str
    fecha_preasignacion: Optional[datetime]
    estado_preasignacion: str
    orden_en_ruta: int
    row_index: Optional[int] = None

    @classmethod
    def from_sheet_row(cls, row: dict) -> "PreasignacionPlan":
        """Construye una preasignacion desde Sheets."""

        orden = row.get("ORDEN_EN_RUTA", "")
        return cls(
            id_preasignacion=row.get("ID_PREASIGNACION", ""),
            autorizacion=row.get("AUTORIZACION", ""),
            id_turno=row.get("ID_TURNO", ""),
            cedula_conductor=row.get("CEDULA_CONDUCTOR", ""),
            nombre_tecnico_preasignacion=row.get("NOMBRE_TECNICO_PREASIGNACION", ""),
            fecha_preasignacion=parse_datetime(row.get("FECHA_PREASIGNACION")),
            estado_preasignacion=row.get("ESTADO_PREASIGNACION", ""),
            orden_en_ruta=int(orden) if str(orden).isdigit() else 1,
            row_index=row.get("_row"),
        )


@dataclass
class DecisionOutcome:
    """Resultado del motor OR."""

    autorizacion: str
    decision: str
    razon: str
    assignments: dict[str, TurnoPlan] = field(default_factory=dict)
    analysis_code: str = ""
