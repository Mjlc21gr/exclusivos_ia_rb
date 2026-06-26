"""Vista operativa de turnos y asignaciones desde Sheets."""

from __future__ import annotations

import html
import json
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from or_engine.models import Coordenadas, PreasignacionPlan, ServicioPlan, TurnoPlan
from utils.config import get_settings
from utils.constants import (
    ESTADO_ASIGNADO_FINAL,
    ESTADO_ACEPTADO_RVE,
    ESTADO_CANCELADO,
    ESTADO_COMPLETADO,
    ESTADO_PREASIGNADO,
    ESTADO_RECHAZADO_RVE,
    ESTADO_URGENTE_GESTIONAR_MANUAL,
    ESTADOS_MANUALES,
    PREASIGNACION_CANCELADA,
)
from utils.geo import haversine_km
from utils.time_utils import now_bogota, parse_datetime

if TYPE_CHECKING:
    from services.google_sheets import DispatchSnapshot


def parse_dashboard_date(value: str | None):
    if not value:
        return now_bogota().date()
    parsed = parse_datetime(value)
    if parsed:
        return parsed.date()
    return datetime.strptime(value, "%Y-%m-%d").date()


def build_turnos_dashboard_data(
    snapshot: DispatchSnapshot,
    target_date,
    departamento: str = "",
    ciudad: str = "",
    servicio_filter: str = "",
    tipo_servicio_filter: str = "",
) -> dict:
    """Construye un snapshot serializable de turnos del dia."""

    settings = get_settings()
    day_start = datetime.combine(target_date, time.min, tzinfo=now_bogota().tzinfo)
    day_end = day_start + timedelta(days=1)
    departamento = departamento.strip().upper()
    ciudad = ciudad.strip().upper()
    servicio_filter = servicio_filter.strip().upper()
    tipo_servicio_filter = tipo_servicio_filter.strip().upper()

    turnos = [
        turno
        for turno in snapshot.turnos
        if _turno_matches(turno, day_start, day_end, departamento, servicio_filter, tipo_servicio_filter)
    ]
    if turnos:
        timeline_start = min(turno.fecha_inicio_turno for turno in turnos if turno.fecha_inicio_turno) - timedelta(hours=4)
        timeline_end = max(turno.fecha_fin_turno for turno in turnos if turno.fecha_fin_turno) + timedelta(hours=4)
    else:
        timeline_start = datetime.combine(target_date, time(18, 0), tzinfo=now_bogota().tzinfo)
        timeline_end = timeline_start + timedelta(hours=12)
    turnos_by_id = {turno.id_turno: turno for turno in turnos}
    vigente_by_auth = _select_current_preasignaciones(snapshot.preasignaciones)
    services_by_turn: Dict[str, List[dict]] = {turno.id_turno: [] for turno in turnos}

    excluded_states = {ESTADO_CANCELADO, ESTADO_RECHAZADO_RVE}

    historical_turns: Dict[str, TurnoPlan] = {}

    for servicio in snapshot.servicios:
        if servicio.estado_operacion in excluded_states:
            continue
        if not _service_matches_day(servicio, timeline_start, timeline_end):
            continue
        id_turno = _assigned_turn_id(servicio, vigente_by_auth)
        turno = turnos_by_id.get(id_turno)
        if not turno:
            turno = _historical_service_turn(servicio, timeline_start, settings.onsite_minutes)
            if departamento and turno.departamento.strip().upper() != departamento:
                continue
            if servicio_filter and turno.servicio.strip().upper() != servicio_filter:
                continue
            if tipo_servicio_filter and turno.tipo_servicio.strip().upper() != tipo_servicio_filter:
                continue
            id_turno = turno.id_turno
            if id_turno not in turnos_by_id:
                turnos_by_id[id_turno] = turno
                services_by_turn[id_turno] = []
                historical_turns[id_turno] = turno
        if ciudad and ciudad not in {
            servicio.ciudad_origen.strip().upper(),
            servicio.ciudad_destino.strip().upper(),
        }:
            continue
        if servicio_filter and servicio.servicio.strip().upper() != servicio_filter:
            continue
        if tipo_servicio_filter and servicio.tipo_servicio.strip().upper() != tipo_servicio_filter:
            continue
        services_by_turn[id_turno].append(
            _service_payload(
                servicio=servicio,
                turno=turno,
                preasignacion=vigente_by_auth.get(servicio.autorizacion),
                average_speed_kmh=settings.average_speed_kmh,
                onsite_minutes=settings.onsite_minutes,
            )
        )

    turn_payloads = []
    all_turnos = list(turnos) + list(historical_turns.values())
    for turno in sorted(all_turnos, key=lambda item: (item.fecha_inicio_turno or day_start, item.id_turno)):
        asignaciones = sorted(
            services_by_turn.get(turno.id_turno, []),
            key=lambda item: (item["fecha_servicio"] or "", item["orden_en_ruta"], item["autorizacion"]),
        )
        if ciudad and not asignaciones:
            continue
        turn_payloads.append(_turno_payload(turno, asignaciones))

    stats = _stats(turn_payloads)
    return {
        "fecha": target_date.isoformat(),
        "filtros": {
            "departamento": departamento,
            "ciudad": ciudad,
            "servicio": servicio_filter,
            "tipo_servicio": tipo_servicio_filter,
        },
        "opciones": _filter_options(snapshot),
        "stats": stats,
        "timeline": {
            "inicio": _iso(timeline_start),
            "fin": _iso(timeline_end),
            "start_ts": int(timeline_start.timestamp()),
            "end_ts": int(timeline_end.timestamp()),
        },
        "turnos": turn_payloads,
    }


def _filter_options(snapshot: DispatchSnapshot) -> dict:
    return {
        "departamentos": _unique_sorted(turno.departamento for turno in snapshot.turnos),
        "ciudades": _unique_sorted(
            value
            for servicio in snapshot.servicios
            for value in [servicio.ciudad_origen, servicio.ciudad_destino]
        ),
        "servicios": _unique_sorted(turno.servicio for turno in snapshot.turnos),
        "tipos_servicio": _unique_sorted(turno.tipo_servicio for turno in snapshot.turnos),
    }


def _unique_sorted(values: Iterable[str]) -> List[str]:
    return sorted({value.strip().upper() for value in values if value and value.strip()})


def _select_options(values: Iterable[str], selected: str, empty_label: str) -> str:
    selected = selected.strip().upper()
    options = [f'<option value="">{html.escape(empty_label)}</option>']
    for value in values:
        escaped = html.escape(value)
        selected_attr = " selected" if value == selected else ""
        options.append(f'<option value="{escaped}"{selected_attr}>{escaped}</option>')
    return "".join(options)


def render_turnos_dashboard_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    safe_payload = html.escape(payload, quote=False)
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RVE - Turnos del dia</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {{
      --bg: #f6f8fb;
      --panel: #ffffff;
      --line: #d9e0ea;
      --text: #172033;
      --muted: #617089;
      --manual: #8b5cf6;
      --manual-missing: #f59e0b;
      --urgent: #dc2626;
      --final: #0f766e;
      --pre: #2563eb;
      --completed: #6b7280;
      --other: #64748b;
      --shift: #16a34a;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, system-ui, -apple-system, Segoe UI, sans-serif; color: var(--text); background: var(--bg); }}
    header {{ padding: 16px 20px; border-bottom: 1px solid var(--line); background: var(--panel); position: sticky; top: 0; z-index: 20; }}
    h1 {{ margin: 0 0 10px; font-size: 20px; font-weight: 700; }}
    form {{ display: grid; grid-template-columns: 150px repeat(4, minmax(150px, 1fr)) auto; gap: 8px; align-items: end; }}
    label {{ display: grid; gap: 4px; font-size: 12px; color: var(--muted); }}
    input, select {{ width: 100%; height: 34px; border: 1px solid var(--line); border-radius: 6px; padding: 0 9px; color: var(--text); background: #fff; }}
    button {{ height: 34px; border: 0; border-radius: 6px; padding: 0 14px; background: #172033; color: white; font-weight: 650; cursor: pointer; }}
    main {{ display: grid; grid-template-columns: minmax(680px, 1.08fr) minmax(460px, .92fr); gap: 12px; padding: 12px; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; padding: 12px; }}
    .metric {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px; }}
    .metric strong {{ display: block; font-size: 22px; }}
    .metric span {{ color: var(--muted); font-size: 12px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; min-height: 520px; }}
    .panel h2 {{ margin: 0; padding: 12px; font-size: 15px; border-bottom: 1px solid var(--line); }}
    .gantt {{ padding: 12px; overflow: auto; max-height: calc(100vh - 190px); }}
    .turn {{ border-bottom: 1px solid var(--line); padding: 12px 0 18px; min-width: 980px; }}
    .turn-head {{ display: flex; justify-content: space-between; gap: 12px; margin-bottom: 8px; }}
    .turn-head strong {{ font-size: 13px; }}
    .turn-head span {{ color: var(--muted); font-size: 12px; }}
    .axis {{ position: relative; height: 18px; margin: 0 0 4px; color: var(--muted); font-size: 11px; }}
    .tick {{ position: absolute; top: 0; transform: translateX(-50%); }}
    .bar {{ position: relative; height: 58px; border-radius: 6px; background: #eef2f7; overflow: hidden; }}
    .bar::before {{ content: ""; position: absolute; inset: 0; background: repeating-linear-gradient(to right, transparent 0, transparent calc(8.333% - 1px), rgba(23,32,51,.08) calc(8.333% - 1px), rgba(23,32,51,.08) 8.333%); pointer-events: none; }}
    .shift-band {{ position: absolute; top: 5px; height: 48px; border-radius: 5px; background: rgba(22, 163, 74, .13); border: 1px solid rgba(22, 163, 74, .35); color: #166534; font-size: 10px; font-weight: 750; display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; padding: 3px 6px; pointer-events: none; }}
    .shift-band span {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .item {{ position: absolute; top: 13px; height: 36px; min-width: 34px; border: 2px solid transparent; border-radius: 5px; color: white; font-size: 11px; padding: 4px 7px; cursor: pointer; display: grid; align-content: center; gap: 1px; overflow: hidden; z-index: 3; }}
    .item b, .item span {{ display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .item.selected {{ border-color: #111827; box-shadow: 0 0 0 2px white inset; }}
    .item.MANUAL {{ background: var(--manual); }}
    .item.MANUAL_SIN_UBICACION {{ background: var(--manual-missing); color: #111827; }}
    .item.URGENTE_MANUAL {{ background: var(--urgent); }}
    .item.ASIGNADO_FINAL {{ background: var(--final); }}
    .item.PREASIGNADO {{ background: var(--pre); }}
    .item.COMPLETADO {{ background: var(--completed); }}
    .item.OTRO {{ background: var(--other); }}
    .legend {{ display: flex; gap: 10px; flex-wrap: wrap; padding: 0 12px 12px; color: var(--muted); font-size: 12px; }}
    .dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; margin-right: 5px; }}
    .map-layout {{ display: grid; grid-template-columns: 240px minmax(260px, 1fr); gap: 0; border-bottom: 1px solid var(--line); }}
    #map {{ height: calc(100vh - 258px); min-height: 460px; }}
    .detail {{ padding: 12px; display: grid; gap: 8px; font-size: 13px; }}
    .detail h3 {{ margin: 0; font-size: 14px; }}
    .detail-card {{ display: grid; gap: 8px; }}
    .detail-meta {{ color: var(--muted); font-size: 12px; }}
    .detail-route {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
    .detail-place {{ border: 1px solid var(--line); border-radius: 6px; padding: 8px; }}
    .detail-place b {{ display: block; margin-bottom: 4px; }}
    .detail-warning {{ color: #92400e; background: #fffbeb; border: 1px solid #fbbf24; border-radius: 6px; padding: 7px 8px; }}
    .route-list {{ display: grid; align-content: start; gap: 6px; padding: 12px; max-height: calc(100vh - 258px); min-height: 460px; overflow: auto; border-right: 1px solid var(--line); background: #f8fafc; }}
    .route-option {{ display: flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 6px; padding: 7px; font-size: 12px; cursor: pointer; background: #fff; }}
    .route-option input {{ width: 16px; height: 16px; }}
    .route-option .warn {{ color: #92400e; font-weight: 700; }}
    .map-node {{ display: grid; place-items: center; border-radius: 999px; border: 2px solid #fff; box-shadow: 0 1px 5px rgba(15,23,42,.32); font-weight: 800; line-height: 1; }}
    .map-node.base {{ width: 22px; height: 22px; background: #111827; color: #fff; font-size: 15px; }}
    .map-node.origin {{ width: 28px; height: 28px; color: #fff; font-size: 11px; }}
    .map-node.destination {{ width: 28px; height: 28px; background: #fff; font-size: 11px; border-width: 3px; }}
    .flow-arrow {{ display: grid; place-items: center; width: 24px; height: 24px; border-radius: 999px; background: #fff; border: 2px solid currentColor; box-shadow: 0 1px 5px rgba(15,23,42,.22); font-size: 17px; font-weight: 900; line-height: 1; }}
    .flow-arrow.move {{ width: 20px; height: 20px; border-width: 1px; font-size: 13px; opacity: .88; }}
    .segment-label {{ padding: 1px 4px; border-radius: 999px; background: rgba(255,255,255,.55); border: 0; box-shadow: none; font-size: 9px; font-weight: 750; white-space: nowrap; opacity: .42; }}
    .service-popover {{ position: fixed; z-index: 1000; width: min(360px, calc(100vw - 24px)); background: #fff; border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 14px 36px rgba(15,23,42,.22); padding: 10px; display: none; font-size: 12px; }}
    .service-popover h3 {{ margin: 0 0 6px; font-size: 13px; }}
    .service-popover .meta {{ color: var(--muted); margin-bottom: 8px; }}
    .service-popover .places {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
    .service-popover .place {{ border: 1px solid var(--line); border-radius: 6px; padding: 7px; }}
    .service-popover b {{ display: block; margin-bottom: 3px; }}
    .empty {{ color: var(--muted); padding: 20px; }}
    @media (max-width: 980px) {{
      form {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
      main {{ grid-template-columns: 1fr; }}
      .summary {{ grid-template-columns: repeat(2, 1fr); }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Turnos y asignaciones RVE</h1>
    <form method="get">
      <input type="hidden" name="clave" value="">
      <label>Fecha<input type="date" name="fecha" value="{html.escape(data['fecha'])}"></label>
      <label>Departamento<select name="departamento">{_select_options(data['opciones']['departamentos'], data['filtros']['departamento'], "Todos")}</select></label>
      <label>Ciudad<select name="ciudad">{_select_options(data['opciones']['ciudades'], data['filtros']['ciudad'], "Todas")}</select></label>
      <label>Servicio<select name="servicio">{_select_options(data['opciones']['servicios'], data['filtros']['servicio'], "Todos")}</select></label>
      <label>Tipo servicio<select name="tipo_servicio">{_select_options(data['opciones']['tipos_servicio'], data['filtros']['tipo_servicio'], "Todos")}</select></label>
      <button type="submit">Filtrar</button>
    </form>
  </header>
  <section class="summary" id="summary"></section>
  <main>
    <section class="panel">
      <h2>Gantt por turno</h2>
      <div class="legend">
        <span><i class="dot" style="background:var(--manual)"></i>Manual</span>
        <span><i class="dot" style="background:var(--urgent)"></i>Urgente manual</span>
        <span><i class="dot" style="background:var(--manual-missing)"></i>Manual sin ubicacion</span>
        <span><i class="dot" style="background:var(--final)"></i>Asignacion final</span>
        <span><i class="dot" style="background:var(--pre)"></i>Preasignacion</span>
        <span><i class="dot" style="background:var(--completed)"></i>Completado</span>
      </div>
      <div class="gantt" id="gantt"></div>
    </section>
    <section class="panel">
      <h2>Mapa por tecnico</h2>
      <div class="map-layout">
        <div class="route-list" id="routeList"></div>
        <div id="map"></div>
      </div>
      <div class="detail" id="detail"></div>
    </section>
  </main>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script id="dashboard-data" type="application/json">{safe_payload}</script>
  <div class="service-popover" id="servicePopover"></div>
  <script>
    const data = JSON.parse(document.getElementById("dashboard-data").textContent);
    const colors = {{ MANUAL: "#8b5cf6", MANUAL_SIN_UBICACION: "#f59e0b", URGENTE_MANUAL: "#dc2626", ASIGNADO_FINAL: "#0f766e", PREASIGNADO: "#2563eb", COMPLETADO: "#6b7280", OTRO: "#64748b" }};
    const params = new URLSearchParams(window.location.search);
    const visualKey = params.get("clave") || "";
    const hiddenKey = document.querySelector('input[name="clave"]');
    if (hiddenKey) hiddenKey.value = visualKey;

    function esc(s) {{ return String(s ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#039;"}}[c])); }}
    function clock(ts) {{
      const d = new Date(ts * 1000);
      return d.toLocaleTimeString("es-CO", {{ hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/Bogota" }});
    }}
    function pct(ts) {{ return Math.max(0, Math.min(100, ((ts - timeline.start) / timeline.span) * 100)); }}

    const summary = document.getElementById("summary");
    const stats = data.stats;
    summary.innerHTML = [
      ["Turnos", stats.turnos],
      ["Servicios", stats.servicios],
      ["Finales", stats.asignado_final],
      ["Preasignados", stats.preasignado],
      ["Completados", stats.completado],
      ["Manuales", stats.manual],
      ["Urgentes", stats.urgente_manual],
      ["Sin ubic.", stats.sin_ubicacion]
    ].map(([label, value]) => `<div class="metric"><strong>${{value}}</strong><span>${{label}}</span></div>`).join("");

    const timeline = {{
      start: data.timeline.start_ts,
      end: data.timeline.end_ts,
      span: Math.max(1, data.timeline.end_ts - data.timeline.start_ts)
    }};
    const ticks = [];
    for (let ts = timeline.start; ts <= timeline.end; ts += 3600) ticks.push(ts);

    const serviceByAuth = new Map();
    data.turnos.forEach(t => t.asignaciones.forEach(a => serviceByAuth.set(a.autorizacion, {{ ...a, turno: t }})));

    function renderDetail(item) {{
      const detail = document.getElementById("detail");
      if (!item) {{
        detail.innerHTML = '<span class="empty">Selecciona un servicio en el Gantt o el mapa.</span>';
        return;
      }}
      const warning = item.sin_ubicacion
        ? '<div class="detail-warning">Manual sin latitud/longitud: se muestra en el Gantt, pero no se puede trazar en el mapa.</div>'
        : '';
      detail.innerHTML = `<div class="detail-card">
        <h3>${{esc(item.autorizacion)}} · ${{esc(item.estado)}}</h3>
        <div class="detail-meta">${{esc(item.hora)}} · Turno ${{esc(item.id_turno)}} · ${{esc(item.nombre_conductor)}} · ${{esc(item.duracion_estimada_min)}} min aprox.</div>
        ${{warning}}
        <div class="detail-route">
          <div class="detail-place"><b>Desde</b>${{esc(item.ciudad_origen)}}<br>${{esc(item.direccion_origen)}}</div>
          <div class="detail-place"><b>Hacia</b>${{esc(item.ciudad_destino)}}<br>${{esc(item.direccion_destino)}}</div>
        </div>
      </div>`;
    }}
    function renderPopover(item, anchor) {{
      const popover = document.getElementById("servicePopover");
      if (!item || !anchor) {{
        popover.style.display = "none";
        return;
      }}
      popover.innerHTML = `<h3>${{esc(item.autorizacion)}} · ${{esc(item.estado)}}</h3>
        <div class="meta">${{esc(item.fecha_servicio)}} · Turno ${{esc(item.id_turno)}} · ${{esc(item.nombre_conductor)}}</div>
        <div class="places">
          <div class="place"><b>Origen</b>${{esc(item.ciudad_origen)}}<br>${{esc(item.direccion_origen)}}</div>
          <div class="place"><b>Destino</b>${{esc(item.ciudad_destino)}}<br>${{esc(item.direccion_destino)}}</div>
      </div>`;
      const rect = anchor.getBoundingClientRect();
      const maxLeft = Math.max(12, window.innerWidth - 372);
      const maxTop = Math.max(12, window.innerHeight - 190);
      const left = Math.min(maxLeft, Math.max(12, rect.left));
      const top = Math.min(maxTop, Math.max(12, rect.bottom + 8));
      popover.style.left = `${{left}}px`;
      popover.style.top = `${{top}}px`;
      popover.style.display = "block";
    }}
    document.addEventListener("click", event => {{
      if (!event.target.closest(".item") && !event.target.closest("#servicePopover")) {{
        document.getElementById("servicePopover").style.display = "none";
      }}
    }});
    function selectService(auth, anchor = null) {{
      document.querySelectorAll(".item.selected").forEach(el => el.classList.remove("selected"));
      document.querySelectorAll(".item").forEach(el => {{
        if (el.dataset.auth === auth) el.classList.add("selected");
      }});
      const item = serviceByAuth.get(auth);
      renderDetail(item);
      renderPopover(item, anchor);
    }}

    const gantt = document.getElementById("gantt");
    if (!data.turnos.length) {{
      gantt.innerHTML = '<div class="empty">No hay turnos para los filtros seleccionados.</div>';
    }} else {{
      const axis = `<div class="axis">${{ticks.map(ts => `<span class="tick" style="left:${{pct(ts)}}%">${{clock(ts)}}</span>`).join("")}}</div>`;
      gantt.innerHTML = data.turnos.map(t => {{
        const shiftLeft = pct(t.inicio_ts);
        const shiftWidth = Math.max(1, pct(t.fin_ts) - shiftLeft);
        const shiftBand = `<div class="shift-band" style="left:${{shiftLeft}}%;width:${{shiftWidth}}%" title="Turno ${{esc(t.id_turno)}} · ${{esc(t.inicio)}} - ${{esc(t.fin)}}"><span>Inicio ${{clock(t.inicio_ts)}}</span><span>Fin ${{clock(t.fin_ts)}}</span></div>`;
        const items = t.asignaciones.map(a => {{
          const left = pct(a.start_ts);
          const width = Math.max(3.2, pct(a.end_ts) - left);
          const locationLabel = a.sin_ubicacion ? " · sin ubic." : "";
          return `<div class="item ${{a.categoria}}" style="left:${{left}}%;width:${{width}}%" data-auth="${{esc(a.autorizacion)}}" title="${{esc(a.autorizacion)}} · ${{esc(a.estado)}} · ${{esc(a.hora)}}">
            <b>${{esc(a.hora)}}</b><span>${{esc(a.autorizacion)}}${{locationLabel}}</span>
          </div>`;
        }}).join("");
        return `<div class="turn">
          <div class="turn-head">
            <strong>${{esc(t.id_turno)}} · ${{esc(t.nombre_conductor)}}</strong>
            <span>${{esc(t.departamento)}} · ${{esc(t.inicio)}} - ${{esc(t.fin)}} · ${{t.asignaciones.length}} servicios</span>
          </div>
          ${{axis}}
          <div class="bar">${{shiftBand}}${{items}}</div>
        </div>`;
      }}).join("");
      document.querySelectorAll(".item").forEach(el => el.addEventListener("click", event => {{
        event.stopPropagation();
        selectService(el.dataset.auth, el);
      }}));
    }}

    const defaultCenter = data.turnos.some(t => t.departamento === "ANTIOQUIA") && !data.turnos.some(t => t.departamento === "CUNDINAMARCA")
      ? [6.244, -75.574]
      : [4.67, -74.09];
    const map = L.map("map").setView(defaultCenter, 11);
    L.tileLayer("https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png", {{ maxZoom: 19, attribution: "&copy; OpenStreetMap" }}).addTo(map);
    const bounds = [];
    const overlayLayers = {{}};
    const routeGroups = new Map();

    function validPoint(point) {{ return point && point.lat && point.lng; }}
    function markerIcon(html, className, size=[28, 28]) {{
      return L.divIcon({{
        className: "",
        html,
        iconSize: size,
        iconAnchor: [size[0] / 2, size[1] / 2],
        popupAnchor: [0, -size[1] / 2],
      }});
    }}
    function addBaseMarker(group, point, t) {{
      if (!validPoint(point)) return;
      bounds.push([point.lat, point.lng]);
      L.marker([point.lat, point.lng], {{
        icon: markerIcon('<span class="map-node base">⌂</span>', "base", [22, 22]),
        zIndexOffset: 900,
      }})
        .bindPopup(`<b>Base del tecnico</b><br>${{esc(t.id_turno)}} · ${{esc(t.nombre_conductor)}}<br>${{esc(t.direccion_origen)}}`)
        .addTo(group);
    }}
    function addServiceNode(group, point, role, index, color, item) {{
      if (!validPoint(point)) return;
      bounds.push([point.lat, point.lng]);
      const label = `${{role}}${{index}}`;
      const isOrigin = role === "O";
      const style = isOrigin
        ? `background:${{color}};border-color:#fff;color:#fff`
        : `color:${{color}};border-color:${{color}}`;
      const title = isOrigin ? "Origen" : "Destino";
      const address = isOrigin ? item.direccion_origen : item.direccion_destino;
      const city = isOrigin ? item.ciudad_origen : item.ciudad_destino;
      const marker = L.marker([point.lat, point.lng], {{
        icon: markerIcon(`<span class="map-node ${{isOrigin ? "origin" : "destination"}}" style="${{style}}">${{label}}</span>`, role, [28, 28]),
        zIndexOffset: isOrigin ? 700 : 720,
      }})
        .bindPopup(`<b>${{label}} · ${{title}}</b><br><b>${{esc(item.autorizacion)}}</b> · ${{esc(item.hora)}}<br>${{esc(city)}}<br>${{esc(address)}}`)
        .addTo(group);
      marker.on("click", () => selectService(item.autorizacion));
    }}
    function projectedAngle(from, to) {{
      const zoom = map.getZoom();
      const p1 = map.project(L.latLng(from[0], from[1]), zoom);
      const p2 = map.project(L.latLng(to[0], to[1]), zoom);
      return Math.atan2(p2.y - p1.y, p2.x - p1.x) * 180 / Math.PI;
    }}
    function pointBetween(from, to, ratio) {{
      return [from[0] + ((to[0] - from[0]) * ratio), from[1] + ((to[1] - from[1]) * ratio)];
    }}
    function addFlowArrow(group, from, to, color, isService) {{
      const mid = pointBetween(from, to, .62);
      const angle = projectedAngle(from, to);
      L.marker(mid, {{
        icon: L.divIcon({{
          className: "",
          html: `<span class="flow-arrow ${{isService ? "" : "move"}}" style="color:${{color}};transform:rotate(${{angle}}deg)">➜</span>`,
          iconSize: isService ? [24, 24] : [20, 20],
          iconAnchor: isService ? [12, 12] : [10, 10],
        }}),
        interactive: false,
        zIndexOffset: 850,
      }}).addTo(group);
    }}
    function addSegmentLabel(group, from, to, color, label) {{
      const mid = pointBetween(from, to, .44);
      L.marker(mid, {{
        icon: L.divIcon({{
          className: "",
          html: `<span class="segment-label" style="color:${{color}}">${{esc(label)}}</span>`,
          iconSize: [80, 20],
          iconAnchor: [40, 10],
        }}),
        interactive: false,
        zIndexOffset: 830,
      }}).addTo(group);
    }}
    function addSegment(group, from, to, color, kind, label) {{
      if (!from || !to) return;
      const isService = kind === "service";
      L.polyline([from, to], {{
        color,
        weight: isService ? 6 : 2,
        opacity: isService ? 0.9 : 0.55,
        dashArray: isService ? null : "6 8",
        lineCap: "round",
        lineJoin: "round",
      }}).addTo(group);
      addFlowArrow(group, from, to, color, isService);
      if (label && isService) addSegmentLabel(group, from, to, color, label);
    }}

    data.turnos.forEach(t => {{
      const group = L.layerGroup();
      routeGroups.set(t.id_turno, group);
      const missingCount = t.asignaciones.filter(a => a.sin_ubicacion).length;
      routeGroups.get(t.id_turno).missingCount = missingCount;
      if (validPoint(t.base)) addBaseMarker(group, t.base, t);
      const ordered = [...t.asignaciones].sort((a, b) => String(a.fecha_servicio).localeCompare(String(b.fecha_servicio)) || (a.orden_en_ruta - b.orden_en_ruta) || String(a.autorizacion).localeCompare(String(b.autorizacion)));
      let previous = validPoint(t.base) ? [t.base.lat, t.base.lng] : null;
      let previousLabel = "Base";
      ordered.forEach((a, idx) => {{
        const index = idx + 1;
        const color = colors[a.categoria] || colors.OTRO;
        const origin = validPoint(a.origen) ? [a.origen.lat, a.origen.lng] : null;
        const destination = validPoint(a.destino) ? [a.destino.lat, a.destino.lng] : null;
        if (previous && origin) addSegment(group, previous, origin, color, "move", `${{previousLabel}} → O${{index}}`);
        if (origin && destination) addSegment(group, origin, destination, color, "service", `O${{index}} → D${{index}}`);
        addServiceNode(group, a.origen, "O", index, color, a);
        addServiceNode(group, a.destino, "D", index, color, a);
        previous = destination || origin || previous;
        previousLabel = destination ? `D${{index}}` : `O${{index}}`;
      }});
      group.addTo(map);
      overlayLayers[`${{t.id_turno}} · ${{t.nombre_conductor}}`] = group;
    }});
    const routeList = document.getElementById("routeList");
    routeList.innerHTML = data.turnos.map(t => `<label class="route-option">
      <input type="checkbox" checked data-turn="${{esc(t.id_turno)}}">
      <span><b>${{esc(t.id_turno)}}</b> · ${{esc(t.nombre_conductor)}} · ${{t.asignaciones.length}} servicios ${{t.asignaciones.some(a => a.sin_ubicacion) ? '<span class="warn">sin ubic.</span>' : ''}}</span>
    </label>`).join("");
    routeList.querySelectorAll("input").forEach(input => input.addEventListener("change", () => {{
      const group = routeGroups.get(input.dataset.turn);
      if (!group) return;
      if (input.checked) group.addTo(map); else map.removeLayer(group);
    }}));
    if (bounds.length) map.fitBounds(bounds, {{ padding: [28, 28] }});
    renderDetail(null);
  </script>
</body>
</html>"""


def _turno_matches(
    turno: TurnoPlan,
    day_start: datetime,
    day_end: datetime,
    departamento: str,
    servicio_filter: str,
    tipo_servicio_filter: str,
) -> bool:
    if not turno.fecha_inicio_turno or not turno.fecha_fin_turno:
        return False
    if not (day_start <= turno.fecha_inicio_turno < day_end):
        return False
    if departamento and turno.departamento.strip().upper() != departamento:
        return False
    if servicio_filter and turno.servicio.strip().upper() != servicio_filter:
        return False
    if tipo_servicio_filter and turno.tipo_servicio.strip().upper() != tipo_servicio_filter:
        return False
    return True


def _service_matches_day(servicio: ServicioPlan, day_start: datetime, day_end: datetime) -> bool:
    if not servicio.fecha_servicio:
        return False
    return day_start <= servicio.fecha_servicio < day_end


def _historical_service_turn(
    servicio: ServicioPlan,
    day_start: datetime,
    onsite_minutes: int,
) -> TurnoPlan:
    start = servicio.fecha_servicio or day_start
    end = start + timedelta(minutes=max(onsite_minutes, 1))
    source_id = servicio.id_turno.strip() or servicio.id_turno_preasignado or "SIN_TURNO"
    return TurnoPlan(
        id_turno=f"HIST-SERVICIO-{source_id}",
        cedula_conductor=servicio.cedula_conductor,
        nombre_conductor=servicio.nombre_conductor or "Completados historicos",
        celular_tecnico="",
        proveedor="",
        direccion_origen="Turno historico no disponible en TURNOS_TECNICOS",
        punto_inicio=servicio.origen,
        fecha_inicio_turno=start,
        fecha_fin_turno=end,
        servicio=servicio.servicio,
        tipo_servicio=servicio.tipo_servicio,
        departamento=servicio.departamento,
        correo=servicio.correos,
    )


def _select_current_preasignaciones(
    preasignaciones: Iterable[PreasignacionPlan],
) -> Dict[str, PreasignacionPlan]:
    current: Dict[str, PreasignacionPlan] = {}
    for preasignacion in preasignaciones:
        if preasignacion.estado_preasignacion == PREASIGNACION_CANCELADA:
            continue
        previous = current.get(preasignacion.autorizacion)
        if previous is None or (preasignacion.row_index or 0) >= (previous.row_index or 0):
            current[preasignacion.autorizacion] = preasignacion
    return current


def _assigned_turn_id(
    servicio: ServicioPlan,
    vigente_by_auth: Dict[str, PreasignacionPlan],
) -> str:
    if servicio.estado_operacion in ESTADOS_MANUALES and servicio.id_turno:
        return servicio.id_turno
    preasignacion = vigente_by_auth.get(servicio.autorizacion)
    if preasignacion:
        return preasignacion.id_turno
    return servicio.id_turno


def _service_payload(
    servicio: ServicioPlan,
    turno: TurnoPlan,
    preasignacion: Optional[PreasignacionPlan],
    average_speed_kmh: float,
    onsite_minutes: int,
) -> dict:
    start = servicio.fecha_servicio
    trip_minutes = _trip_minutes(servicio.origen, servicio.destino, average_speed_kmh)
    end = start + timedelta(minutes=onsite_minutes + trip_minutes) if start else None
    sin_ubicacion = not _has_coords(servicio.origen) or not _has_coords(servicio.destino)
    categoria = _category(servicio.estado_operacion, sin_ubicacion)
    return {
        "autorizacion": servicio.autorizacion,
        "caso": servicio.caso,
        "estado": servicio.estado_operacion,
        "categoria": categoria,
        "sin_ubicacion": sin_ubicacion,
        "id_turno": turno.id_turno,
        "cedula_conductor": turno.cedula_conductor,
        "nombre_conductor": turno.nombre_conductor,
        "ciudad_origen": servicio.ciudad_origen,
        "ciudad_destino": servicio.ciudad_destino,
        "departamento": servicio.departamento,
        "servicio": servicio.servicio,
        "tipo_servicio": servicio.tipo_servicio,
        "fecha_servicio": _iso(start),
        "hora": start.strftime("%H:%M") if start else "",
        "start_ts": int(start.timestamp()) if start else None,
        "end_ts": int(end.timestamp()) if end else None,
        "duracion_estimada_min": round(onsite_minutes + trip_minutes, 1),
        "orden_en_ruta": preasignacion.orden_en_ruta if preasignacion else 999,
        "direccion_origen": servicio.direccion_origen,
        "direccion_destino": servicio.direccion_destino,
        "origen": _coords(servicio.origen),
        "destino": _coords(servicio.destino),
    }


def _turno_payload(turno: TurnoPlan, asignaciones: List[dict]) -> dict:
    return {
        "id_turno": turno.id_turno,
        "cedula_conductor": turno.cedula_conductor,
        "nombre_conductor": turno.nombre_conductor,
        "correo": turno.correo,
        "departamento": turno.departamento,
        "servicio": turno.servicio,
        "tipo_servicio": turno.tipo_servicio,
        "inicio": _iso(turno.fecha_inicio_turno),
        "fin": _iso(turno.fecha_fin_turno),
        "inicio_ts": int(turno.fecha_inicio_turno.timestamp()) if turno.fecha_inicio_turno else None,
        "fin_ts": int(turno.fecha_fin_turno.timestamp()) if turno.fecha_fin_turno else None,
        "base": _coords(turno.punto_inicio),
        "direccion_origen": turno.direccion_origen,
        "asignaciones": asignaciones,
    }


def _stats(turnos: List[dict]) -> dict:
    services = [item for turno in turnos for item in turno["asignaciones"]]
    return {
        "turnos": len(turnos),
        "servicios": len(services),
        "manual": sum(1 for item in services if item["categoria"] in {"MANUAL", "MANUAL_SIN_UBICACION"}),
        "urgente_manual": sum(1 for item in services if item["categoria"] == "URGENTE_MANUAL"),
        "sin_ubicacion": sum(1 for item in services if item["sin_ubicacion"]),
        "asignado_final": sum(1 for item in services if item["categoria"] == "ASIGNADO_FINAL"),
        "preasignado": sum(1 for item in services if item["categoria"] == "PREASIGNADO"),
        "completado": sum(1 for item in services if item["categoria"] == "COMPLETADO"),
    }


def _category(estado: str, sin_ubicacion: bool = False) -> str:
    if estado == ESTADO_URGENTE_GESTIONAR_MANUAL:
        return "URGENTE_MANUAL"
    if estado in ESTADOS_MANUALES:
        if sin_ubicacion:
            return "MANUAL_SIN_UBICACION"
        return "MANUAL"
    if estado == ESTADO_ASIGNADO_FINAL:
        return "ASIGNADO_FINAL"
    if estado in {ESTADO_PREASIGNADO, ESTADO_ACEPTADO_RVE}:
        return "PREASIGNADO"
    if estado == ESTADO_COMPLETADO:
        return "COMPLETADO"
    return "OTRO"


def _trip_minutes(origin: Coordenadas, destination: Coordenadas, average_speed_kmh: float) -> float:
    if not _has_coords(origin) or not _has_coords(destination) or average_speed_kmh <= 0:
        return 0.0
    return (haversine_km(origin.lat, origin.lng, destination.lat, destination.lng) / average_speed_kmh) * 60.0


def _has_coords(coords: Coordenadas) -> bool:
    return bool(coords and coords.lat and coords.lng)


def _coords(coords: Coordenadas) -> dict:
    if not _has_coords(coords):
        return {"lat": None, "lng": None}
    return {"lat": coords.lat, "lng": coords.lng}


def _iso(value) -> str:
    return value.isoformat(sep=" ", timespec="minutes") if value else ""
