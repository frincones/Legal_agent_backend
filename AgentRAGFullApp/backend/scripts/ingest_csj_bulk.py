"""Sprint M19.2 · Ingesta masiva Corte Suprema Justicia (~19k sentencias).

CSJ no tiene URL predecible por sentencia — usa buscador WordPress
y RSS feeds. Estrategia:

1. Para cada sala (Laboral=SL, Civil=SC, Penal=SP):
   - GET RSS: /corte/index.php/feed?cat=NN
   - Por cada entry: extraer providencia + URL + título + fecha
   - INSERT en jurisprudencia (idempotente)
2. Para sentencias antiguas no en RSS: usar relatoria.cortesuprema.gov.co
   con paginación.

USO:
    DATABASE_URL=... python scripts/ingest_csj_bulk.py --sala SL --max-pages 100

ETA:
    ~7000 SL + ~7000 SC + ~5000 SP = ~19k
    100 sentencias por feed page × ~200 pages × 1s = ~7-10 horas

ESTADO: SKELETON — requiere análisis de estructura RSS/feed real antes de full run.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("ingest_csj")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def main(sala: str, max_pages: int):
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set"); return 1

    logger.warning(
        "M19.2 ingest_csj_bulk: SKELETON — pendiente implementación de:\n"
        "  1. Parser RSS de cortesuprema.gov.co/corte/index.php/feed?cat=...\n"
        "  2. Mapeo sala -> categoría WordPress\n"
        "  3. Backfill desde relatoria.cortesuprema.gov.co para histórico\n"
        "  4. INSERT idempotente en tabla jurisprudencia\n"
        "Implementar después de M19.1 validado."
    )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--sala", choices=["SL", "SC", "SP", "STC", "STL", "STP"], required=True)
    p.add_argument("--max-pages", type=int, default=100)
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.sala, args.max_pages)))
