"""Sprint M19.1 · Ingesta masiva Corte Constitucional (~28k sentencias).

Itera años 1992-2026 × tipos {T, C, SU, A} y descubre URLs
predecibles `corteconstitucional.gov.co/relatoria/{año}/{TIPO}-{N}-{YY}.htm`.

Patrón observado:
  T-001-92.htm hasta T-NNN-YY.htm (T: tutelas, ~varias/día)
  C-001-92.htm hasta C-NNN-YY.htm (C: constitucionalidad)
  SU001-92.htm hasta SUNNN-YY.htm (SU: sin guión)
  A-001-92.htm hasta A-NNN-YY.htm (A: autos)

Por cada URL: GET, parsear título + rubro + magistrado, INSERT en
tabla `jurisprudencia` (idempotente con ON CONFLICT).

USO:
    DATABASE_URL=... python scripts/ingest_corte_constitucional_bulk.py \\
        --year 2020 \\
        --tipo T \\
        --max-num 1500 \\
        --concurrency 5

ETA:
    1 año × 4 tipos × ~2000 sentencias / 5 concurrentes / 1s c/u
    = ~6-8 horas para un año completo
    Para 1992-2026: ~7-10 días continuos (recomendado worker en Railway).

ESTADO: SKELETON LISTO, NO EJECUTADO TODAVÍA.
Requiere validación de patrón URL contra ~50 sentencias antes de full backfill.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Optional

import asyncpg
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("ingest_cc")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BASE_URL = "https://www.corteconstitucional.gov.co/relatoria"
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LexAI-Ingester/1.0)"}


def build_url(tipo: str, numero: int, anio: int) -> str:
    yy = str(anio)[-2:]
    if tipo == "SU":
        return f"{BASE_URL}/{anio}/{tipo}{numero}-{yy}.htm"
    num_str = f"{numero:03d}" if numero < 1000 else str(numero)
    return f"{BASE_URL}/{anio}/{tipo}-{num_str}-{yy}.htm"


async def fetch_one(client, tipo, numero, anio, pool, sem):
    """Fetch + parse + insert una sentencia."""
    url = build_url(tipo, numero, anio)
    providencia = f"{tipo}{numero}/{str(anio)[-2:]}" if tipo == "SU" else f"{tipo}-{numero}/{str(anio)[-2:]}"

    async with sem:
        try:
            r = await client.get(url, timeout=HTTP_TIMEOUT, headers=HEADERS, follow_redirects=True)
            if r.status_code != 200 or len(r.text) < 5000:
                return None
            html = r.text

            # Extracción minimal (BeautifulSoup mejor en prod)
            import re
            magistrado_match = re.search(r"Magistrad[oa] [Pp]onente[:\s]+([A-ZÁÉÍÓÚÑ][^<\n]{5,120})", html)
            rubro_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)

            magistrado = magistrado_match.group(1).strip() if magistrado_match else None
            rubro = rubro_match.group(1).strip()[:300] if rubro_match else providencia

            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO jurisprudencia
                      (providencia, tipo, numero, anio, rubro, magistrado_ponente,
                       fuente_url, source, fetched_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, 'cc_scraper_m19', now())
                    ON CONFLICT (providencia) DO UPDATE
                      SET fuente_url = EXCLUDED.fuente_url,
                          rubro = COALESCE(jurisprudencia.rubro, EXCLUDED.rubro),
                          fetched_at = now()
                    """,
                    providencia, tipo, str(numero), anio, rubro, magistrado, url,
                )
            return providencia
        except Exception as e:
            logger.debug("fetch failed %s: %s", url, e)
            return None


async def main(year: int, tipo: str, max_num: int, concurrency: int):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set"); return 1

    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=concurrency + 2)
    sem = asyncio.Semaphore(concurrency)
    inserted = []

    async with httpx.AsyncClient() as client:
        tasks = [fetch_one(client, tipo, n, year, pool, sem) for n in range(1, max_num + 1)]
        for i in range(0, len(tasks), 50):
            batch = tasks[i:i + 50]
            results = await asyncio.gather(*batch, return_exceptions=True)
            for r in results:
                if isinstance(r, str):
                    inserted.append(r)
            logger.info("Year %d %s: %d/%d processed, %d inserted",
                        year, tipo, i + len(batch), len(tasks), len(inserted))

    await pool.close()
    logger.info("DONE: Year %d %s -> %d sentencias en BD", year, tipo, len(inserted))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--tipo", choices=["T", "C", "SU", "A"], required=True)
    p.add_argument("--max-num", type=int, default=1500)
    p.add_argument("--concurrency", type=int, default=5)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.year, args.tipo, args.max_num, args.concurrency)))
