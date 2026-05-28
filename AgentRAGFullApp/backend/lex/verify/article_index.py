"""M19.24.B.2 — Validador de existencia de artículos por código legal.

Permite detectar citas inexistentes como "Art. 836 CGP" (el CGP llega al 627).
Consulta la tabla `article_index` (M19.24.B.1) que tiene el max_articulo
por cada código normativo colombiano.

Usado por:
  - legal_classifier (M19.24.B): valida las citas del prompt del usuario
  - context_enrichment (M19.24.E.2): valida citas extraídas del intent

Reproduce el "Paso 1" de Claude (crítica del input antes de actuar).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ArticleVerdict:
    """Resultado de validar Art. X de Ley Y."""
    cita_original: str               # "Art. 836 CGP"
    ley_resolved: Optional[str]      # "CGP" (normalizado tras alias lookup)
    articulo_solicitado: Optional[int]  # 836
    exists: bool                      # True si articulo <= max_articulo
    max_articulo_in_law: Optional[int]  # 627 (lo que dice la tabla)
    ley_nombre: Optional[str]         # "Código General del Proceso"
    ley_numero: Optional[str]         # "Ley 1564 de 2012"
    suggested_correction: Optional[str]  # "el último artículo del CGP es 627"
    fuente_url: Optional[str]
    parse_ok: bool                    # False si no pudo parsear la cita

    def to_dict(self) -> dict:
        return asdict(self)


# Regex para parsear citas tipo "Art. X CGP", "Artículo X CC", "Art. X de la Ley Y de Z"
_ARTICULO_PATTERNS = [
    # "Art. 836 CGP" / "Artículo 836 CGP" / "Art 836 del CGP"
    re.compile(
        r"art(?:[íi]culo|\.?)\s+(?P<num>\d{1,5})\s+(?:de(?:l)?\s+)?(?:la\s+)?(?P<ley>[A-Za-zÁÉÍÓÚáéíóúñÑ\.\s/]{2,80})",
        re.IGNORECASE,
    ),
    # "Art. 154 Código Civil"
    re.compile(
        r"art(?:[íi]culo|\.?)\s+(?P<num>\d{1,5})\s+(?P<ley>c[óo]digo\s+[a-záéíóúñ]+(?:\s+[a-záéíóúñ]+)?)",
        re.IGNORECASE,
    ),
]


def parse_article_citation(cita: str) -> tuple[Optional[int], Optional[str]]:
    """Parsea una cita tipo 'Art. X Ley' → (numero, ley_string).

    Returns (None, None) si no se puede parsear.
    """
    if not cita or not isinstance(cita, str):
        return None, None
    cita_clean = cita.strip()
    for pat in _ARTICULO_PATTERNS:
        m = pat.search(cita_clean)
        if m:
            try:
                num = int(m.group("num"))
                ley = m.group("ley").strip()
                # Limpiar ley de palabras vacías comunes
                ley = re.sub(r"\s+(de|del|la)\s+", " ", ley).strip()
                ley = re.sub(r"\s+", " ", ley)
                return num, ley
            except Exception:
                continue
    return None, None


async def _lookup_ley_code(pool, ley_string: str) -> Optional[dict]:
    """Encuentra el row de article_index que matchea por ley_codigo o alias.

    Args:
        pool: asyncpg Pool
        ley_string: e.g. "CGP", "Código General del Proceso", "Ley 1564"

    Returns dict con {ley_codigo, ley_nombre, ley_numero, max_articulo,
    fuente_url} o None.
    """
    if pool is None or not ley_string:
        return None
    ley_normalized = ley_string.strip().lower()
    try:
        async with pool.acquire() as conn:
            # Match exacto por código (lo más rápido)
            row = await conn.fetchrow(
                """
                SELECT ley_codigo, ley_nombre, ley_numero, max_articulo, fuente_url, ley_alias
                FROM article_index
                WHERE lower(ley_codigo) = $1
                """,
                ley_normalized,
            )
            if row:
                return dict(row)
            # Match por alias (cualquiera del array JSONB)
            row = await conn.fetchrow(
                """
                SELECT ley_codigo, ley_nombre, ley_numero, max_articulo, fuente_url, ley_alias
                FROM article_index
                WHERE EXISTS (
                  SELECT 1 FROM jsonb_array_elements_text(ley_alias) AS a
                  WHERE lower(a) = $1
                )
                """,
                ley_normalized,
            )
            if row:
                return dict(row)
            # Match LIKE por nombre (más laxo)
            row = await conn.fetchrow(
                """
                SELECT ley_codigo, ley_nombre, ley_numero, max_articulo, fuente_url, ley_alias
                FROM article_index
                WHERE lower(ley_nombre) LIKE $1
                LIMIT 1
                """,
                f"%{ley_normalized}%",
            )
            if row:
                return dict(row)
    except Exception as e:
        logger.debug("article_index lookup failed for '%s': %s", ley_string, e)
    return None


async def verify_article_exists(pool, cita: str) -> ArticleVerdict:
    """Verifica si una cita tipo 'Art. X Y' es válida.

    Args:
        pool: asyncpg Pool
        cita: cita textual del usuario, ej. "Art. 836 CGP"

    Returns ArticleVerdict con el dictamen.
    """
    num, ley = parse_article_citation(cita)
    if num is None or ley is None:
        return ArticleVerdict(
            cita_original=cita,
            ley_resolved=None,
            articulo_solicitado=None,
            exists=False,
            max_articulo_in_law=None,
            ley_nombre=None,
            ley_numero=None,
            suggested_correction=None,
            fuente_url=None,
            parse_ok=False,
        )

    row = await _lookup_ley_code(pool, ley)
    if row is None:
        # Ley desconocida → no podemos validar, devolver "exists=True" para
        # no bloquear (no asumimos error). El judge_agent del verifier
        # tendrá la última palabra.
        return ArticleVerdict(
            cita_original=cita,
            ley_resolved=None,
            articulo_solicitado=num,
            exists=True,
            max_articulo_in_law=None,
            ley_nombre=None,
            ley_numero=None,
            suggested_correction=None,
            fuente_url=None,
            parse_ok=True,
        )

    max_art = int(row["max_articulo"]) if row.get("max_articulo") else 0
    exists = (num <= max_art) if max_art > 0 else True

    suggested = None
    if not exists:
        suggested = (
            f"El artículo {num} no existe en el {row['ley_nombre']} "
            f"({row.get('ley_numero', '')}). El último artículo es el {max_art}. "
            f"Verifica la cita en {row.get('fuente_url') or 'fuentes oficiales'}."
        )

    return ArticleVerdict(
        cita_original=cita,
        ley_resolved=row["ley_codigo"],
        articulo_solicitado=num,
        exists=exists,
        max_articulo_in_law=max_art,
        ley_nombre=row.get("ley_nombre"),
        ley_numero=row.get("ley_numero"),
        suggested_correction=suggested,
        fuente_url=row.get("fuente_url"),
        parse_ok=True,
    )


async def verify_article_batch(pool, citas: list[str]) -> list[ArticleVerdict]:
    """Verifica un batch de citas. Útil para legal_classifier.

    Args:
        pool: asyncpg Pool
        citas: lista de cita strings

    Returns lista de ArticleVerdict (mismo orden).
    """
    out: list[ArticleVerdict] = []
    for c in citas[:30]:  # limit razonable
        try:
            v = await verify_article_exists(pool, c)
            out.append(v)
        except Exception as e:
            logger.warning("verify_article_exists failed for '%s': %s", c, e)
            out.append(ArticleVerdict(
                cita_original=c,
                ley_resolved=None,
                articulo_solicitado=None,
                exists=True,
                max_articulo_in_law=None,
                ley_nombre=None,
                ley_numero=None,
                suggested_correction=None,
                fuente_url=None,
                parse_ok=False,
            ))
    return out


# Re-export helpers para reuso
__all__ = [
    "ArticleVerdict",
    "parse_article_citation",
    "verify_article_exists",
    "verify_article_batch",
]
