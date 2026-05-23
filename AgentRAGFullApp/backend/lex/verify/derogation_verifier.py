"""Verifica vigencia de normas citadas contra tabla derogaciones / leyes_normas.

Wrapper sobre el sistema existente derogation/ que retorna un dict simple
por norma: { vigente: bool, derogada_por: str | null, fecha_derogacion: str | null }
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class DerogationCheckResult:
    norma: str
    vigente: bool
    derogada_por: str | None = None
    fecha_derogacion: str | None = None
    confidence: float = 1.0


# Regex para extraer ID normativo de una cita: "Art. 64 CST", "Ley 50 de 1990", etc.
_LEY_PATTERN = re.compile(r"[Ll]ey\s+(\d+)\s+de\s+(\d{4})")
_DECRETO_PATTERN = re.compile(r"[Dd]ecreto\s+(\d+)\s+de\s+(\d{4})")
_ARTICULO_PATTERN = re.compile(r"[Aa]rt(?:ículo|iculo|\.)\s*(\d+)\s*(?:del?\s*)?([A-Z]{1,5})?")


class DerogationVerifier:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def check(self, norma_text: str) -> DerogationCheckResult:
        """Verifica si una norma citada está vigente.

        Best-effort: usa tabla leyes_normas o derogaciones si existen.
        Si no encuentra info, asume vigente (más informativo que blocking).
        """
        # Intentar match exacto contra leyes_normas
        try:
            ley_match = _LEY_PATTERN.search(norma_text)
            decreto_match = _DECRETO_PATTERN.search(norma_text)

            tipo, numero, anio = None, None, None
            if ley_match:
                tipo = "ley"
                numero = int(ley_match.group(1))
                anio = int(ley_match.group(2))
            elif decreto_match:
                tipo = "decreto"
                numero = int(decreto_match.group(1))
                anio = int(decreto_match.group(2))

            if tipo and numero and anio:
                async with self.pool.acquire() as conn:
                    # Buscar en leyes_normas (si existe)
                    try:
                        row = await conn.fetchrow("""
                            SELECT vigente, derogada_por, fecha_derogacion
                            FROM leyes_normas
                            WHERE tipo = $1 AND numero = $2 AND anio = $3
                            LIMIT 1
                        """, tipo, numero, anio)
                        if row:
                            return DerogationCheckResult(
                                norma=norma_text,
                                vigente=bool(row["vigente"]),
                                derogada_por=row["derogada_por"],
                                fecha_derogacion=str(row["fecha_derogacion"]) if row["fecha_derogacion"] else None,
                                confidence=0.95,
                            )
                    except Exception:
                        pass  # tabla no existe o columnas distintas
        except Exception as e:
            logger.debug("derogation check exception for %s: %s", norma_text, e)

        # Fallback: asumir vigente
        return DerogationCheckResult(norma=norma_text, vigente=True, confidence=0.5)

    async def check_batch(self, normas: list[str]) -> list[DerogationCheckResult]:
        results = []
        for n in normas:
            results.append(await self.check(n))
        return results
