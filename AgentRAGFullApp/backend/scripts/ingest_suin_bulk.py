"""Sprint M19.3 · Ingesta masiva SUIN-Juriscol (~11k normas).

SUIN-Juriscol expone documentos en URLs predecibles:
  /viewDocument.asp?id=N donde N va de ~1 a ~30050000+ (con huecos)
  /viewDocument.asp?ruta=Leyes/NNNN/AAAA

Estrategia: enumerar IDs comunes + scrape de índices A-Z.

USO:
    DATABASE_URL=... python scripts/ingest_suin_bulk.py --range-start 1 --range-end 100000

ETA:
    Discovery cubre ~3M docs potenciales pero solo ~11k son leyes/decretos.
    Bloque de 100k IDs/día con rate limit suave = ~10-15 días total.

ESTADO: SKELETON — requiere mapping previo de qué IDs son normas vs otros.
Probablemente más rentable usar el feed de "leyes y normas nuevas" de SUIN.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("ingest_suin")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main(start: int, end: int, concurrency: int):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set"); return 1

    logger.warning(
        "M19.3 ingest_suin_bulk: SKELETON — pendiente:\n"
        "  1. Identificar bloques de IDs que correspondan a leyes (vs decretos vs otros)\n"
        "  2. Parser HTML SUIN para extraer numero/anio/titulo\n"
        "  3. INSERT idempotente en leyes_normas (UPSERT por tipo+numero+anio)\n"
        "  4. Mejor approach: ingestar desde 'tabla A-Z' de SUIN antes del enumerate brute-force\n"
        "Implementar tras validar el approach con ~100 documentos."
    )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--range-start", type=int, default=1)
    p.add_argument("--range-end", type=int, default=100000)
    p.add_argument("--concurrency", type=int, default=3)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.range_start, args.range_end, args.concurrency)))
