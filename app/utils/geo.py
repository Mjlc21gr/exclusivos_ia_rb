"""Funciones geograficas del sistema."""

from __future__ import annotations

import math

from utils.config import get_settings


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calcula distancia Haversine en kilometros."""

    radius = 6371.0
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def travel_minutes(distance_km: float) -> float:
    """Convierte kilometros a minutos usando la velocidad promedio configurada."""

    speed = get_settings().average_speed_kmh
    return (distance_km / speed) * 60
