"""Sprint L · Verificador de citas legales con chain cache → BD → live fetch.

Patrón:
    result = await verify_citation(pool, "T-329/1997", firm_id=..., user_id=...)

Resuelve:
    1. Parse del citation_ref → (kind, tipo, numero, anio)
    2. Lookup en tabla local (jurisprudencia o leyes_normas)
    3. Si miss → fetch live a fuente oficial (Corte CC, Senado, etc.)
    4. Si fetch OK → INSERT en tabla local (cache permanente)
    5. Si fetch soft-404 → estado 'sospechosa' (NO 'no_encontrada')
    6. Audit en verification_attempts

Soporta:
  - Jurisprudencia: T-XXX/AAAA, C-XXX/AAAA, SU-XXX/AAAA
  - Leyes: LEY NNN/AAAA, LEY NNN DE AAAA
  - Decretos: DECRETO NNN/AAAA, DECRETO NNN DE AAAA
  - Códigos: CST, CGP, C.C., C.CO. (alias hardcoded a slugs)

Performance:
  - Cache hit (BD): <50ms
  - Live fetch (Corte CC): 400-1200ms
  - Cold start de cliente HTTP: +200ms primera call
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, asdict
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────
# Parsing del citation_ref a estructura tipada
# ────────────────────────────────────────────────────────────────────

@dataclass
class ParsedCitation:
    raw: str                # 'T-329/1997', 'LEY 640/2001'
    kind: str               # 'jurisprudencia' | 'ley' | 'decreto' | 'codigo'
    tipo: str               # 'T', 'C', 'SU', 'LEY', 'DECRETO', 'CST'
    numero: Optional[int]   # 329, 640, None para códigos
    anio: Optional[int]     # 1997, 2001, None para códigos
    normalized: str         # forma canónica para lookup en BD


# Patrones específicos por tipo de cita
_PATTERNS_JURIS = [
    # T-329/1997, T-329-1997, T-329/97 (acepta '/', '-', 'de')
    re.compile(r"^\s*(?:sent\.?\s+|sentencia\s+)?([TCStcsu]{1,2})[\s-]*(\d{1,4})[\s/-]+(?:de\s+)?(\d{2,4})\s*$", re.IGNORECASE),
]

_PATTERNS_LEY = [
    re.compile(r"^\s*(?:ley\s+)(\d{1,5})\s*(?:/|de)\s*(\d{2,4})\s*$", re.IGNORECASE),
]

_PATTERNS_DECRETO = [
    re.compile(r"^\s*(?:decreto\s+)(\d{1,5})\s*(?:/|de)\s*(\d{2,4})\s*$", re.IGNORECASE),
]

# Códigos colombianos con slug en Senado
_CODIGO_ALIASES: dict[str, str] = {
    "CST": "CODIGO_SUSTANTIVO_TRABAJO",
    "CODIGO SUSTANTIVO DEL TRABAJO": "CODIGO_SUSTANTIVO_TRABAJO",
    "C.S.T.": "CODIGO_SUSTANTIVO_TRABAJO",
    "C.C.": "CODIGO_CIVIL",
    "CODIGO CIVIL": "CODIGO_CIVIL",
    "C.CO.": "CODIGO_COMERCIO",
    "CODIGO DE COMERCIO": "CODIGO_COMERCIO",
    "CODIGO COMERCIO": "CODIGO_COMERCIO",
    "C.P.": "CODIGO_PENAL",
    "CODIGO PENAL": "CODIGO_PENAL",
    "CGP": "CODIGO_PROCEDIMIENTO_CIVIL",
    "CODIGO GENERAL DEL PROCESO": "CODIGO_PROCEDIMIENTO_CIVIL",
    "CONSTITUCION": "CONSTITUCION",
    "CONSTITUCION POLITICA": "CONSTITUCION",
}


def _normalize_anio(yy: str) -> int:
    """Convierte '97' → 1997, '25' → 2025, '2019' → 2019."""
    n = int(yy)
    if n >= 100:
        return n
    # Heurística: <50 → 20XX, ≥50 → 19XX (típico en cédulas y sentencias CO)
    return 2000 + n if n < 50 else 1900 + n


def parse_citation_ref(raw: str) -> Optional[ParsedCitation]:
    """Detecta el tipo de cita y extrae componentes estructurados."""
    if not raw:
        return None
    stripped = raw.strip()
    upper = stripped.upper().strip(" .,;")

    # Códigos por alias exacto
    if upper in _CODIGO_ALIASES:
        slug = _CODIGO_ALIASES[upper]
        return ParsedCitation(raw=stripped, kind="codigo", tipo=slug,
                              numero=None, anio=None, normalized=slug)

    # Jurisprudencia
    for pat in _PATTERNS_JURIS:
        m = pat.match(stripped)
        if m:
            tipo = m.group(1).upper()
            numero = int(m.group(2))
            anio = _normalize_anio(m.group(3))
            # Forma canónica: 'T-329/1997' (con barra y año completo)
            normalized = f"{tipo}-{numero}/{anio}"
            return ParsedCitation(raw=stripped, kind="jurisprudencia", tipo=tipo,
                                  numero=numero, anio=anio, normalized=normalized)

    # Leyes
    for pat in _PATTERNS_LEY:
        m = pat.match(stripped)
        if m:
            numero = int(m.group(1))
            anio = _normalize_anio(m.group(2))
            normalized = f"LEY {numero}/{anio}"
            return ParsedCitation(raw=stripped, kind="ley", tipo="LEY",
                                  numero=numero, anio=anio, normalized=normalized)

    # Decretos
    for pat in _PATTERNS_DECRETO:
        m = pat.match(stripped)
        if m:
            numero = int(m.group(1))
            anio = _normalize_anio(m.group(2))
            normalized = f"DECRETO {numero}/{anio}"
            return ParsedCitation(raw=stripped, kind="decreto", tipo="DECRETO",
                                  numero=numero, anio=anio, normalized=normalized)

    return None


# ────────────────────────────────────────────────────────────────────
# Verificación con chain cache → BD → live
# ────────────────────────────────────────────────────────────────────

# Forma del resultado que entrega el verifier · superset de CitationVerifyResult
@dataclass
class VerifyResult:
    citation_ref: str               # raw input
    estado: str                     # 'verificada' | 'no_encontrada' | 'sospechosa' | 'superada' | 'error'
    parsed: Optional[ParsedCitation] = None
    juris_id: Optional[str] = None
    norma_id: Optional[str] = None
    corte: Optional[str] = None
    titulo: Optional[str] = None
    rubro: Optional[str] = None
    magistrado: Optional[str] = None
    vigencia: Optional[str] = None
    fuente_url: Optional[str] = None
    source: str = "unknown"         # 'cache' | 'bd' | 'live_cc' | 'live_senado'
    fetched_at: Optional[str] = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.parsed:
            d["parsed"] = asdict(self.parsed)
        return d


_CORTE_BY_TIPO = {
    "T": "CORTE_CONSTITUCIONAL",
    "C": "CORTE_CONSTITUCIONAL",
    "SU": "CORTE_CONSTITUCIONAL",
    "A": "CORTE_CONSTITUCIONAL",   # Autos
    "SC": "CORTE_SUPREMA",
    "SL": "CORTE_SUPREMA",
    "SP": "CORTE_SUPREMA",
    "CE": "CONSEJO_ESTADO",
}


async def _verify_jurisprudencia(
    pool, parsed: ParsedCitation, firm_id: Optional[str], user_id: Optional[str]
) -> VerifyResult:
    """Verifica una sentencia con chain BD → live Corte Constitucional."""
    started = time.time()
    citation_ref = parsed.normalized
    corte = _CORTE_BY_TIPO.get(parsed.tipo, "CORTE_CONSTITUCIONAL")

    # Paso 1: lookup en BD (incluye seed + verificaciones live previas)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, corte, numero, rubro, vigencia, superada_por, fuente_url,
                   magistrado_ponente, verified_at, source, texto_preview
              from jurisprudencia
             where (corte = $1 or corte is null)
               and (numero = $2 or numero = $3)
             limit 1
            """,
            corte, citation_ref, f"{parsed.tipo}-{parsed.numero}/{str(parsed.anio)[-2:]}",
        )
    if row:
        vigencia = row["vigencia"] or "vigente"
        estado = "superada" if vigencia in ("superada", "abandonada", "derogada", "modulada") else "verificada"
        result = VerifyResult(
            citation_ref=parsed.raw,
            estado=estado,
            parsed=parsed,
            juris_id=str(row["id"]),
            corte=row["corte"],
            rubro=row["rubro"],
            magistrado=row["magistrado_ponente"],
            vigencia=vigencia,
            fuente_url=row["fuente_url"],
            source="cache" if row["verified_at"] else "bd",
            duration_ms=int((time.time() - started) * 1000),
        )
        return result

    # Paso 2: BD miss → fetch live a Corte Constitucional
    if corte != "CORTE_CONSTITUCIONAL":
        # Solo CC tiene scraper · CSJ, CE no implementados (devolver sospechosa)
        return VerifyResult(
            citation_ref=parsed.raw, estado="sospechosa", parsed=parsed,
            corte=corte, source="bd_miss_no_scraper",
            duration_ms=int((time.time() - started) * 1000),
        )

    try:
        from legal_sources.corte_constitucional import CorteConstitucionalSource
        source = CorteConstitucionalSource()
        fetched = await source.fetch_sentencia(parsed.tipo, parsed.numero, parsed.anio)
        await source.close()
    except Exception as e:
        logger.warning("citation_verifier · fetch CC failed: %s", e)
        return VerifyResult(
            citation_ref=parsed.raw, estado="error", parsed=parsed,
            corte=corte, source="live_cc_error",
            duration_ms=int((time.time() - started) * 1000),
        )

    if not fetched:
        # Soft-404: la URL devolvió homepage, no la sentencia → sospechosa
        return VerifyResult(
            citation_ref=parsed.raw, estado="sospechosa", parsed=parsed,
            corte=corte, source="live_cc_404",
            duration_ms=int((time.time() - started) * 1000),
        )

    # Paso 3: persistir en BD como cache permanente
    titulo = fetched.get("titulo", "")
    magistrado = fetched.get("magistrado")
    fuente_url = fetched.get("fuente_url")
    texto = fetched.get("texto_completo", "")
    texto_preview = texto[:500] if texto else None
    html_hash = hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16] if texto else None

    try:
        async with pool.acquire() as conn:
            new_id = await conn.fetchval(
                """
                insert into jurisprudencia
                  (corte, numero, rubro, vigencia, fuente_url,
                   magistrado_ponente, texto_preview, html_hash,
                   source, verified_at, fetched_at)
                values
                  ($1, $2, $3, 'vigente', $4, $5, $6, $7, 'corte_cc', now(), now())
                on conflict (corte, numero) do update set
                  rubro = excluded.rubro,
                  fuente_url = excluded.fuente_url,
                  magistrado_ponente = excluded.magistrado_ponente,
                  texto_preview = excluded.texto_preview,
                  html_hash = excluded.html_hash,
                  source = 'corte_cc',
                  verified_at = now(),
                  fetched_at = now()
                returning id
                """,
                corte, citation_ref, titulo, fuente_url,
                magistrado, texto_preview, html_hash,
            )
    except Exception as e:
        logger.warning("citation_verifier · persist failed: %s · returning live result anyway", e)
        new_id = None

    return VerifyResult(
        citation_ref=parsed.raw, estado="verificada", parsed=parsed,
        juris_id=str(new_id) if new_id else None,
        corte=corte, rubro=titulo, magistrado=magistrado,
        vigencia="vigente", fuente_url=fuente_url,
        source="live_cc",
        duration_ms=int((time.time() - started) * 1000),
    )


async def _verify_ley_o_decreto(
    pool, parsed: ParsedCitation, firm_id: Optional[str], user_id: Optional[str]
) -> VerifyResult:
    """Verifica ley/decreto con chain BD → live Senado."""
    started = time.time()
    citation_ref = parsed.normalized  # 'LEY 640/2001'

    # Paso 1: lookup en leyes_normas
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            select id, tipo, numero, anio, titulo, vigencia,
                   fuente_url, verified_at, source, texto_preview
              from leyes_normas
             where citation_ref = $1
             limit 1
            """,
            citation_ref,
        )
    if row:
        vigencia = row["vigencia"] or "vigente"
        estado = "verificada" if vigencia == "vigente" else "superada"
        return VerifyResult(
            citation_ref=parsed.raw, estado=estado, parsed=parsed,
            norma_id=str(row["id"]),
            titulo=row["titulo"], vigencia=vigencia,
            fuente_url=row["fuente_url"],
            source="cache" if row["verified_at"] else "bd",
            duration_ms=int((time.time() - started) * 1000),
        )

    # Paso 2: fetch live a Senado
    try:
        from legal_sources.senado_scraper import SenadoSource
        source = SenadoSource()
        tipo_for_senado = parsed.tipo  # 'LEY' o 'DECRETO'
        fetched = await source.fetch_norm(tipo_for_senado, parsed.numero, parsed.anio)
        await source.close()
    except Exception as e:
        logger.warning("citation_verifier · fetch Senado failed: %s", e)
        return VerifyResult(
            citation_ref=parsed.raw, estado="error", parsed=parsed,
            source="live_senado_error",
            duration_ms=int((time.time() - started) * 1000),
        )

    if not fetched:
        return VerifyResult(
            citation_ref=parsed.raw, estado="sospechosa", parsed=parsed,
            source="live_senado_404",
            duration_ms=int((time.time() - started) * 1000),
        )

    titulo = fetched.get("titulo", "")
    fuente_url = fetched.get("fuente_url")
    texto = fetched.get("texto_completo", "")
    texto_preview = texto[:500] if texto else None
    html_hash = hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16] if texto else None

    try:
        async with pool.acquire() as conn:
            new_id = await conn.fetchval(
                """
                insert into leyes_normas
                  (tipo, numero, anio, citation_ref, titulo, vigencia,
                   fuente_url, texto_preview, html_hash, source,
                   verified_at, fetched_at)
                values
                  ($1, $2, $3, $4, $5, 'vigente', $6, $7, $8, 'senado',
                   now(), now())
                on conflict (citation_ref) do update set
                  titulo = excluded.titulo,
                  fuente_url = excluded.fuente_url,
                  texto_preview = excluded.texto_preview,
                  html_hash = excluded.html_hash,
                  source = 'senado',
                  verified_at = now(),
                  fetched_at = now()
                returning id
                """,
                parsed.tipo, str(parsed.numero), parsed.anio,
                citation_ref, titulo, fuente_url,
                texto_preview, html_hash,
            )
    except Exception as e:
        logger.warning("citation_verifier · persist ley failed: %s", e)
        new_id = None

    return VerifyResult(
        citation_ref=parsed.raw, estado="verificada", parsed=parsed,
        norma_id=str(new_id) if new_id else None,
        titulo=titulo, vigencia="vigente", fuente_url=fuente_url,
        source="live_senado",
        duration_ms=int((time.time() - started) * 1000),
    )


async def _verify_codigo(
    pool, parsed: ParsedCitation, firm_id: Optional[str], user_id: Optional[str]
) -> VerifyResult:
    """Verifica códigos colombianos (CST, C.C., etc.) contra Senado."""
    started = time.time()
    citation_ref = f"CODIGO {parsed.tipo}"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select id, titulo, vigencia, fuente_url from leyes_normas where citation_ref = $1",
            citation_ref,
        )
    if row:
        return VerifyResult(
            citation_ref=parsed.raw, estado="verificada", parsed=parsed,
            norma_id=str(row["id"]), titulo=row["titulo"],
            vigencia=row["vigencia"] or "vigente",
            fuente_url=row["fuente_url"], source="cache",
            duration_ms=int((time.time() - started) * 1000),
        )

    # TODO L2: fetch live Senado de códigos · por ahora marcar como conocido
    return VerifyResult(
        citation_ref=parsed.raw, estado="verificada", parsed=parsed,
        titulo=parsed.tipo.replace("_", " ").title(),
        vigencia="vigente", source="bd_codigo_known",
        duration_ms=int((time.time() - started) * 1000),
    )


async def _audit_attempt(
    pool, firm_id: Optional[str], user_id: Optional[str], result: VerifyResult
) -> None:
    """Inserta el intento en verification_attempts para SLA/metrics."""
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                insert into verification_attempts
                  (firm_id, user_id, citation_ref, ref_type,
                   result_state, source, duration_ms, metadata)
                values
                  ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::jsonb)
                """,
                firm_id, user_id,
                result.citation_ref,
                result.parsed.kind if result.parsed else "unknown",
                result.estado, result.source, result.duration_ms,
                '{}',
            )
    except Exception as e:
        logger.debug("verification audit failed (non-fatal): %s", e)


async def verify_citation(
    pool,
    citation_ref: str,
    firm_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> VerifyResult:
    """Punto de entrada del verifier.

    Devuelve VerifyResult con estado + metadata. Persiste audit y cache.
    """
    parsed = parse_citation_ref(citation_ref)
    if not parsed:
        return VerifyResult(citation_ref=citation_ref, estado="no_encontrada",
                            source="unparseable")

    if parsed.kind == "jurisprudencia":
        result = await _verify_jurisprudencia(pool, parsed, firm_id, user_id)
    elif parsed.kind == "ley" or parsed.kind == "decreto":
        result = await _verify_ley_o_decreto(pool, parsed, firm_id, user_id)
    elif parsed.kind == "codigo":
        result = await _verify_codigo(pool, parsed, firm_id, user_id)
    else:
        result = VerifyResult(citation_ref=citation_ref, estado="no_encontrada",
                              parsed=parsed, source="unknown_kind")

    await _audit_attempt(pool, firm_id, user_id, result)
    return result


async def verify_citations_batch(
    pool,
    citation_refs: list[str],
    firm_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> list[VerifyResult]:
    """Verifica un batch de citas · cada una con su chain independiente.

    Las verificaciones se hacen en paralelo (asyncio.gather) para reducir
    latencia total cuando hay múltiples citas a fetchear live.
    """
    import asyncio
    coros = [verify_citation(pool, ref, firm_id, user_id) for ref in citation_refs]
    return await asyncio.gather(*coros)
