"""Colombian jurisprudence citation registry · search + verify + lookup.

Anti-hallucination contract from PRD F3 (adapted to Colombia):
  · Citation Existence Rate = 100% over gold set
  · Citation Groundedness ≥ 95%
  · Citas sin badge verde tienen copy-paste bloqueado en UI

Source of truth: `jurisprudencia` table (existing legal_migration.sql) with
Colombian courts: CORTE_CONSTITUCIONAL, CORTE_SUPREMA, CONSEJO_ESTADO.

Citation refs use the Colombian numbering scheme:
  · Tutela:           T-XXX/AAAA      (Sala Revisión Corte Constitucional)
  · Constitucionalidad: C-XXX/AAAA    (Sala Plena Corte Constitucional)
  · Unificación:      SU-XXX/AAAA     (Sala Plena Corte Constitucional)
  · Casación laboral: SL-XXXXX-AAAA   (Sala Laboral Corte Suprema)
  · Casación civil:   SC-XXXXX-AAAA   (Sala Civil Corte Suprema)
  · Casación penal:   SP-XXXXX-AAAA   (Sala Penal Corte Suprema)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm
from utils.llm import llm_generate_embedding


def _vec_to_pg(v: list[float]) -> str:
    """Convert Python list of floats to pgvector literal string."""
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


# In-process embedding cache · LRU-style with hard cap.
# Citation queries are highly repetitive ("despido", "tutela", etc.) so
# caching saves ~700ms per hit. TTL 30 min covers a typical workday.
_EMBED_CACHE: dict[str, tuple[float, list[float]]] = {}
_EMBED_CACHE_MAX = 512
_EMBED_CACHE_TTL_S = 1800
_embed_lock = asyncio.Lock()


async def _embed_cached(text: str, session_id: str) -> list[float]:
    key = hashlib.sha1(text.lower().strip().encode("utf-8")).hexdigest()
    now = time.time()
    cached = _EMBED_CACHE.get(key)
    if cached and now - cached[0] < _EMBED_CACHE_TTL_S:
        return cached[1]
    emb = await llm_generate_embedding(
        text=text, purpose="citation_search", session_id=session_id,
    )
    async with _embed_lock:
        if len(_EMBED_CACHE) >= _EMBED_CACHE_MAX:
            # drop oldest 20%
            drop = sorted(_EMBED_CACHE.items(), key=lambda x: x[1][0])[: _EMBED_CACHE_MAX // 5]
            for k, _ in drop:
                _EMBED_CACHE.pop(k, None)
        _EMBED_CACHE[key] = (now, emb)
    return emb

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/citations", tags=["citations"])


# Allowed Colombian courts (filter at query time)
ALLOWED_CORTES_CO = ("CORTE_CONSTITUCIONAL", "CORTE_SUPREMA", "CONSEJO_ESTADO")


# ─────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────


class CitationSearchRequest(BaseModel):
    query: str = Field(min_length=3)
    materia: Optional[str] = None
    corte: Optional[str] = None      # CORTE_CONSTITUCIONAL | CORTE_SUPREMA | CONSEJO_ESTADO
    tipo_sentencia: Optional[str] = None  # T | C | SU | SL | SC | SP | CASACION
    limit: int = Field(default=8, ge=1, le=20)
    only_vigente: bool = True


class CitationHit(BaseModel):
    juris_id: str
    corte: str
    citation_ref: str               # T-388/2019, C-200/1995, SL-12345/2024, etc.
    rubro: Optional[str]
    vigencia: str
    url_oficial: Optional[str]
    ratio_decidendi: Optional[str]
    relevancia: str                 # Muy alta | Alta | Media
    combined_score: float


class CitationVerifyRequest(BaseModel):
    citation_refs: list[str] = Field(min_length=1, max_length=20)


class CitationVerifyResult(BaseModel):
    citation_ref: str
    estado: str                     # verificada | no_encontrada | superada | sospechosa
    juris_id: Optional[str] = None
    corte: Optional[str] = None
    rubro: Optional[str] = None
    vigencia: Optional[str] = None
    url_oficial: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _relevancia_from_score(s: float) -> str:
    if s >= 0.65:
        return "Muy alta"
    if s >= 0.45:
        return "Alta"
    return "Media"


# ─────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────


@router.post("/search", response_model=list[CitationHit])
async def citation_search(
    req: CitationSearchRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Hybrid search jurisprudencia colombiana → top-k sentencias vigentes."""
    embedding = await _embed_cached(req.query, principal.user_id)

    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage not available")

    # If no specific court requested, search all Colombian courts.
    corte_filter = req.corte if req.corte in ALLOWED_CORTES_CO else None

    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select juris_id, corte, citation_ref, rubro, vigencia, url_oficial,
                   ratio_decidendi, combined_score
            from match_juris($1::vector, $2, $3, $4, $5, 0.45, 0.10, $6)
            """,
            _vec_to_pg(embedding), req.query, corte_filter, req.tipo_sentencia,
            req.limit, req.only_vigente,
        )

    return [
        CitationHit(
            juris_id=str(r["juris_id"]),
            corte=r["corte"],
            citation_ref=r["citation_ref"] or "",
            rubro=r["rubro"],
            vigencia=r["vigencia"] or "vigente",
            url_oficial=r["url_oficial"],
            ratio_decidendi=r["ratio_decidendi"],
            relevancia=_relevancia_from_score(float(r["combined_score"] or 0)),
            combined_score=float(r["combined_score"] or 0),
        )
        for r in rows
    ]


@router.post("/verify", response_model=list[CitationVerifyResult])
async def citation_verify(
    req: CitationVerifyRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Batch-verify citas con chain cache → BD → live fetch.

    Sprint L · ahora soporta:
      - Jurisprudencia: T-XXX/AAAA, C-XXX/AAAA, SU-XXX/AAAA
      - Leyes: LEY NNN/AAAA · fetch live a Senado
      - Decretos: DECRETO NNN/AAAA · fetch live a Senado
      - Códigos: CST, C.C., C.CO., CGP

    Si la cita no está en BD, hace fetch al sitio oficial (Corte
    Constitucional o Secretaría del Senado) y persiste el resultado
    como cache permanente. Soft-404 → estado 'sospechosa' (alucinación
    posible). Cada intento se audita en verification_attempts.
    """
    from utils.db import get_storage
    from utils.citation_verifier import verify_citations_batch
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage not available")

    results = await verify_citations_batch(
        storage.pool,
        req.citation_refs,
        firm_id=principal.firm_id,
        user_id=principal.user_id,
    )

    out: list[CitationVerifyResult] = []
    for r in results:
        # Mapear el estado interno al schema del response model.
        # 'sospechosa' es nuevo (alucinación detectada en live fetch).
        # 'error' (fuente caída) → no_encontrada para no asustar al usuario.
        estado_api = r.estado
        if estado_api == "error":
            estado_api = "no_encontrada"
        out.append(CitationVerifyResult(
            citation_ref=r.citation_ref,
            estado=estado_api,
            juris_id=r.juris_id,
            corte=r.corte,
            rubro=r.rubro or r.titulo,
            vigencia=r.vigencia,
            url_oficial=r.fuente_url,
        ))
    return out


# ────────────────────────────────────────────────────────────────────
# Sprint 3 · S3-05 · Suggest replacement for an outdated citation
# ────────────────────────────────────────────────────────────────────


class SuggestReplacementRequest(BaseModel):
    citation_ref: str = Field(..., min_length=3, max_length=80)
    rubro: Optional[str] = None  # current cited summary, helps the LLM
    case_context: Optional[str] = Field(None, max_length=600)


class SuggestReplacementResponse(BaseModel):
    original_ref: str
    replacement_ref: Optional[str]
    replacement_rubro: Optional[str]
    replacement_url: Optional[str]
    reason: str
    confidence: float = 0.0
    found: bool


@router.post("/suggest-replacement", response_model=SuggestReplacementResponse)
async def suggest_replacement(
    body: SuggestReplacementRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Suggests a replacement when a cited norm/sentence is outdated.

    Strategy (cheap-first, falls back on LLM):
      1. If we have the citation in our `jurisprudencia` table with a
         `superada_por` link → return that immediately (deterministic).
      2. Else, do a RAG-style similarity search on `jurisprudencia` by
         the rubro/case_context.
      3. Otherwise, return found=false (no hallucinated suggestion).
    """
    from utils.db import get_storage
    storage = await get_storage()
    ref = body.citation_ref.strip()

    async with storage.pool.acquire() as conn:
        # 1. Direct successor in registry.
        row = await conn.fetchrow(
            """
            select j.numero, j.tipo_sentencia, j.rubro, j.fuente_url, j.vigencia,
                   succ.numero as succ_numero,
                   succ.tipo_sentencia as succ_tipo,
                   succ.rubro as succ_rubro,
                   succ.fuente_url as succ_url
            from jurisprudencia j
            left join jurisprudencia succ on succ.id = j.superada_por
            where j.numero = $1
            limit 1
            """,
            ref,
        )
        if row and row["succ_numero"]:
            return SuggestReplacementResponse(
                original_ref=ref,
                replacement_ref=row["succ_numero"],
                replacement_rubro=row["succ_rubro"],
                replacement_url=row["succ_url"],
                reason="Sentencia superada según registro de jurisprudencia.",
                confidence=0.95,
                found=True,
            )

        # 2. Similarity search by rubro + context.
        query_text = " ".join(filter(None, [body.rubro, body.case_context]))
        if not query_text and row:
            query_text = row["rubro"] or ""
        if query_text and len(query_text) >= 8:
            try:
                emb = await llm_generate_embedding(query_text)
                qvec = _vec_to_pg(emb)
                hits = await conn.fetch(
                    f"""
                    select numero, tipo_sentencia, rubro, fuente_url, vigencia,
                           1 - (embedding <=> '{qvec}'::vector) as score
                    from jurisprudencia
                    where vigencia in ('vigente','modulada')
                      and numero <> $1
                    order by embedding <=> '{qvec}'::vector
                    limit 1
                    """,
                    ref,
                )
                if hits:
                    h = hits[0]
                    score = float(h["score"])
                    if score >= 0.65:
                        return SuggestReplacementResponse(
                            original_ref=ref,
                            replacement_ref=h["numero"],
                            replacement_rubro=h["rubro"],
                            replacement_url=h["fuente_url"],
                            reason=(
                                f"Mejor match semántico vigente (score {score:.2f})."
                            ),
                            confidence=score,
                            found=True,
                        )
            except Exception as e:
                logger.warning("suggest-replacement embedding failed: %s", e)

    # 3. No suggestion found · don't hallucinate.
    return SuggestReplacementResponse(
        original_ref=ref,
        replacement_ref=None,
        replacement_rubro=None,
        replacement_url=None,
        reason="No encontramos una cita vigente equivalente. Revisa manualmente.",
        confidence=0.0,
        found=False,
    )


@router.get("/{citation_ref}", response_model=CitationVerifyResult)
async def citation_get(
    citation_ref: str,
    principal: Principal = Depends(get_current_firm),
):
    """GET single sentencia full record for the citation drawer."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage not available")

    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, corte, numero as citation_ref, rubro, vigencia,
                   superada_por, fuente_url
            from jurisprudencia
            where corte = any($1::text[])
              and numero = $2
            """,
            list(ALLOWED_CORTES_CO), citation_ref,
        )
    if not row:
        raise HTTPException(404, "sentencia not found")
    vigencia = row["vigencia"] or "vigente"
    estado = "verificada" if vigencia == "vigente" else "superada"
    return CitationVerifyResult(
        citation_ref=row["citation_ref"],
        estado=estado,
        juris_id=str(row["id"]),
        corte=row["corte"],
        rubro=row["rubro"],
        vigencia=vigencia,
        url_oficial=row["fuente_url"],
    )


# ─────────────────────────────────────────────────────────────────────
# Tool adapters for OpenAI Realtime
# ─────────────────────────────────────────────────────────────────────


async def research_jurisprudence_tool(args: dict, ctx: dict) -> dict:
    """Tool adapter for `research_jurisprudence` (Colombia)."""
    query = args.get("query")
    if not query:
        return {"error": "query required"}
    corte = args.get("corte") if args.get("corte") in ALLOWED_CORTES_CO else None
    limit = int(args.get("limit", 6) or 6)

    embedding = await _embed_cached(query, ctx.get("session_id", ""))
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage not available", "hits": []}
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            select juris_id, corte, citation_ref, rubro, vigencia, url_oficial,
                   ratio_decidendi, combined_score
            from match_juris($1::vector, $2, $3, null, $4, 0.45, 0.10, true)
            """,
            _vec_to_pg(embedding), query, corte, limit,
        )

    hits = [
        {
            "juris_id": str(r["juris_id"]),
            "corte": r["corte"],
            "citation_ref": r["citation_ref"],
            "rubro": (r["rubro"] or "")[:300],
            "vigencia": r["vigencia"],
            "url_oficial": r["url_oficial"],
            "ratio_decidendi": (r["ratio_decidendi"] or "")[:600],
            "relevancia": _relevancia_from_score(float(r["combined_score"] or 0)),
            "score": round(float(r["combined_score"] or 0), 3),
        }
        for r in rows
    ]
    return {"hits": hits, "count": len(hits)}


async def validate_citation_tool(args: dict, ctx: dict) -> dict:
    """Tool adapter for `validate_citation` (Colombia)."""
    ref = (args.get("citation_ref") or "").strip()
    if not ref:
        return {"error": "citation_ref required"}
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"estado": "no_encontrada", "citation_ref": ref}
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, corte, numero as citation_ref, rubro, vigencia,
                   superada_por, fuente_url
            from jurisprudencia
            where corte = any($1::text[]) and numero = $2
            """,
            list(ALLOWED_CORTES_CO), ref,
        )
    if not row:
        return {"estado": "no_encontrada", "citation_ref": ref}
    vigencia = row["vigencia"] or "vigente"
    if vigencia != "vigente":
        return {
            "estado": "superada",
            "citation_ref": ref,
            "corte": row["corte"],
            "rubro": row["rubro"],
            "url_oficial": row["fuente_url"],
            "vigencia": vigencia,
        }
    return {
        "estado": "verificada",
        "citation_ref": ref,
        "corte": row["corte"],
        "rubro": row["rubro"],
        "url_oficial": row["fuente_url"],
        "vigencia": vigencia,
    }


async def validate_norm_vigencia_tool(args: dict, ctx: dict) -> dict:
    """Tool adapter for `validate_norm_vigencia`.

    Reuses the existing derogation/vigencia_checker from the Colombia backend.
    """
    try:
        tipo = args["tipo"]
        numero = int(args["numero"])
        anio = int(args["anio"])
    except (KeyError, ValueError) as e:
        return {"error": f"invalid arguments: {e}"}

    try:
        from api.legal import _vigencia_checker  # set by main.py at startup
        if _vigencia_checker is None:
            return {"error": "vigencia checker not initialized"}
        result = await _vigencia_checker.check(
            tipo=tipo, numero=numero, anio=anio, check_live_sources=True,
        )
        return {
            "tipo": result.tipo,
            "numero": result.numero,
            "anio": result.anio,
            "titulo": result.titulo,
            "estado": result.estado,
            "encontrada": result.encontrada,
            "derogaciones": result.derogaciones,
        }
    except Exception as e:
        logger.warning("vigencia tool failed: %s", e)
        return {"error": str(e)}
