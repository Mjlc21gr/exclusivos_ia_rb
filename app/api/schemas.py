"""Schemas HTTP del sistema RVE."""

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from utils.constants import TIPOS_ENRUTAMIENTO


class ServicioRequest(BaseModel):
    """Payload de entrada para nuevos servicios."""

    autorizacion: str = Field(..., description="Numero de autorizacion")
    caso: str = Field(..., description="Numero de caso")
    ciudad_origen: str = Field(..., description="Ciudad de origen")
    ciudad_destino: str = Field(..., description="Ciudad de destino")
    departamento: str = Field(..., description="Departamento")
    tipo_servicio: str = Field(..., description="Tipo de servicio que atiende el turno")
    fecha_servicio: str = Field(..., description="Fecha y hora del servicio")
    lat_origen: str = Field(..., description="Latitud de origen")
    lng_origen: str = Field(..., description="Longitud de origen")
    lat_destino: str = Field(..., description="Latitud de destino")
    lng_destino: str = Field(..., description="Longitud de destino")

    placa: str = ""
    direccion_origen: str = ""
    direccion_destino: str = ""
    cedula_asegurado: str = ""
    asegurado: str = ""
    celular_asegurado: str = ""
    clv: str = ""
    observaciones: str = ""
    tipo_ciudad: str = ""
    tipo_fallido: str = ""
    fecha_creacion_servicio: str = ""
    fecha_recepcion_rb: str = ""
    evidencias: str = ""
    correos: str = ""
    modalidad_servicio: str = "PROGRAMADO"


class DecisionResponse(BaseModel):
    """Respuesta final del endpoint de decision."""

    autorizacion: str
    decision: str
    id_turno: Optional[str] = None
    cedula_conductor: Optional[str] = None
    nombre_conductor: Optional[str] = None
    razon: str
    timestamp: str


class CancelacionResponse(BaseModel):
    """Respuesta del endpoint de cancelacion."""

    autorizacion: str
    estado: str
    resultado: str


class CompletarResponse(BaseModel):
    """Respuesta del endpoint de completar."""

    autorizacion: str
    estado: str
    resultado: str


class DebugNowRequest(BaseModel):
    """Payload para fijar la hora simulada del proceso local."""

    now_value: str = ""


class RoutingModeRequest(BaseModel):
    """Payload para cambiar el tipo de enrutamiento."""

    tipo_enrutamiento: str = Field(..., description="AUTOMATICO o MANUAL")

    @field_validator("tipo_enrutamiento")
    @classmethod
    def validate_tipo_enrutamiento(cls, value: str) -> str:
        normalized = str(value or "").strip().upper()
        if normalized not in TIPOS_ENRUTAMIENTO:
            raise ValueError("tipo_enrutamiento debe ser AUTOMATICO o MANUAL")
        return normalized
