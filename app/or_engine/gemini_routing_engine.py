"""Motor de decision basado en Gemini 2.5 Flash para asignacion inteligente."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from or_engine.engine import DecisionEngine
from or_engine.models import DecisionOutcome, PreasignacionPlan, ServicioPlan, TurnoPlan
from services.gemini_client import GeminiClient
from utils.constants import (
    ESTADO_ASIGNADO_FINAL,
    ESTADO_MANUAL,
    ESTADO_URGENTE_GESTIONAR_MANUAL,
    ESTADOS_ACEPTADOS,
    ESTADOS_TERMINALES,
    PREASIGNACION_CONGELADA,
)
from utils.geo import haversine_km, travel_minutes
from utils.time_utils import format_datetime, now_bogota

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Eres el motor de asignacion de servicios vehiculares RVE de Seguros Bolivar.
Tu UNICA funcion es elegir el MEJOR tecnico de la lista de CANDIDATOS para atender el servicio nuevo.

## REGLAS ESTRICTAS ANTI-ALUCINACION
- SOLO puedes elegir un id_turno que aparezca en la lista de CANDIDATOS DISPONIBLES.
- NUNCA inventes un id_turno, nombre, cedula o dato que no este en los candidatos.
- Si ningun candidato cumple las restricciones, responde con id_turno: null.
- NO agregues campos adicionales al JSON de respuesta.
- NO expliques tu razonamiento fuera del campo "razon".

## REGLAS DE PRIORIZACION (en orden de importancia)
1. PROVEEDORES EXCLUSIVOS SUBUTILIZADOS: Si un proveedor exclusivo (es_proveedor_exclusivo=true) tiene menos servicios asignados que otros, PRIORIZALO. La empresa paga por ellos y DEBEN estar trabajando.
2. DISTANCIA AL ORIGEN: Preferir al tecnico MAS CERCANO al punto de origen del servicio (menor distancia_km_al_origen).
3. BALANCEO DE CARGA: No saturar un turno con muchos servicios si hay otro disponible con menos carga (servicios_asignados_actualmente).
4. TIEMPO SIN ASIGNACION: Si hay empate, preferir al tecnico con mayor minutos_sin_recibir_servicio.
5. VIABILIDAD TEMPORAL: El tecnico DEBE poder llegar a tiempo (puede_llegar_a_tiempo=true). Si es false, DESCARTALO.

## RESTRICCIONES DURAS (candidato ya filtrado, pero verifica)
- puede_llegar_a_tiempo DEBE ser true (si es false, no lo elijas).
- distancia_km_al_origen NO puede exceder radio_maximo_km.

## FORMATO DE RESPUESTA (JSON estricto, sin texto adicional)
{"id_turno": "<id exacto del turno elegido de la lista>", "razon": "<maximo 10 palabras>"}

Si NINGUN candidato es viable:
{"id_turno": null, "razon": "<maximo 10 palabras>"}
"""


class GeminiRoutingEngine(DecisionEngine):
    """Evalua servicios usando Gemini 2.5 Flash para seleccion inteligente."""

    def __init__(self) -> None:
        super().__init__()
        self._gemini_client = GeminiClient()
        self._datos_tecnicos_cache: Optional[List[dict]] = None
        self._distance_service = None

    def _get_distance_service(self):
        """Obtiene el servicio de distancias reales (lazy init)."""

        if self._distance_service is None:
            from services.distance_service import get_distance_service
            self._distance_service = get_distance_service()
        return self._distance_service

    def decidir(
        self,
        nuevo_servicio: ServicioPlan,
        servicios: List[ServicioPlan],
        turnos: List[TurnoPlan],
        preasignaciones: List[PreasignacionPlan],
    ) -> DecisionOutcome:
        """Ejecuta la decision final ACEPTAR o RECHAZAR usando Gemini."""

        started = time.monotonic()
        now_value = now_bogota()
        logger.warning(
            "engine.gemini.decidir.start autorizacion=%s servicios=%s turnos=%s preasignaciones=%s",
            nuevo_servicio.autorizacion,
            len(servicios),
            len(turnos),
            len(preasignaciones),
        )

        validation_result = self._validate_basic_constraints(nuevo_servicio, now_value)
        if validation_result is not None:
            return validation_result

        turnos_compatibles = self._candidate_turns(nuevo_servicio, turnos, now_value)
        logger.warning(
            "engine.gemini.decidir.candidates autorizacion=%s compatibles=%s",
            nuevo_servicio.autorizacion,
            len(turnos_compatibles),
        )
        if not turnos_compatibles:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                "No existe turno compatible para departamento, servicio y horario",
            )

        candidates_context = self._build_candidates_context(
            nuevo_servicio,
            turnos_compatibles,
            servicios,
            preasignaciones,
            now_value,
        )

        if not candidates_context:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                "Todos los turnos compatibles exceden el radio maximo",
            )

        try:
            selected_turn_id, razon = self._ask_gemini(
                nuevo_servicio, candidates_context, now_value
            )
        except Exception as exc:
            logger.exception(
                "engine.gemini.decidir.fallback autorizacion=%s error=%s",
                nuevo_servicio.autorizacion,
                str(exc),
            )
            return self._fallback_decision(
                nuevo_servicio, turnos_compatibles, servicios, preasignaciones, candidates_context
            )

        if selected_turn_id is None:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "engine.gemini.decidir.rejected autorizacion=%s razon=%s elapsed_ms=%s",
                nuevo_servicio.autorizacion,
                razon,
                elapsed_ms,
            )
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                razon or "Gemini determino que no hay candidato viable",
            )

        # Anti-alucinacion: verificar que el id_turno elegido existe en los candidatos
        valid_turn_ids = {c["id_turno"] for c in candidates_context}
        if selected_turn_id not in valid_turn_ids:
            logger.error(
                "engine.gemini.decidir.hallucination autorizacion=%s id_turno_gemini=%s valid_ids=%s",
                nuevo_servicio.autorizacion,
                selected_turn_id,
                list(valid_turn_ids)[:10],
            )
            return self._fallback_decision(
                nuevo_servicio, turnos_compatibles, servicios, preasignaciones, candidates_context
            )

        turno_elegido = next(
            (turno for turno in turnos_compatibles if turno.id_turno == selected_turn_id),
            None,
        )
        if turno_elegido is None:
            logger.error(
                "engine.gemini.decidir.invalid_turn autorizacion=%s id_turno_gemini=%s",
                nuevo_servicio.autorizacion,
                selected_turn_id,
            )
            return self._fallback_decision(
                nuevo_servicio, turnos_compatibles, servicios, preasignaciones, candidates_context
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "engine.gemini.decidir.accepted autorizacion=%s id_turno=%s razon=%s elapsed_ms=%s",
            nuevo_servicio.autorizacion,
            selected_turn_id,
            razon,
            elapsed_ms,
        )

        assignments = self._build_assignments(
            nuevo_servicio, turno_elegido, servicios, preasignaciones, turnos_compatibles
        )
        return DecisionOutcome(
            autorizacion=nuevo_servicio.autorizacion,
            decision="ACEPTAR",
            razon=f"Aceptado en turno {turno_elegido.id_turno} ({turno_elegido.nombre_conductor}) - {razon}",
            assignments=assignments,
        )

    def _validate_basic_constraints(
        self, nuevo_servicio: ServicioPlan, now_value: datetime
    ) -> Optional[DecisionOutcome]:
        """Valida restricciones basicas antes de consultar Gemini."""

        if not nuevo_servicio.fecha_servicio:
            return DecisionOutcome(
                nuevo_servicio.autorizacion, "RECHAZAR", "FECHA_SERVICIO invalida"
            )

        min_allowed = now_value + timedelta(minutes=self.settings.min_notice_minutes)
        max_allowed = now_value + timedelta(days=self.settings.horizon_days)

        if nuevo_servicio.fecha_servicio < min_allowed:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                f"FECHA_SERVICIO debe ser al menos {self.settings.min_notice_minutes} minutos despues de ahora",
            )
        if nuevo_servicio.fecha_servicio > max_allowed:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                f"FECHA_SERVICIO excede el horizonte de {self.settings.horizon_days} dias",
            )
        return None

    def _load_datos_tecnicos(self) -> Dict[str, dict]:
        """Carga y cachea la pestaña DATOS_TECNICOS indexada por cedula."""

        if self._datos_tecnicos_cache is None:
            try:
                from services.google_sheets import GoogleSheetsRepository

                repository = GoogleSheetsRepository()
                self._datos_tecnicos_cache = repository.list_datos_tecnicos()
            except Exception:
                logger.warning("engine.gemini.datos_tecnicos.load_failed")
                self._datos_tecnicos_cache = []

        result: Dict[str, dict] = {}
        for row in self._datos_tecnicos_cache:
            cedula = row.get("CEDULA_CONDUCTOR", row.get("CEDULA", "")).strip()
            if cedula:
                result[cedula] = row
        return result

    def _build_candidates_context(
        self,
        nuevo_servicio: ServicioPlan,
        turnos_compatibles: List[TurnoPlan],
        servicios: List[ServicioPlan],
        preasignaciones: List[PreasignacionPlan],
        now_value: datetime,
    ) -> List[dict]:
        """Construye el contexto enriquecido de cada candidato para Gemini."""

        assignments_by_auth = self._select_current_assignments(preasignaciones)
        service_count_by_turn: Dict[str, int] = {}
        last_service_time_by_turn: Dict[str, datetime] = {}

        for servicio in servicios:
            if servicio.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio.estado_operacion == ESTADO_URGENTE_GESTIONAR_MANUAL:
                continue
            preasignacion = assignments_by_auth.get(servicio.autorizacion)
            if preasignacion:
                turn_id = preasignacion.id_turno
                service_count_by_turn[turn_id] = service_count_by_turn.get(turn_id, 0) + 1
                if servicio.fecha_servicio:
                    current_last = last_service_time_by_turn.get(turn_id)
                    if current_last is None or servicio.fecha_servicio > current_last:
                        last_service_time_by_turn[turn_id] = servicio.fecha_servicio

        # Enriquecer con datos de la pestaña DATOS_TECNICOS
        datos_tecnicos_by_cedula = self._load_datos_tecnicos()

        # Calcular distancias reales via Google Distance Matrix (batch)
        distance_service = self._get_distance_service()
        origins_for_batch = [
            (turno.punto_inicio.lat, turno.punto_inicio.lng)
            for turno in turnos_compatibles
        ]
        destination = (nuevo_servicio.origen.lat, nuevo_servicio.origen.lng)

        if distance_service.is_available and origins_for_batch:
            distance_results = distance_service.get_distances_batch(origins_for_batch, destination)
        else:
            distance_results = None

        candidates = []
        for idx, turno in enumerate(turnos_compatibles):
            # Usar distancia real si disponible, haversine como fallback
            if distance_results and idx < len(distance_results):
                dist_result = distance_results[idx]
                distancia_km = dist_result.distance_km
                minutos_viaje = dist_result.duration_in_traffic_minutes
                distancia_source = dist_result.source
            else:
                distancia_km = haversine_km(
                    turno.punto_inicio.lat,
                    turno.punto_inicio.lng,
                    nuevo_servicio.origen.lat,
                    nuevo_servicio.origen.lng,
                )
                minutos_viaje = travel_minutes(distancia_km)
                distancia_source = "haversine"

            if distancia_km > self.settings.max_radius_km:
                continue

            servicios_asignados = service_count_by_turn.get(turno.id_turno, 0)
            ultimo_servicio = last_service_time_by_turn.get(turno.id_turno)
            minutos_sin_servicio = 0
            if ultimo_servicio:
                minutos_sin_servicio = int((now_value - ultimo_servicio).total_seconds() / 60)
            elif turno.fecha_inicio_turno:
                minutos_sin_servicio = int(
                    (now_value - turno.fecha_inicio_turno).total_seconds() / 60
                )

            puede_llegar_a_tiempo = True
            if nuevo_servicio.fecha_servicio and turno.punto_inicio.lat != 0.0:
                tiempo_llegada_estimado = now_value + timedelta(minutes=minutos_viaje)
                margen = nuevo_servicio.fecha_servicio - timedelta(
                    minutes=self.settings.buffer_minutes
                )
                if tiempo_llegada_estimado > margen:
                    puede_llegar_a_tiempo = False

            # Datos extendidos del tecnico desde DATOS_TECNICOS
            datos_tecnico = datos_tecnicos_by_cedula.get(turno.cedula_conductor, {})
            info_tecnico = {}
            if datos_tecnico:
                # Incluir todos los campos relevantes para que Gemini tenga contexto completo
                campos_relevantes = [
                    "PROVEEDOR", "TIPO_PROVEEDOR", "ESPECIALIDAD", "ZONA",
                    "EXPERIENCIA", "CALIFICACION", "VEHICULO", "TIPO_VEHICULO",
                    "PLACA", "ESTADO", "OBSERVACIONES", "CIUDAD_BASE",
                ]
                for campo in campos_relevantes:
                    valor = datos_tecnico.get(campo, "").strip()
                    if valor:
                        info_tecnico[campo.lower()] = valor
                # Incluir cualquier campo extra que tenga la pestaña
                for key, value in datos_tecnico.items():
                    normalized_key = key.strip().upper()
                    if (
                        normalized_key not in {"CEDULA_CONDUCTOR", "CEDULA", "NOMBRE_CONDUCTOR", "NOMBRE", "_ROW"}
                        and normalized_key not in [c.upper() for c in info_tecnico]
                        and str(value).strip()
                    ):
                        info_tecnico[key.lower().replace(" ", "_")] = str(value).strip()

            candidate = {
                "id_turno": turno.id_turno,
                "nombre_conductor": turno.nombre_conductor,
                "cedula_conductor": turno.cedula_conductor,
                "proveedor": turno.proveedor,
                "lat_tecnico": round(turno.punto_inicio.lat, 6),
                "lng_tecnico": round(turno.punto_inicio.lng, 6),
                "distancia_km_al_origen": round(distancia_km, 2),
                "minutos_viaje_estimados": round(minutos_viaje, 1),
                "distancia_calculada_con": distancia_source,
                "servicios_asignados_actualmente": servicios_asignados,
                "minutos_sin_recibir_servicio": max(0, minutos_sin_servicio),
                "puede_llegar_a_tiempo": puede_llegar_a_tiempo,
                "inicio_turno": format_datetime(turno.fecha_inicio_turno),
                "fin_turno": format_datetime(turno.fecha_fin_turno),
                "es_proveedor_exclusivo": bool(
                    turno.proveedor and turno.proveedor.strip().upper() != "PROPIO"
                ),
            }

            if info_tecnico:
                candidate["datos_tecnico_extendidos"] = info_tecnico

            candidates.append(candidate)

        return candidates

    def _ask_gemini(
        self,
        nuevo_servicio: ServicioPlan,
        candidates: List[dict],
        now_value: datetime,
    ) -> tuple[Optional[str], str]:
        """Consulta a Gemini para elegir el mejor turno."""

        servicio_context = {
            "autorizacion": nuevo_servicio.autorizacion,
            "lat_origen": round(nuevo_servicio.origen.lat, 6),
            "lng_origen": round(nuevo_servicio.origen.lng, 6),
            "lat_destino": round(nuevo_servicio.destino.lat, 6),
            "lng_destino": round(nuevo_servicio.destino.lng, 6),
            "ciudad_origen": nuevo_servicio.ciudad_origen,
            "ciudad_destino": nuevo_servicio.ciudad_destino,
            "departamento": nuevo_servicio.departamento,
            "tipo_servicio": nuevo_servicio.tipo_servicio,
            "servicio": nuevo_servicio.servicio,
            "fecha_servicio": format_datetime(nuevo_servicio.fecha_servicio),
            "hora_actual": format_datetime(now_value),
            "radio_maximo_km": self.settings.max_radius_km,
        }

        # IDs validos para refuerzo anti-alucinacion
        valid_ids = [c["id_turno"] for c in candidates]

        prompt = f"""{SYSTEM_PROMPT}

SERVICIO:
{json.dumps(servicio_context, ensure_ascii=False)}

CANDIDATOS ({len(candidates)}):
{json.dumps(candidates, ensure_ascii=False)}

IDs VALIDOS: {json.dumps(valid_ids)}

Responde SOLO el JSON:"""

        response_text = self._gemini_client.generate(prompt, temperature=0.05, max_tokens=1024)
        return self._parse_gemini_response(response_text, valid_ids)

    def _parse_gemini_response(
        self, response_text: str, valid_ids: List[str]
    ) -> tuple[Optional[str], str]:
        """Parsea la respuesta JSON de Gemini con validacion anti-alucinacion."""

        try:
            cleaned = response_text.strip()
            # Limpiar markdown code blocks si los hay
            if "```json" in cleaned:
                start = cleaned.index("```json") + 7
                end = cleaned.index("```", start)
                cleaned = cleaned[start:end].strip()
            elif "```" in cleaned:
                start = cleaned.index("```") + 3
                # Skip language tag if on same line
                if "\n" in cleaned[start:start+20]:
                    start = cleaned.index("\n", start) + 1
                end = cleaned.index("```", start)
                cleaned = cleaned[start:end].strip()

            # Buscar el JSON dentro del texto si hay texto extra
            if not cleaned.startswith("{"):
                json_start = cleaned.find("{")
                if json_start >= 0:
                    cleaned = cleaned[json_start:]
            if not cleaned.endswith("}"):
                json_end = cleaned.rfind("}")
                if json_end >= 0:
                    cleaned = cleaned[:json_end + 1]

            result = json.loads(cleaned)
            id_turno = result.get("id_turno")
            razon = result.get("razon", "")

            # Validacion: si devolvio un id_turno, verificar que sea valido
            if id_turno is not None and id_turno not in valid_ids:
                logger.error(
                    "engine.gemini.parse.hallucinated_id id_turno=%s valid_ids=%s",
                    id_turno,
                    valid_ids[:10],
                )
                raise RuntimeError(
                    f"Gemini alucino un id_turno invalido: {id_turno}"
                )

            return id_turno, razon
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.error(
                "engine.gemini.parse_error response=%s error=%s",
                response_text[:300],
                str(exc),
            )
            raise RuntimeError(f"Gemini retorno respuesta no parseable: {response_text[:200]}") from exc

    def _fallback_decision(
        self,
        nuevo_servicio: ServicioPlan,
        turnos_compatibles: List[TurnoPlan],
        servicios: List[ServicioPlan],
        preasignaciones: List[PreasignacionPlan],
        candidates: List[dict],
    ) -> DecisionOutcome:
        """Fallback deterministico si Gemini falla: elige por menor distancia + proveedor exclusivo."""

        logger.warning(
            "engine.gemini.fallback.start autorizacion=%s candidates=%s",
            nuevo_servicio.autorizacion,
            len(candidates),
        )

        viable = [c for c in candidates if c["puede_llegar_a_tiempo"]]
        if not viable:
            viable = candidates

        if not viable:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                "Gemini fallo y no hay candidatos viables para fallback",
            )

        # Fallback inteligente: priorizar exclusivos subutilizados, luego distancia
        viable.sort(
            key=lambda c: (
                not c["es_proveedor_exclusivo"],  # exclusivos primero
                c["distancia_km_al_origen"],  # menor distancia
                c["servicios_asignados_actualmente"],  # menor carga
            )
        )
        best = viable[0]
        turno_elegido = next(
            (t for t in turnos_compatibles if t.id_turno == best["id_turno"]),
            None,
        )

        if turno_elegido is None:
            return DecisionOutcome(
                nuevo_servicio.autorizacion,
                "RECHAZAR",
                "Fallback: turno no encontrado en lista de compatibles",
            )

        assignments = self._build_assignments(
            nuevo_servicio, turno_elegido, servicios, preasignaciones, turnos_compatibles
        )
        return DecisionOutcome(
            autorizacion=nuevo_servicio.autorizacion,
            decision="ACEPTAR",
            razon=f"Aceptado en turno {turno_elegido.id_turno} ({turno_elegido.nombre_conductor}) [fallback inteligente]",
            assignments=assignments,
        )

    def _build_assignments(
        self,
        nuevo_servicio: ServicioPlan,
        turno_elegido: TurnoPlan,
        servicios: List[ServicioPlan],
        preasignaciones: List[PreasignacionPlan],
        turnos_compatibles: List[TurnoPlan],
    ) -> Dict[str, TurnoPlan]:
        """Construye el mapa de asignaciones manteniendo las existentes."""

        assignments: Dict[str, TurnoPlan] = {}
        assignments[nuevo_servicio.autorizacion] = turno_elegido

        assignments_by_auth = self._select_current_assignments(preasignaciones)
        turnos_by_id = {t.id_turno: t for t in turnos_compatibles}
        now_value = now_bogota()

        for servicio in servicios:
            if servicio.autorizacion == nuevo_servicio.autorizacion:
                continue
            if servicio.estado_operacion in ESTADOS_TERMINALES:
                continue
            if servicio.estado_operacion == ESTADO_URGENTE_GESTIONAR_MANUAL:
                continue
            if self._is_historical_service(servicio, now_value):
                continue
            preasignacion = assignments_by_auth.get(servicio.autorizacion)
            if not preasignacion:
                continue
            turno = turnos_by_id.get(preasignacion.id_turno)
            if turno:
                assignments[servicio.autorizacion] = turno

        return assignments

    def _select_current_assignments(
        self, preasignaciones: List[PreasignacionPlan]
    ) -> Dict[str, PreasignacionPlan]:
        """Selecciona la preasignacion vigente por autorizacion."""

        result: Dict[str, PreasignacionPlan] = {}
        for p in preasignaciones:
            if p.estado_preasignacion in ("CANCELADA",):
                continue
            existing = result.get(p.autorizacion)
            if existing is None:
                result[p.autorizacion] = p
            elif p.estado_preasignacion == PREASIGNACION_CONGELADA:
                result[p.autorizacion] = p
            elif existing.estado_preasignacion != PREASIGNACION_CONGELADA:
                if (p.fecha_preasignacion or datetime.min) > (
                    existing.fecha_preasignacion or datetime.min
                ):
                    result[p.autorizacion] = p
        return result
