"""SECOP scraper · pulls real public contracts from datos.gov.co.

Data source: https://www.datos.gov.co/Gastos-Gubernamentales/SECOP-II-Contratos-Electr-nicos/jbjy-vk9h
API:        SODA 2.1 (REST JSON, paginated, no auth required, public open data)

We pull the most relevant fields for templates (objeto, justificacion,
proceso_de_compra, etc.) and emit them as TemplateCandidate rows. The LLM
enrichment step later classifies materia (mostly 'comercial' or
'administrativo') and extracts clauses.

Legal: SECOP is mandated public data under Ley 1712/2014 (Transparency).
Bulk download is explicitly allowed. We use a polite User-Agent.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from .base import TemplateCandidate, TemplateSourceBase

logger = logging.getLogger(__name__)

# SODA endpoint · SECOP II contracts dataset (jbjy-vk9h).
# Returns JSON · default page size 1000 · supports $where, $select, $offset.
SECOP_API_URL = "https://www.datos.gov.co/resource/jbjy-vk9h.json"

# Conservative · pulling more than this in one run risks rate limits.
DEFAULT_PAGE_SIZE = 200
HTTP_TIMEOUT = 30.0


class SecopScraper(TemplateSourceBase):
    name = "secop"
    description = "Contratos electrónicos públicos · SECOP II (datos.gov.co)"
    base_url = SECOP_API_URL
    request_delay_seconds = 1.5      # SODA tolerates up to ~10 req/sec but be nice

    def __init__(self, *, page_size: int = DEFAULT_PAGE_SIZE, where_clause: Optional[str] = None):
        self.page_size = page_size
        # Example filter: only contracts > 50M COP to skip noise.
        # Default = no filter · let enrichment decide quality.
        self.where_clause = where_clause

    async def fetch(self, *, limit: int = 100) -> AsyncIterator[TemplateCandidate]:
        try:
            import httpx
        except ImportError:
            logger.error("httpx not installed · skipping SECOP scraper")
            return

        emitted = 0
        offset = 0
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": self.user_agent},
        ) as client:
            while emitted < limit:
                params: dict[str, str] = {
                    "$limit": str(min(self.page_size, limit - emitted)),
                    "$offset": str(offset),
                    "$order": ":id",   # deterministic pagination
                }
                if self.where_clause:
                    params["$where"] = self.where_clause

                try:
                    resp = await client.get(SECOP_API_URL, params=params)
                    resp.raise_for_status()
                    rows = resp.json()
                except Exception as e:
                    logger.warning("secop · page offset=%d failed: %s", offset, e)
                    break

                if not rows:
                    break

                for row in rows:
                    if emitted >= limit:
                        break
                    cand = self._row_to_candidate(row)
                    if cand:
                        yield cand
                        emitted += 1

                offset += len(rows)
                if len(rows) < self.page_size:
                    break
                await self.polite_sleep()

    def _row_to_candidate(self, row: dict) -> Optional[TemplateCandidate]:
        """Map a SECOP SODA row to a TemplateCandidate. Returns None to skip."""
        # Field names vary slightly by dataset version · be defensive.
        objeto = (row.get("descripcion_del_proceso") or row.get("objeto_del_contrato") or "").strip()
        if not objeto or len(objeto) < 50:
            return None

        proc_id = row.get("id_contrato") or row.get("id_proceso") or row.get(":id")
        if not proc_id:
            return None

        url = row.get("urlproceso", {})
        if isinstance(url, dict):
            url = url.get("url")

        # Build a minimal markdown body that captures the contract essentials.
        parts: list[str] = []
        parts.append(f"# Contrato SECOP · {proc_id}\n")

        entidad = row.get("nombre_entidad") or row.get("entidad")
        if entidad:
            parts.append(f"**Entidad:** {entidad}")

        modalidad = row.get("modalidad_de_contratacion") or row.get("modalidad")
        if modalidad:
            parts.append(f"**Modalidad:** {modalidad}")

        tipo = row.get("tipo_de_contrato")
        if tipo:
            parts.append(f"**Tipo de contrato:** {tipo}")

        valor = row.get("valor_del_contrato")
        if valor:
            parts.append(f"**Valor:** {valor}")

        plazo = row.get("plazo_de_ejec_n") or row.get("duracion_del_contrato")
        if plazo:
            parts.append(f"**Plazo:** {plazo}")

        proveedor = row.get("proveedor_adjudicado") or row.get("nombre_del_proveedor")
        if proveedor:
            parts.append(f"**Proveedor:** {proveedor}")

        parts.append("\n## Objeto del contrato\n")
        parts.append(self.normalize_whitespace(objeto))

        justificacion = (row.get("justificacion_modalidad_de") or "").strip()
        if justificacion and len(justificacion) > 30:
            parts.append("\n## Justificación\n")
            parts.append(self.normalize_whitespace(justificacion))

        normalized = "\n".join(parts).strip()

        # SECOP contracts default to materia=administrativo (gov contracts);
        # purely commercial/civil mappings are decided by LLM enrichment.
        return TemplateCandidate(
            source=self.name,
            source_ref=str(proc_id),
            source_url=url if isinstance(url, str) else None,
            raw_text=str(row),
            normalized_md=normalized,
            suggested_materia="administrativo",
            suggested_doc_type="contrato",
            metadata={
                "secop_modalidad": modalidad,
                "secop_tipo": tipo,
                "secop_entidad": entidad,
                "secop_valor": valor,
            },
        )
