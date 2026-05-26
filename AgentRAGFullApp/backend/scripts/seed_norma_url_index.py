"""Sprint M18 · Seed inicial de `norma_url_index`.

Inserta los mappings hardcoded (_FP_NORMA_ID, _FP_LEY_ID, _CODIGO_ICBF_SLUG)
del módulo `citation_url_builder` en la tabla `norma_url_index` con
`discovered_by='manual'` y `confidence=1.0`.

Después de correr este script, las normas mapeadas son cache hits instantáneos
para todos los firms del SaaS (latencia <50ms vs Brave Search ~800ms).

Uso:
    python scripts/seed_norma_url_index.py [--dry-run] [--validate]

Variables:
    DATABASE_URL: connection string Postgres
    --dry-run    : solo imprime, no inserta
    --validate   : valida cada URL con GET antes de persistir (lento ~1min)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

# Hack para correr desde scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.citation_url_builder import (
    _FP_NORMA_ID,
    _FP_LEY_ID,
    _CODIGO_ICBF_SLUG,
    _CODIGO_URL_SLUG,
)

logger = logging.getLogger("seed_m18")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@dataclass
class SeedEntry:
    kind: str
    tipo: str
    numero: Optional[int]
    anio: Optional[int]
    fuente_url: str
    titulo: str
    discovered_by: str = "manual"
    confidence: float = 1.0
    normalized_ref: str = ""

    def __post_init__(self):
        if not self.normalized_ref:
            if self.numero is not None and self.anio is not None:
                self.normalized_ref = f"{self.tipo} {self.numero}/{self.anio}"
            else:
                self.normalized_ref = self.tipo


def build_seed_entries() -> list[SeedEntry]:
    """Construye entries desde los dicts hardcoded de citation_url_builder."""
    entries: list[SeedEntry] = []

    # --- Constitución ---
    entries.append(SeedEntry(
        kind="codigo",
        tipo="CONSTITUCION",
        numero=None,
        anio=None,
        fuente_url=f"https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i={_FP_NORMA_ID['CONSTITUCION']}",
        titulo="Constitución Política de Colombia 1991",
    ))

    # --- Códigos en Función Pública (C.P., C.CO., CGP, CPACA, CPP) ---
    titulos_fp = {
        "C.P.": "Código Penal Colombia (Ley 599 de 2000)",
        "C.CO.": "Código de Comercio Colombia (Decreto 410 de 1971)",
        "CGP": "Código General del Proceso (Ley 1564 de 2012)",
        "CPACA": "CPACA (Ley 1437 de 2011)",
        "CPP": "Código de Procedimiento Penal (Ley 906 de 2004)",
    }
    for tipo, fp_id in _FP_NORMA_ID.items():
        if tipo == "CONSTITUCION":
            continue
        entries.append(SeedEntry(
            kind="codigo",
            tipo=tipo,
            numero=None,
            anio=None,
            fuente_url=f"https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i={fp_id}",
            titulo=titulos_fp.get(tipo, tipo),
        ))

    # --- Códigos en ICBF mirror (CST, C.C.) ---
    titulos_icbf = {
        "CST": "Código Sustantivo del Trabajo",
        "C.C.": "Código Civil Colombia",
    }
    for tipo, slug in _CODIGO_ICBF_SLUG.items():
        entries.append(SeedEntry(
            kind="codigo",
            tipo=tipo,
            numero=None,
            anio=None,
            fuente_url=f"https://www.icbf.gov.co/cargues/avance/docs/{slug}.html",
            titulo=titulos_icbf.get(tipo, tipo),
        ))

    # --- Leyes con ID FP validado ---
    titulos_leyes = {
        (50, 1990):   "Ley 50 de 1990 — Reforma Laboral",
        (100, 1993):  "Ley 100 de 1993 — Seguridad Social",
        (361, 1997):  "Ley 361 de 1997 — Inclusión personas con discapacidad",
        (712, 2001):  "Ley 712 de 2001 — Código Procesal del Trabajo",
        (789, 2002):  "Ley 789 de 2002 — Reforma Laboral",
        (1010, 2006): "Ley 1010 de 2006 — Acoso laboral",
        (1437, 2011): "Ley 1437 de 2011 — CPACA",
        (1564, 2012): "Ley 1564 de 2012 — Código General del Proceso",
    }
    for (numero, anio), fp_id in _FP_LEY_ID.items():
        entries.append(SeedEntry(
            kind="ley",
            tipo="LEY",
            numero=numero,
            anio=anio,
            fuente_url=f"https://www.funcionpublica.gov.co/eva/gestornormativo/norma.php?i={fp_id}",
            titulo=titulos_leyes.get((numero, anio), f"Ley {numero} de {anio}"),
        ))

    return entries


async def validate_url(url: str) -> tuple[bool, Optional[int]]:
    """GET + soft 404 check."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 LexAI-Seed"}) as client:
            r = await client.get(url)
            body = (r.text or "")[:3000].lower()
            is_soft = (
                "norma_error.php" in str(r.url).lower()
                or "esta página no está disponible" in body
                or "enlace erróneo" in body
            )
            small = len(r.text or "") < 18000
            return (r.status_code == 200 and not is_soft and not small, r.status_code)
    except Exception as e:
        logger.warning("validate failed for %s: %s", url, e)
        return False, None


async def seed(dry_run: bool = False, do_validate: bool = False):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        return 1

    entries = build_seed_entries()
    logger.info("Building %d seed entries", len(entries))

    if do_validate:
        logger.info("Validating URLs (this takes ~1 min)...")
        valid_entries = []
        for e in entries:
            ok, status = await validate_url(e.fuente_url)
            mark = "OK" if ok else f"FAIL ({status})"
            logger.info("  [%s] %s -> %s", mark, e.normalized_ref, e.fuente_url[:80])
            if ok:
                valid_entries.append(e)
        entries = valid_entries
        logger.info("After validation: %d/%d valid", len(entries), len(entries))

    if dry_run:
        for e in entries:
            print(f"WOULD INSERT: kind={e.kind} tipo={e.tipo} num={e.numero} anio={e.anio} -> {e.fuente_url}")
        return 0

    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    try:
        inserted = 0
        skipped = 0
        for e in entries:
            try:
                async with pool.acquire() as conn:
                    result = await conn.execute(
                        """
                        INSERT INTO norma_url_index (
                            kind, tipo, numero, anio, normalized_ref,
                            fuente_url, url_validated, url_http_status,
                            titulo, discovered_by, confidence,
                            last_validated_at, revalidate_after
                        )
                        VALUES ($1, $2, $3, $4, $5,
                                $6, $7, $8,
                                $9, $10, $11,
                                now(), now() + interval '30 days')
                        ON CONFLICT DO NOTHING
                        """,
                        e.kind, e.tipo, e.numero, e.anio, e.normalized_ref,
                        e.fuente_url, True, 200,
                        e.titulo, e.discovered_by, e.confidence,
                    )
                if "INSERT 0 1" in result:
                    inserted += 1
                    logger.info("INSERTED: %s -> %s", e.normalized_ref, e.fuente_url[:80])
                else:
                    skipped += 1
                    logger.info("SKIP (exists): %s", e.normalized_ref)
            except Exception as ex:
                logger.error("Error inserting %s: %s", e.normalized_ref, ex)

        logger.info("Done: %d inserted, %d skipped (%d total entries)",
                    inserted, skipped, len(entries))
    finally:
        await pool.close()
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No insertar, solo imprimir")
    parser.add_argument("--validate", action="store_true", help="GET cada URL antes de persistir")
    args = parser.parse_args()
    sys.exit(asyncio.run(seed(dry_run=args.dry_run, do_validate=args.validate)))


if __name__ == "__main__":
    main()
