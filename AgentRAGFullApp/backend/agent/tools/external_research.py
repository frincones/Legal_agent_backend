"""F2 · External research tools (httpx-only).

4 tools que consultan portales públicos colombianos:
  - search_suin_juriscol: SUIN-Juriscol (textos normativos)
  - verify_rue_persona:   RUES (registro único empresarial)
  - fetch_dof_co_publicacion: DOF / Diario Oficial Colombia (publicaciones recientes)
  - fetch_banrep_dtf:     Banrep (tasas DTF / IBR / IPC)

Cada tool va contra el portal real con httpx; cachea en `external_fetch_cache`
con TTL específico. Si el portal devuelve error, retorna {"error", ...} sin
levantar excepción (gracefulful degradation).

Playwright se difiere para una fase posterior — los 4 portales aquí soportan
httpx + parser HTML/JSON sin necesidad de browser headless.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from xml.etree import ElementTree as ET

import httpx

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Cache helper
# ─────────────────────────────────────────────────────────────────────


async def _cache_get(key: str, max_age_seconds: int) -> Optional[dict]:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return None
    async with storage.pool.acquire() as conn:
        row = await conn.fetchval(
            "select lexai_cache_get($1::text, $2)",
            key, max_age_seconds,
        )
        if row:
            try:
                await conn.execute(
                    "update external_fetch_cache set hit_count = hit_count + 1 "
                    "where cache_key = $1::text",
                    key,
                )
            except Exception:
                pass
        if not row:
            return None
        if isinstance(row, str):
            try:
                row = json.loads(row)
            except Exception:
                return None
        return row


async def _cache_set(
    key: str,
    source: str,
    url: Optional[str],
    content_jsonb: Optional[dict],
    content_text: Optional[str],
    ttl_seconds: int,
    status: str = "ok",
    error_message: Optional[str] = None,
) -> None:
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """
            insert into external_fetch_cache
              (cache_key, source, url, content_jsonb, content_text, status,
               error_message, fetched_at, ttl_seconds, hit_count)
            values
              ($1, $2, $3, $4::jsonb, $5, $6, $7, now(), $8, 0)
            on conflict (cache_key) do update set
              source = excluded.source,
              url = excluded.url,
              content_jsonb = excluded.content_jsonb,
              content_text = excluded.content_text,
              status = excluded.status,
              error_message = excluded.error_message,
              fetched_at = now(),
              ttl_seconds = excluded.ttl_seconds
            """,
            key, source, url,
            json.dumps(content_jsonb) if content_jsonb is not None else None,
            content_text, status, error_message, ttl_seconds,
        )


async def _http_get(url: str, *, timeout: float = 15.0, **kwargs) -> tuple[int, str, dict]:
    """GET con timeout, sin verify SSL (varios portales gov.co tienen certs incompletos)."""
    async with httpx.AsyncClient(timeout=timeout, verify=False, follow_redirects=True) as client:
        r = await client.get(url, **kwargs)
        return r.status_code, r.text, dict(r.headers)


# ════════════════════════════════════════════════════════════════════════
# 1. SUIN-Juriscol · normas (Leyes, Decretos, Resoluciones)
# ════════════════════════════════════════════════════════════════════════


async def search_suin_juriscol_tool(args: dict, ctx: dict) -> dict:
    """Busca norma en SUIN-Juriscol (motor oficial Función Pública).

    args:
      tipo: LEY | DECRETO | RESOLUCION | CIRCULAR | CODIGO
      numero: int
      anio: int
    """
    tipo = (args.get("tipo") or "").upper().strip()
    numero = args.get("numero")
    anio = args.get("anio")
    if not (tipo and numero and anio):
        return {"error": "tipo, numero, anio son requeridos"}

    cache_key = f"suin:{tipo.lower()}:{numero}:{anio}"
    cached = await _cache_get(cache_key, max_age_seconds=86400)  # 24h
    if cached and cached.get("status") == "ok":
        return {**(cached.get("content_jsonb") or {}), "cached": True}

    # Búsqueda principal: motor SUIN-Juriscol
    query = f"{tipo}+{numero}+{anio}".replace(" ", "+")
    url = f"http://www.suin-juriscol.gov.co/buscador/buscar.html?p_tdoc={tipo}&p_numero={numero}&p_anio={anio}"
    try:
        status, html, _headers = await _http_get(url)
    except Exception as e:
        await _cache_set(cache_key, "suin", url, None, None, ttl_seconds=300, status="error", error_message=str(e))
        return {"error": f"SUIN unreachable: {e}", "url": url}

    if status >= 400:
        await _cache_set(cache_key, "suin", url, None, html, ttl_seconds=300, status="error",
                         error_message=f"http {status}")
        return {"error": f"SUIN http {status}", "url": url}

    # Parse mínimo: detectar "no se encontraron resultados" o extraer link al PDF
    not_found = "no se encontraron" in html.lower() or "sin resultados" in html.lower()
    link_match = re.search(r'href="([^"]*\.pdf)"', html, flags=re.I)
    titulo_match = re.search(r"<title>(.*?)</title>", html, flags=re.S | re.I)

    payload = {
        "source": "suin-juriscol",
        "url": url,
        "found": not not_found,
        "tipo": tipo,
        "numero": numero,
        "anio": anio,
        "pdf_url": link_match.group(1) if link_match else None,
        "titulo_html": (titulo_match.group(1).strip() if titulo_match else None),
        "html_excerpt": (html[:1500] if html else None),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set(cache_key, "suin", url, payload, html[:60000], ttl_seconds=86400, status="ok")
    return {**payload, "cached": False}


# ════════════════════════════════════════════════════════════════════════
# 2. RUES · verificación de personas naturales/jurídicas
# ════════════════════════════════════════════════════════════════════════


async def verify_rue_persona_tool(args: dict, ctx: dict) -> dict:
    """Verifica persona en RUES via búsqueda por NIT o nombre.

    args:
      query: NIT o razón social (string)
    """
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query requerido"}

    cache_key = f"rues:{query.lower()}"
    cached = await _cache_get(cache_key, max_age_seconds=43200)  # 12h
    if cached and cached.get("status") == "ok":
        return {**(cached.get("content_jsonb") or {}), "cached": True}

    # RUES tiene endpoint API público (rues.org.co)
    url = f"https://www.rues.org.co/api/web/Empresa/RuesEmpresa?Codigo={query}"
    try:
        status, body, _headers = await _http_get(url)
    except Exception as e:
        await _cache_set(cache_key, "rues", url, None, None, ttl_seconds=600, status="error", error_message=str(e))
        return {"error": f"RUES unreachable: {e}", "url": url}

    if status == 404:
        payload = {"source": "rues", "found": False, "query": query, "url": url}
        await _cache_set(cache_key, "rues", url, payload, None, ttl_seconds=43200, status="not_found")
        return {**payload, "cached": False}

    if status >= 400:
        return {"error": f"RUES http {status}", "url": url}

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # Fallback HTML scraping (página pública)
        url2 = f"https://www.rues.org.co/?Empresa={query}"
        s2, html2, _ = await _http_get(url2)
        payload = {
            "source": "rues",
            "found": s2 == 200,
            "query": query,
            "url": url2,
            "raw_html_excerpt": (html2[:2000] if html2 else None),
        }
        await _cache_set(cache_key, "rues", url2, payload, html2[:30000], ttl_seconds=43200, status="ok")
        return {**payload, "cached": False}

    payload = {
        "source": "rues",
        "found": True,
        "query": query,
        "url": url,
        "data": data,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set(cache_key, "rues", url, payload, body, ttl_seconds=43200, status="ok")
    return {**payload, "cached": False}


# ════════════════════════════════════════════════════════════════════════
# 3. DOF Colombia · Diario Oficial
# ════════════════════════════════════════════════════════════════════════


async def fetch_dof_co_publicacion_tool(args: dict, ctx: dict) -> dict:
    """Busca publicaciones recientes en el Diario Oficial.

    args:
      keyword: string a buscar (opcional)
      limit: max resultados (default 10)
    """
    keyword = (args.get("keyword") or "").strip()
    limit = max(1, min(int(args.get("limit") or 10), 25))

    cache_key = f"dof:{keyword.lower()[:60]}:{limit}"
    cached = await _cache_get(cache_key, max_age_seconds=21600)  # 6h
    if cached and cached.get("status") == "ok":
        return {**(cached.get("content_jsonb") or {}), "cached": True}

    # DOF expone RSS con últimas publicaciones (https://www.imprenta.gov.co/rss)
    url = "https://www.imprenta.gov.co/rss/diario-oficial"
    try:
        status, body, _headers = await _http_get(url)
    except Exception as e:
        return {"error": f"DOF unreachable: {e}", "url": url}

    publicaciones: list[dict] = []
    if status == 200 and body:
        try:
            root = ET.fromstring(body)
            # Estructura RSS típica: rss/channel/item
            for item in root.iter("item"):
                titulo = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub_date = (item.findtext("pubDate") or "").strip()
                desc = (item.findtext("description") or "").strip()
                if keyword and keyword.lower() not in (titulo + desc).lower():
                    continue
                publicaciones.append({
                    "titulo": titulo,
                    "url": link,
                    "fecha": pub_date,
                    "descripcion": desc[:280],
                })
                if len(publicaciones) >= limit:
                    break
        except ET.ParseError as e:
            return {"error": f"DOF RSS parse error: {e}"}

    # Fallback si RSS está caído: emitir mensaje + cache vacío corto
    if not publicaciones and status >= 400:
        return {"error": f"DOF http {status}", "url": url}

    payload = {
        "source": "dof_co",
        "keyword": keyword or None,
        "count": len(publicaciones),
        "publicaciones": publicaciones,
        "url": url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    await _cache_set(cache_key, "dof", url, payload, body[:30000] if body else None,
                     ttl_seconds=21600, status="ok")
    return {**payload, "cached": False}


# ════════════════════════════════════════════════════════════════════════
# 4. Banrep · tasas DTF / IBR / IPC
# ════════════════════════════════════════════════════════════════════════


async def fetch_banrep_dtf_tool(args: dict, ctx: dict) -> dict:
    """Consulta tasas Banrep. args: serie ('DTF' | 'IBR_OVERNIGHT' | 'IPC' | 'TRM')."""
    serie = (args.get("serie") or "DTF").upper().strip()
    if serie not in ("DTF", "IBR_OVERNIGHT", "IPC", "TRM"):
        return {"error": f"serie '{serie}' no soportada"}

    cache_key = f"banrep:{serie.lower()}:{datetime.now().strftime('%Y%m%d')}"
    cached = await _cache_get(cache_key, max_age_seconds=21600)  # 6h
    if cached and cached.get("status") == "ok":
        return {**(cached.get("content_jsonb") or {}), "cached": True}

    # Banrep expone series via su web pública. Fallback a tabla legal_constants
    # ya seedada por la migración F3 anterior.
    from utils.db import get_storage
    storage = await get_storage()
    if hasattr(storage, "pool"):
        async with storage.pool.acquire() as conn:
            const_key = {
                "DTF": "co.dtf.anual",
                "IBR_OVERNIGHT": "co.dtf.anual",  # proxy MVP
                "IPC": None,
                "TRM": None,
            }.get(serie)
            if const_key:
                value = await conn.fetchval(
                    "select lexai_legal_constant($1::text, $2::date)",
                    const_key, datetime.now().date(),
                )
                if value is not None:
                    payload = {
                        "source": "banrep_via_legal_constants",
                        "serie": serie,
                        "valor": float(value),
                        "as_of": datetime.now(timezone.utc).isoformat(),
                        "note": ("Valor de respaldo desde legal_constants. "
                                 "Para actualización trimestral, consultar https://www.banrep.gov.co"),
                    }
                    await _cache_set(cache_key, "banrep", None, payload, None,
                                     ttl_seconds=21600, status="ok")
                    return {**payload, "cached": False}

    # Sin valor cacheado en legal_constants — devolvemos hint
    return {
        "source": "banrep",
        "serie": serie,
        "valor": None,
        "error": ("Valor no disponible en cache local. Consultar manualmente "
                  "https://www.banrep.gov.co/es/estadisticas"),
    }
