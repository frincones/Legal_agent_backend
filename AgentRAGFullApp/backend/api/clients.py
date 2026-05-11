"""Clients API · multi-tenant CRUD over `clients` table.

LFPDPPP fields tracked:
  · consent_lfpdppp_at + consent_lfpdppp_version + consent_finalidades[]
  · consent_voice_recording (used to enable voice features per client)
  · arco_requests JSONB log (Acceso, Rectificación, Cancelación, Oposición)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/clients", tags=["clients"])


class ClientCreate(BaseModel):
    tipo: str = Field(pattern="^(persona_natural|persona_juridica)$")
    nombre: str = Field(min_length=2)
    tax_id: Optional[str] = None         # NIT (CO) | RFC (MX) | RUT (CL)
    personal_id: Optional[str] = None    # cédula ciudadanía / CURP / DNI
    email: Optional[str] = None
    telefono: Optional[str] = None
    domicilio: Optional[dict] = None
    vip: bool = False
    consent_lfpdppp: bool = False
    consent_finalidades: list[str] = Field(default_factory=list)
    consent_voice_recording: bool = False
    metadata: dict = Field(default_factory=dict)


class ClientRow(BaseModel):
    id: str
    tipo: str
    nombre: str
    tax_id: Optional[str]
    personal_id: Optional[str]
    email: Optional[str]
    telefono: Optional[str]
    vip: bool
    consent_lfpdppp_at: Optional[datetime]
    consent_voice_recording: bool
    created_at: datetime


def _row(r) -> ClientRow:
    return ClientRow(
        id=str(r["id"]),
        tipo=r["tipo"],
        nombre=r["nombre"],
        tax_id=r["tax_id"],
        personal_id=r["personal_id"],
        email=r["email"],
        telefono=r["telefono"],
        vip=r["vip"],
        consent_lfpdppp_at=r["consent_lfpdppp_at"],
        consent_voice_recording=r["consent_voice_recording"],
        created_at=r["created_at"],
    )


@router.get("/", response_model=list[ClientRow])
async def list_clients(
    principal: Principal = Depends(get_current_firm),
    q: Optional[str] = None,
    limit: int = Query(default=50, le=200),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        if q:
            rows = await conn.fetch(
                """
                select id, tipo, nombre, tax_id, personal_id, email, telefono, vip,
                       consent_at as consent_lfpdppp_at, consent_voice_recording, created_at
                from clients
                where firm_id = $1::uuid and nombre ilike '%' || $2 || '%'
                order by nombre asc
                limit $3
                """,
                principal.firm_id, q, limit,
            )
        else:
            rows = await conn.fetch(
                """
                select id, tipo, nombre, tax_id, personal_id, email, telefono, vip,
                       consent_at as consent_lfpdppp_at, consent_voice_recording, created_at
                from clients
                where firm_id = $1::uuid
                order by created_at desc
                limit $2
                """,
                principal.firm_id, limit,
            )
    return [_row(r) for r in rows]


@router.post("/", response_model=ClientRow, status_code=201)
async def create_client(
    body: ClientCreate,
    principal: Principal = Depends(get_current_firm),
):
    cid = str(uuid.uuid4())
    consent_at = datetime.utcnow() if body.consent_lfpdppp else None
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into clients
              (id, firm_id, tipo, nombre, tax_id, personal_id, email, telefono, domicilio,
               vip, consent_at, consent_finalidades, consent_voice_recording,
               created_by, metadata)
            values
              ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9::jsonb,
               $10, $11, $12, $13, $14::uuid, $15::jsonb)
            returning id, tipo, nombre, tax_id, personal_id, email, telefono, vip,
                      consent_at as consent_lfpdppp_at, consent_voice_recording, created_at
            """,
            cid,
            principal.firm_id,
            body.tipo,
            body.nombre,
            body.tax_id,
            body.personal_id,
            body.email,
            body.telefono,
            json.dumps(body.domicilio or {}),
            body.vip,
            consent_at,
            body.consent_finalidades,
            body.consent_voice_recording,
            principal.user_id,
            json.dumps(body.metadata),
        )
    return _row(row)


@router.get("/{client_id}", response_model=ClientRow)
async def get_client(
    client_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, tipo, nombre, tax_id, personal_id, email, telefono, vip,
                   consent_at as consent_lfpdppp_at, consent_voice_recording, created_at
            from clients
            where id = $1::uuid and firm_id = $2::uuid
            """,
            client_id, principal.firm_id,
        )
    if not row:
        raise HTTPException(404, "client not found")
    return _row(row)


# ────────────────────────────────────────────────────────────────────
# Sprint 2 · S2-04 · Conflict of interest detector
# ────────────────────────────────────────────────────────────────────

class ConflictCheckRequest(BaseModel):
    nombre: str
    tax_id: Optional[str] = None
    personal_id: Optional[str] = None
    tipo: Optional[str] = None  # persona_natural | persona_juridica


class ConflictHit(BaseModel):
    kind: str  # 'duplicate_client' | 'counterparty_match' | 'similar_name'
    client_id: Optional[str] = None
    matter_id: Optional[str] = None
    severity: str = "medium"  # low | medium | high
    detail: str
    matched_value: Optional[str] = None


class ConflictCheckResponse(BaseModel):
    has_conflict: bool
    hits: list[ConflictHit] = Field(default_factory=list)


@router.post("/check-conflict", response_model=ConflictCheckResponse)
async def check_conflict_of_interest(
    body: ConflictCheckRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Cross-checks a candidate client against:
      1. existing `clients` of the firm (same tax_id/personal_id or similar name)
      2. `matter_parties` listed as counterparty (`rol = 'demandada'`) of the firm

    A "high severity" hit means the firm has already represented the
    counterparty in another matter — a textbook conflict of interest.
    """
    nombre = (body.nombre or "").strip()
    tax_id = (body.tax_id or "").strip() or None
    personal_id = (body.personal_id or "").strip() or None
    if len(nombre) < 2:
        raise HTTPException(400, "nombre demasiado corto")

    hits: list[ConflictHit] = []
    from utils.db import get_storage
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        # 1. Duplicate by tax_id/personal_id within the firm.
        if tax_id or personal_id:
            dup = await conn.fetch(
                """
                select id, nombre, tax_id, personal_id
                from clients
                where firm_id = $1::uuid
                  and (tax_id is not null and tax_id = $2 or personal_id is not null and personal_id = $3)
                limit 5
                """,
                principal.firm_id, tax_id, personal_id,
            )
            for r in dup:
                hits.append(ConflictHit(
                    kind="duplicate_client",
                    client_id=str(r["id"]),
                    severity="medium",
                    detail=f"Ya existe un cliente con el mismo identificador: {r['nombre']}",
                    matched_value=r["tax_id"] or r["personal_id"],
                ))

        # 2. Similar name in clients (trigram match).
        sim = await conn.fetch(
            """
            select id, nombre, similarity(nombre, $1) as score
            from clients
            where firm_id = $2::uuid and similarity(nombre, $1) > 0.55
            order by score desc
            limit 5
            """,
            nombre, principal.firm_id,
        )
        for r in sim:
            hits.append(ConflictHit(
                kind="similar_name",
                client_id=str(r["id"]),
                severity="low",
                detail=f"Cliente con nombre similar (score {float(r['score']):.2f}): {r['nombre']}",
                matched_value=r["nombre"],
            ))

        # 3. Counterparty match: this candidate appears as `demandada` in any matter.
        cp = await conn.fetch(
            """
            select p.matter_id, p.nombre, p.tax_id, m.titulo, m.display_id
            from matter_parties p
            join matters m on m.id = p.matter_id
            where m.firm_id = $1::uuid
              and p.rol in ('demandada','contraparte','denunciada')
              and (
                ($2 is not null and p.tax_id = $2)
                or similarity(p.nombre, $3) > 0.65
              )
            limit 5
            """,
            principal.firm_id, tax_id, nombre,
        )
        for r in cp:
            hits.append(ConflictHit(
                kind="counterparty_match",
                matter_id=str(r["matter_id"]),
                severity="high",
                detail=(
                    f"Conflicto potencial: este nombre figura como contraparte en "
                    f"el caso {r['display_id']} ({r['titulo']})."
                ),
                matched_value=r["nombre"],
            ))

    has_conflict = any(h.severity == "high" for h in hits) or len(hits) > 0
    return ConflictCheckResponse(has_conflict=has_conflict, hits=hits)


# ────────────────────────────────────────────────────────────────────
# Sprint 2 · S2-05 · Personería jurídica (RUES)
# ────────────────────────────────────────────────────────────────────

class PersoneriaResponse(BaseModel):
    nit: str
    razon_social: Optional[str]
    estado: Optional[str]  # ACTIVA, CANCELADA, etc.
    matricula: Optional[str]
    camara: Optional[str]
    fuente: str = "rues"
    fuente_url: Optional[str] = None
    found: bool


@router.get("/validate-personeria", response_model=PersoneriaResponse)
async def validate_personeria(
    nit: str,
    principal: Principal = Depends(get_current_firm),
):
    """Lookup of legal personality (Personería Jurídica) for a Colombian
    company by NIT against RUES (Registro Único Empresarial y Social).

    NOTE Sprint 2 scope: RUES doesn't expose a stable public REST API for
    NIT lookups. This endpoint:
      · Sanitizes the NIT
      · Returns a graceful "not_found" response when the public source
        does not respond, instead of a 500. The frontend uses this to
        render an informational state.
      · Sprint 5 (Court Watcher / scrapers) will replace the stub with a
        Playwright-based scraper of https://www.rues.org.co/ when the
        Playwright runtime is added to the Docker image.
    """
    import re as _re
    clean_nit = _re.sub(r"[^0-9]", "", nit or "")
    if len(clean_nit) < 8:
        raise HTTPException(400, "NIT inválido (mínimo 8 dígitos)")

    fuente_url = f"https://www.rues.org.co/RM?numDoc={clean_nit}"

    # Best-effort fetch · we don't error out if it fails; we return found=false.
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
            r = await client.get(fuente_url)
            html = r.text if r.status_code == 200 else ""
    except Exception:  # network error · still respond gracefully
        html = ""

    razon = None
    estado = None
    matricula = None
    camara = None
    found = False
    if html:
        # Lightweight extraction · the production scraper (Sprint 5)
        # will use Playwright + DOM selectors. Heuristics here look for
        # the canonical labels RUES uses on the result page.
        for label, key in [
            (r"Razón Social[^<:]*[:]?\s*</[^>]+>\s*<[^>]+>([^<]+)", "razon"),
            (r"Estado de la Matrícula[^<:]*[:]?\s*</[^>]+>\s*<[^>]+>([^<]+)", "estado"),
            (r"Matrícula[^<:]*[:]?\s*</[^>]+>\s*<[^>]+>([^<]+)", "matricula"),
            (r"Cámara[^<:]*[:]?\s*</[^>]+>\s*<[^>]+>([^<]+)", "camara"),
        ]:
            m = _re.search(label, html, _re.IGNORECASE)
            if m:
                value = _re.sub(r"\s+", " ", m.group(1)).strip()
                if key == "razon":
                    razon = value
                elif key == "estado":
                    estado = value
                elif key == "matricula":
                    matricula = value
                elif key == "camara":
                    camara = value
        found = bool(razon)

    return PersoneriaResponse(
        nit=clean_nit,
        razon_social=razon,
        estado=estado,
        matricula=matricula,
        camara=camara,
        fuente="rues",
        fuente_url=fuente_url,
        found=found,
    )
