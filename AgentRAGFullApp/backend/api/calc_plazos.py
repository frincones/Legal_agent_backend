"""POST /v1/calc/plazos · Sprint 4 · S4-06.

Calculadora determinística de plazos procesales colombianos.

Tipos de plazo soportados (CGP/CST/CPP):
  · contestacion_demanda      → 20 días hábiles (CGP Art. 369)
  · traslado                  → 10 días hábiles (CGP Art. 110)
  · apelacion                 → 3 días hábiles (CGP Art. 322)
  · casacion                  → 30 días hábiles (CGP Art. 339)
  · reposicion                → 3 días hábiles (CGP Art. 318)
  · contestacion_tutela       → 2 días hábiles (Decreto 2591/91 Art. 19)
  · derecho_peticion_general  → 15 días hábiles (Ley 1755/2015 Art. 14)
  · derecho_peticion_consulta → 30 días hábiles (Ley 1755/2015 Art. 14)
  · derecho_peticion_documentos → 10 días hábiles (Ley 1755/2015 Art. 14)
  · prescripcion_laboral      → 3 años calendario (CST Art. 488)
  · personalizado             → días configurables

Sin alucinación: lee festivos y vacancias desde `legal_constants` y
salta esos días al contar hábiles.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/calc", tags=["calc"])


PLAZO_DEFAULTS: dict[str, dict] = {
    "contestacion_demanda": {
        "dias": 20, "tipo_dia": "habil",
        "fundamento": "Art. 369 CGP (Código General del Proceso)",
    },
    "traslado": {
        "dias": 10, "tipo_dia": "habil",
        "fundamento": "Art. 110 CGP",
    },
    "apelacion": {
        "dias": 3, "tipo_dia": "habil",
        "fundamento": "Art. 322 CGP",
    },
    "casacion": {
        "dias": 30, "tipo_dia": "habil",
        "fundamento": "Art. 339 CGP",
    },
    "reposicion": {
        "dias": 3, "tipo_dia": "habil",
        "fundamento": "Art. 318 CGP",
    },
    "contestacion_tutela": {
        "dias": 2, "tipo_dia": "habil",
        "fundamento": "Art. 19 Decreto 2591 de 1991",
    },
    "derecho_peticion_general": {
        "dias": 15, "tipo_dia": "habil",
        "fundamento": "Art. 14 Ley 1755 de 2015",
    },
    "derecho_peticion_consulta": {
        "dias": 30, "tipo_dia": "habil",
        "fundamento": "Art. 14 Ley 1755 de 2015 (consulta)",
    },
    "derecho_peticion_documentos": {
        "dias": 10, "tipo_dia": "habil",
        "fundamento": "Art. 14 Ley 1755 de 2015 (documentos)",
    },
    "prescripcion_laboral": {
        "dias": 3 * 365, "tipo_dia": "calendario",
        "fundamento": "Art. 488 CST · 3 años calendario",
    },
}


class PlazoRequest(BaseModel):
    fecha_inicio: date
    tipo_plazo: str = Field(..., description="Una de las claves de PLAZO_DEFAULTS o 'personalizado'.")
    # Para tipo_plazo='personalizado'
    dias_personalizados: Optional[int] = Field(None, ge=1, le=3650)
    tipo_dia_personalizado: Optional[str] = Field(None, pattern="^(habil|calendario)$")
    incluir_dia_inicio: bool = False
    matter_id: Optional[str] = None
    notas: Optional[str] = None


class PlazoResponse(BaseModel):
    fecha_inicio: date
    fecha_vence: date
    dias_total: int
    tipo_dia: str
    tipo_plazo: str
    fundamento: str
    detalle_dias: list[dict] = Field(default_factory=list)  # día a día con label
    festivos_excluidos: list[date] = Field(default_factory=list)
    vacancias_excluidas: list[dict] = Field(default_factory=list)
    desglose_legible: str
    saved_id: Optional[str] = None


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


async def _load_calendar_year(conn, year: int) -> tuple[set[date], list[dict]]:
    """Reads festivos + vacancias for the given year from legal_constants.
    Returns (set_of_holiday_dates, list_of_vacancia_ranges)."""
    rows = await conn.fetch(
        """
        select key, value_jsonb from legal_constants
         where (key = $1 or key = $2)
           and effective_from <= $3::date
        """,
        f"festivos_nacionales_{year}",
        f"vacancia_judicial_{year}",
        date(year, 12, 31),
    )
    festivos: set[date] = set()
    vacancias: list[dict] = []
    for r in rows:
        key = r["key"]
        v = r["value_jsonb"]
        if not v:
            continue
        if key.startswith("festivos_nacionales_"):
            for d in v:
                try:
                    festivos.add(date.fromisoformat(d))
                except Exception:
                    pass
        elif key.startswith("vacancia_judicial_"):
            for span in v:
                try:
                    vacancias.append({
                        "label": span.get("label", ""),
                        "from": date.fromisoformat(span["from"]),
                        "to": date.fromisoformat(span["to"]),
                    })
                except Exception:
                    pass
    return festivos, vacancias


def _is_in_vacancia(d: date, vacancias: list[dict]) -> Optional[dict]:
    for v in vacancias:
        if v["from"] <= d <= v["to"]:
            return v
    return None


@router.post("/plazos", response_model=PlazoResponse)
async def calc_plazos(
    body: PlazoRequest,
    principal: Principal = Depends(get_current_firm),
):
    cfg = PLAZO_DEFAULTS.get(body.tipo_plazo)
    if cfg is None:
        if body.tipo_plazo != "personalizado":
            raise HTTPException(400, f"tipo_plazo desconocido: {body.tipo_plazo}")
        if not body.dias_personalizados or not body.tipo_dia_personalizado:
            raise HTTPException(400, "personalizado requiere dias_personalizados y tipo_dia_personalizado")
        dias = body.dias_personalizados
        tipo_dia = body.tipo_dia_personalizado
        fundamento = "Plazo personalizado · sin fundamento legal preestablecido"
    else:
        dias = int(cfg["dias"])
        tipo_dia = cfg["tipo_dia"]
        fundamento = cfg["fundamento"]

    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        # Load calendar for years that may be touched by the count.
        years_needed = {body.fecha_inicio.year, body.fecha_inicio.year + 1, body.fecha_inicio.year + 2}
        festivos: set[date] = set()
        vacancias: list[dict] = []
        for y in sorted(years_needed):
            f_y, v_y = await _load_calendar_year(conn, y)
            festivos |= f_y
            vacancias.extend(v_y)

    # Walk day-by-day. For "habil" we skip weekends + festivos + vacancias.
    detalle: list[dict] = []
    festivos_used: set[date] = set()
    vacancias_used: list[dict] = []
    counted = 0
    cursor = body.fecha_inicio
    if not body.incluir_dia_inicio:
        cursor += timedelta(days=1)
    # Hard cap: 6 years to avoid infinite loops with bad input.
    max_iter = 365 * 6
    iter_count = 0
    while counted < dias and iter_count < max_iter:
        iter_count += 1
        is_weekend = cursor.weekday() >= 5  # Sat=5, Sun=6
        is_festivo = cursor in festivos
        v_in = _is_in_vacancia(cursor, vacancias)
        if tipo_dia == "habil":
            if is_weekend or is_festivo or v_in:
                if is_festivo:
                    festivos_used.add(cursor)
                if v_in:
                    if v_in not in vacancias_used:
                        vacancias_used.append(v_in)
                detalle.append({
                    "fecha": cursor.isoformat(),
                    "label": (
                        "Fin de semana" if is_weekend
                        else "Festivo" if is_festivo
                        else f"Vacancia · {v_in['label']}" if v_in else ""
                    ),
                    "cuenta": False,
                })
            else:
                counted += 1
                detalle.append({
                    "fecha": cursor.isoformat(),
                    "label": f"Día hábil {counted}",
                    "cuenta": True,
                })
        else:  # calendario
            counted += 1
            detalle.append({
                "fecha": cursor.isoformat(),
                "label": f"Día calendario {counted}",
                "cuenta": True,
            })
        if counted >= dias:
            break
        cursor += timedelta(days=1)

    fecha_vence = cursor
    desglose = (
        f"Plazo de {dias} día{'s' if dias != 1 else ''} {tipo_dia}{' (saltando fines de semana, festivos y vacancia judicial)' if tipo_dia == 'habil' else ''}.\n"
        f"Inicio: {body.fecha_inicio.isoformat()} (incluye día inicio: {'sí' if body.incluir_dia_inicio else 'no'}).\n"
        f"Vence: {fecha_vence.isoformat()}.\n"
        f"Fundamento: {fundamento}."
    )

    # Persist if matter_id provided.
    saved_id: Optional[str] = None
    if body.matter_id:
        from utils.db import get_storage as _get
        st = await _get()
        async with st.pool.acquire() as conn:
            try:
                row = await conn.fetchrow(
                    """
                    insert into calc_plazos (
                      id, firm_id, matter_id, user_id,
                      fecha_inicio, fecha_vence, dias_total, tipo_dia,
                      tipo_plazo, fundamento, payload, created_at
                    )
                    values (
                      gen_random_uuid(), $1::uuid, $2::uuid, $3::uuid,
                      $4::date, $5::date, $6, $7, $8, $9, $10::jsonb, now()
                    )
                    returning id
                    """,
                    principal.firm_id, body.matter_id, principal.user_id,
                    body.fecha_inicio, fecha_vence, dias, tipo_dia,
                    body.tipo_plazo, fundamento,
                    json.dumps({
                        "festivos": [f.isoformat() for f in sorted(festivos_used)],
                        "vacancias": [
                            {"label": v["label"], "from": v["from"].isoformat(), "to": v["to"].isoformat()}
                            for v in vacancias_used
                        ],
                        "incluir_dia_inicio": body.incluir_dia_inicio,
                        "notas": body.notas,
                    }),
                )
                saved_id = str(row["id"]) if row else None
            except Exception as e:
                # Tabla calc_plazos puede no existir aún · no bloquear el cálculo.
                logger.info("calc_plazos persist skipped: %s", e)

    return PlazoResponse(
        fecha_inicio=body.fecha_inicio,
        fecha_vence=fecha_vence,
        dias_total=dias,
        tipo_dia=tipo_dia,
        tipo_plazo=body.tipo_plazo,
        fundamento=fundamento,
        detalle_dias=detalle,
        festivos_excluidos=sorted(festivos_used),
        vacancias_excluidas=[
            {"label": v["label"], "from": v["from"].isoformat(), "to": v["to"].isoformat()}
            for v in vacancias_used
        ],
        desglose_legible=desglose,
        saved_id=saved_id,
    )
