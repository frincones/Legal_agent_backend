"""F1 · smoke test for document deep analysis.

Extracts entities from the demo seeded matter_documents to validate the
end-to-end path (LLM JSON-mode + persistence + matter_parties autofill).

Requires OPENAI_API_KEY + DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.db import get_storage, close_storage  # noqa: E402


async def _resolve_demo_ids() -> dict:
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        firm = await conn.fetchrow(
            "select id from firms where razon_social ilike '%López%' or razon_social ilike '%Lopez%' limit 1"
        )
        if not firm:
            raise RuntimeError("Demo firm 'López & Asociados' no encontrado")
        firm_id = str(firm["id"])
        user = await conn.fetchrow(
            "select id from users where firm_id = $1::uuid order by created_at limit 1",
            firm_id,
        )
        # Pick a document with resumen_ia (seeded)
        doc = await conn.fetchrow(
            "select id, matter_id, titulo from matter_documents "
            "where firm_id = $1::uuid and resumen_ia is not null and length(resumen_ia) > 80 "
            "order by created_at desc limit 1",
            firm_id,
        )
        if not doc:
            raise RuntimeError("No hay documentos con resumen_ia para extraer")
    return {
        "firm_id": firm_id,
        "user_id": str(user["id"]),
        "document_id": str(doc["id"]),
        "matter_id": str(doc["matter_id"]),
        "titulo": doc["titulo"],
    }


async def main() -> int:
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY no configurada", file=sys.stderr)
        return 2
    ids = await _resolve_demo_ids()
    print(f"Doc: {ids['titulo']}  ({ids['document_id']})")

    from agent.tools.document_analysis import extract_document_entities_tool

    fails: list[str] = []

    # Caso 1: extracción cold (regenerate=true para evitar caché)
    print("\n[1] Cold extraction (regenerate=true)...")
    r1 = await extract_document_entities_tool(
        args={"document_id": ids["document_id"], "regenerate": True},
        ctx={"firm_id": ids["firm_id"], "user_id": ids["user_id"], "session_id": "test"},
    )
    if r1.get("error"):
        print(f"   FAIL: {r1['error']}")
        fails.append("cold")
    else:
        print(f"   OK · confidence={r1.get('confidence_score'):.2f} · "
              f"parties={r1.get('parties_count')} · "
              f"oblig={r1.get('obligations_count')} · "
              f"incons={r1.get('inconsistencies_count')} · "
              f"matter_parties_inserted={r1.get('matter_parties_inserted')} · "
              f"{r1.get('duration_ms')}ms")

    # Caso 2: cached (sin regenerate)
    print("\n[2] Cached extraction...")
    r2 = await extract_document_entities_tool(
        args={"document_id": ids["document_id"]},
        ctx={"firm_id": ids["firm_id"], "user_id": ids["user_id"], "session_id": "test"},
    )
    if r2.get("error"):
        print(f"   FAIL: {r2['error']}")
        fails.append("cached")
    elif not r2.get("cached"):
        print(f"   FAIL: esperaba cached=true, obtuvo {r2}")
        fails.append("cached_flag")
    else:
        print(f"   OK · cached={r2.get('cached')} · same id={r2.get('id')}")

    # Caso 3: documento inexistente
    print("\n[3] Documento inexistente...")
    r3 = await extract_document_entities_tool(
        args={"document_id": "00000000-0000-0000-0000-000000000000"},
        ctx={"firm_id": ids["firm_id"], "user_id": ids["user_id"]},
    )
    if not r3.get("error"):
        print(f"   FAIL: esperaba error pero devolvió {r3}")
        fails.append("not_found")
    else:
        print(f"   OK · rechazado: {r3['error']}")

    # Caso 4: inspeccionar la fila persistida
    print("\n[4] Verificar persistencia...")
    storage = await get_storage()
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            "select status, parties_jsonb, dates_jsonb, hechos_clave, "
            "       confidence_score, model_used, prompt_version "
            "from document_extractions where id = $1::uuid",
            r1.get("id"),
        )
    if row and row["status"] == "completed":
        print(f"   OK · model={row['model_used']} · prompt={row['prompt_version']}")
    else:
        print(f"   FAIL: persist row inválida")
        fails.append("persist")

    await close_storage()
    print(f"\n{4 - len(fails)}/4 cases OK ({len(fails)} failed)")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
