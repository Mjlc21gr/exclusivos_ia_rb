"""Servicio de distancias reales via Google Distance Matrix con cache."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

GOOGLE_DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
CACHE_PRECISION_DECIMALS = 3
MAX_ELEMENTS_PER_REQUEST = 25
DEFAULT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class DistanceResult:
    """Resultado de una consulta de distancia real."""

    distance_km: float
    duration_minutes: float
    duration_in_traffic_minutes: float
    source: str  # "cache" | "api" | "haversine_fallback"


class DistanceService:
    """Calcula distancias reales entre puntos usando Google Distance Matrix.

    Implementa cache en memoria por tramo redondeado para minimizar llamadas.
    Si la API falla, usa haversine como fallback transparente.
    """

    def __init__(self) -> None:
        self._api_key = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
        self._cache: Dict[Tuple[float, float, float, float], DistanceResult] = {}
        self._api_calls = 0
        self._cache_hits = 0
        self._fallback_count = 0

    @property
    def is_available(self) -> bool:
        """Indica si el servicio tiene API key configurada."""

        return bool(self._api_key)

    @property
    def stats(self) -> dict:
        """Estadisticas de uso del servicio."""

        return {
            "api_calls": self._api_calls,
            "cache_hits": self._cache_hits,
            "cache_size": len(self._cache),
            "fallback_count": self._fallback_count,
        }

    def get_distance(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
    ) -> DistanceResult:
        """Obtiene distancia y tiempo real entre dos puntos.

        Busca en cache primero. Si no hay hit, llama a Google Distance Matrix.
        Si la API falla, retorna haversine como fallback.
        """

        cache_key = self._cache_key(origin_lat, origin_lng, dest_lat, dest_lng)

        # Cache hit
        if cache_key in self._cache:
            self._cache_hits += 1
            return self._cache[cache_key]

        # Sin API key → haversine directo
        if not self._api_key:
            return self._haversine_fallback(origin_lat, origin_lng, dest_lat, dest_lng)

        # Llamar a Google Distance Matrix
        try:
            result = self._call_distance_matrix(origin_lat, origin_lng, dest_lat, dest_lng)
            self._cache[cache_key] = result
            return result
        except Exception as exc:
            logger.warning(
                "distance.api.failed origin=(%s,%s) dest=(%s,%s) error=%s",
                origin_lat,
                origin_lng,
                dest_lat,
                dest_lng,
                str(exc),
            )
            self._fallback_count += 1
            return self._haversine_fallback(origin_lat, origin_lng, dest_lat, dest_lng)

    def get_distances_batch(
        self,
        origins: List[Tuple[float, float]],
        destination: Tuple[float, float],
    ) -> List[DistanceResult]:
        """Obtiene distancias de multiples origenes a un solo destino.

        Optimiza llamadas agrupando origenes sin cache hit en un solo request.
        """

        results: List[Optional[DistanceResult]] = [None] * len(origins)
        uncached_indices: List[int] = []

        # Buscar en cache
        for i, (lat, lng) in enumerate(origins):
            cache_key = self._cache_key(lat, lng, destination[0], destination[1])
            if cache_key in self._cache:
                self._cache_hits += 1
                results[i] = self._cache[cache_key]
            else:
                uncached_indices.append(i)

        # Si todo esta en cache, retornar
        if not uncached_indices:
            return results  # type: ignore

        # Sin API key → haversine para los que faltan
        if not self._api_key:
            for i in uncached_indices:
                lat, lng = origins[i]
                results[i] = self._haversine_fallback(lat, lng, destination[0], destination[1])
            return results  # type: ignore

        # Llamar en batches
        for batch_start in range(0, len(uncached_indices), MAX_ELEMENTS_PER_REQUEST):
            batch_indices = uncached_indices[batch_start: batch_start + MAX_ELEMENTS_PER_REQUEST]
            batch_origins = [origins[i] for i in batch_indices]

            try:
                batch_results = self._call_distance_matrix_batch(batch_origins, destination)
                for j, idx in enumerate(batch_indices):
                    result = batch_results[j]
                    cache_key = self._cache_key(
                        origins[idx][0], origins[idx][1], destination[0], destination[1]
                    )
                    self._cache[cache_key] = result
                    results[idx] = result
            except Exception as exc:
                logger.warning(
                    "distance.batch.failed batch_size=%s error=%s",
                    len(batch_indices),
                    str(exc),
                )
                for idx in batch_indices:
                    lat, lng = origins[idx]
                    results[idx] = self._haversine_fallback(
                        lat, lng, destination[0], destination[1]
                    )
                    self._fallback_count += 1

        return results  # type: ignore

    def _call_distance_matrix(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
    ) -> DistanceResult:
        """Llamada individual a Google Distance Matrix."""

        query = urllib.parse.urlencode(
            {
                "origins": f"{origin_lat:.6f},{origin_lng:.6f}",
                "destinations": f"{dest_lat:.6f},{dest_lng:.6f}",
                "mode": "driving",
                "traffic_model": "best_guess",
                "departure_time": "now",
                "key": self._api_key,
            }
        )

        request = urllib.request.Request(f"{GOOGLE_DISTANCE_MATRIX_URL}?{query}")
        self._api_calls += 1
        started = time.monotonic()

        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))

        elapsed_ms = int((time.monotonic() - started) * 1000)

        if body.get("status") != "OK":
            raise RuntimeError(
                f"Distance Matrix status={body.get('status')}: {body.get('error_message', '')}"
            )

        element = body["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            raise RuntimeError(f"Distance Matrix element status={element.get('status')}")

        distance_m = element["distance"]["value"]
        duration_s = element["duration"]["value"]
        traffic_s = element.get("duration_in_traffic", element["duration"])["value"]

        logger.info(
            "distance.api.success origin=(%s,%s) dest=(%s,%s) km=%.2f min=%.1f elapsed_ms=%s",
            origin_lat,
            origin_lng,
            dest_lat,
            dest_lng,
            distance_m / 1000.0,
            traffic_s / 60.0,
            elapsed_ms,
        )

        return DistanceResult(
            distance_km=round(distance_m / 1000.0, 2),
            duration_minutes=round(duration_s / 60.0, 1),
            duration_in_traffic_minutes=round(traffic_s / 60.0, 1),
            source="api",
        )

    def _call_distance_matrix_batch(
        self,
        origins: List[Tuple[float, float]],
        destination: Tuple[float, float],
    ) -> List[DistanceResult]:
        """Llamada batch a Google Distance Matrix (multiples origenes, 1 destino)."""

        origins_str = "|".join(f"{lat:.6f},{lng:.6f}" for lat, lng in origins)
        query = urllib.parse.urlencode(
            {
                "origins": origins_str,
                "destinations": f"{destination[0]:.6f},{destination[1]:.6f}",
                "mode": "driving",
                "traffic_model": "best_guess",
                "departure_time": "now",
                "key": self._api_key,
            }
        )

        request = urllib.request.Request(f"{GOOGLE_DISTANCE_MATRIX_URL}?{query}")
        self._api_calls += 1
        started = time.monotonic()

        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))

        elapsed_ms = int((time.monotonic() - started) * 1000)

        if body.get("status") != "OK":
            raise RuntimeError(
                f"Distance Matrix batch status={body.get('status')}: {body.get('error_message', '')}"
            )

        results: List[DistanceResult] = []
        for row in body["rows"]:
            element = row["elements"][0]
            if element.get("status") != "OK":
                # Fallback individual para elementos fallidos
                results.append(
                    DistanceResult(
                        distance_km=0.0,
                        duration_minutes=0.0,
                        duration_in_traffic_minutes=0.0,
                        source="api_element_failed",
                    )
                )
                continue

            distance_m = element["distance"]["value"]
            duration_s = element["duration"]["value"]
            traffic_s = element.get("duration_in_traffic", element["duration"])["value"]
            results.append(
                DistanceResult(
                    distance_km=round(distance_m / 1000.0, 2),
                    duration_minutes=round(duration_s / 60.0, 1),
                    duration_in_traffic_minutes=round(traffic_s / 60.0, 1),
                    source="api",
                )
            )

        logger.info(
            "distance.batch.success origins=%s elapsed_ms=%s",
            len(origins),
            elapsed_ms,
        )
        return results

    def _haversine_fallback(
        self,
        origin_lat: float,
        origin_lng: float,
        dest_lat: float,
        dest_lng: float,
    ) -> DistanceResult:
        """Calcula distancia con haversine cuando la API no esta disponible."""

        from utils.geo import haversine_km, travel_minutes

        distance_km = haversine_km(origin_lat, origin_lng, dest_lat, dest_lng)
        duration_min = travel_minutes(distance_km)

        return DistanceResult(
            distance_km=round(distance_km, 2),
            duration_minutes=round(duration_min, 1),
            duration_in_traffic_minutes=round(duration_min * 1.3, 1),  # factor trafico estimado
            source="haversine_fallback",
        )

    def _cache_key(
        self, lat1: float, lng1: float, lat2: float, lng2: float
    ) -> Tuple[float, float, float, float]:
        """Genera cache key redondeando coordenadas para agrupar tramos similares."""

        return (
            round(lat1, CACHE_PRECISION_DECIMALS),
            round(lng1, CACHE_PRECISION_DECIMALS),
            round(lat2, CACHE_PRECISION_DECIMALS),
            round(lng2, CACHE_PRECISION_DECIMALS),
        )


# Singleton global del servicio
_instance: Optional[DistanceService] = None


def get_distance_service() -> DistanceService:
    """Obtiene la instancia singleton del servicio de distancias."""

    global _instance
    if _instance is None:
        _instance = DistanceService()
        if _instance.is_available:
            logger.info("distance_service.initialized with_api_key=true")
        else:
            logger.warning("distance_service.initialized with_api_key=false fallback=haversine")
    return _instance
