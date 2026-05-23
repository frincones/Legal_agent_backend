"""
Sprint L-DOC: Endpoint generico para ingestar 1 URL especifica.

POST /admin/pipeline/ingest-url
  Body: { source, url, doc_type, title? }
  - Descarga URL (PDF o HTML)
  - Extrae texto
  - Chunks + embeddings text-embedding-3-small
  - Persiste en documents + chunks (con vector)
  - Devuelve doc_id + count chunks

Permite validar end-to-end por fuente sin tener que implementar scrapers completos.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from utils.db import get_storage
from utils.llm import get_openai_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/pipeline", tags=["admin-ingest"])

HTTP_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"


class IngestURLRequest(BaseModel):
    source: str = Field(..., min_length=2, max_length=50)
    url: str = Field(..., min_length=10)
    doc_type: str = Field(default="generic")
    title: str | None = None
    materia: str | None = None


async def _require_session(request: Request) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    return {"token": auth[7:]}


def _extract_text(content_bytes: bytes, content_type: str, url: str) -> str:
    """Extrae texto plano desde HTML o PDF."""
    ct = (content_type or "").lower()
    is_pdf = "pdf" in ct or url.lower().endswith(".pdf")

    if is_pdf:
        try:
            # Intentar con pypdf
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content_bytes))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
            return text.strip()
        except Exception as e:
            logger.warning("pypdf extract failed: %s, trying alternative", e)
            try:
                # Fallback: docling si esta disponible
                from ingestion.readers.docling_reader import DoclingReader
                import tempfile
                import os
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                    f.write(content_bytes)
                    tmp_path = f.name
                reader = DoclingReader()
                doc = asyncio.run(reader.read(tmp_path)) if not asyncio.iscoroutinefunction(reader.read) else None
                os.unlink(tmp_path)
                return doc.text if doc else ""
            except Exception as e2:
                logger.error("docling fallback failed: %s", e2)
                return ""
    else:
        # HTML
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content_bytes.decode("utf-8", errors="ignore"), "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            # Collapse whitespace
            text = re.sub(r'\n{3,}', '\n\n', text)
            text = re.sub(r' {2,}', ' ', text)
            return text.strip()
        except Exception as e:
            logger.warning("HTML parse failed: %s", e)
            return content_bytes.decode("utf-8", errors="ignore")


def _chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    if not text:
        return []
    chunks = []
    for i in range(0, len(text), chunk_size - overlap):
        chunk = text[i:i + chunk_size].strip()
        if len(chunk) > 100:
            chunks.append(chunk)
    return chunks


@router.post("/ingest-url")
async def ingest_url(
    req: IngestURLRequest,
    _claims: dict = Depends(_require_session),
) -> dict[str, Any]:
    """
    Ingesta 1 URL real: descarga + extract + chunk + embed + persist.
    Devuelve resultado detallado.
    """
    storage = await get_storage()
    pool = storage.pool

    # 1. Descargar
    try:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(req.url)
            if resp.status_code != 200:
                return {
                    "ok": False,
                    "stage": "fetch",
                    "error": f"HTTP {resp.status_code}",
                    "url": req.url,
                }
            content_bytes = resp.content
            content_type = resp.headers.get("content-type", "")
    except Exception as e:
        return {"ok": False, "stage": "fetch", "error": str(e)[:200], "url": req.url}

    byte_size = len(content_bytes)

    # 2. Extraer texto
    try:
        text = _extract_text(content_bytes, content_type, req.url)
    except Exception as e:
        return {"ok": False, "stage": "extract", "error": str(e)[:200]}

    if not text or len(text) < 200:
        return {
            "ok": False,
            "stage": "extract",
            "error": f"text too short ({len(text)} chars)",
            "byte_size_downloaded": byte_size,
            "content_type": content_type,
        }

    # 3. Chunkear
    chunks = _chunk_text(text)
    if not chunks:
        return {"ok": False, "stage": "chunk", "error": "no chunks generated"}

    # 4. Embeddings
    try:
        client = get_openai_client()
        # OpenAI accept hasta 2048 inputs por batch
        # Limit chunks a 30 (mas que suficiente para 1 doc grande)
        chunks_to_embed = chunks[:30]
        emb_resp = await client.embeddings.create(
            model="text-embedding-3-small",
            input=chunks_to_embed,
        )
        embeddings = [e.embedding for e in emb_resp.data]
    except Exception as e:
        return {"ok": False, "stage": "embed", "error": str(e)[:200]}

    # 5. Persistir
    title = req.title or req.url.split("/")[-1][:200] or f"{req.source}-doc"
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                doc_row = await conn.fetchrow("""
                    INSERT INTO documents (title, source, content, doc_type, metadata)
                    VALUES ($1, $2, $3, $4, $5::jsonb)
                    RETURNING id
                """,
                    title,
                    req.source,
                    text[:10000],
                    req.doc_type,
                    json.dumps({
                        "url": req.url,
                        "materia": req.materia,
                        "content_type": content_type,
                        "byte_size": byte_size,
                    }),
                )
                doc_id = str(doc_row["id"])

                for idx, (chunk, emb) in enumerate(zip(chunks_to_embed, embeddings)):
                    emb_str = "[" + ",".join(str(x) for x in emb) + "]"
                    await conn.execute("""
                        INSERT INTO chunks (document_id, content, embedding, chunk_index, token_count, metadata)
                        VALUES ($1::uuid, $2, $3::vector, $4, $5, $6::jsonb)
                    """,
                        doc_id, chunk, emb_str, idx, len(chunk) // 4,
                        json.dumps({"source": req.source}),
                    )
    except Exception as e:
        return {"ok": False, "stage": "persist", "error": str(e)[:300]}

    return {
        "ok": True,
        "doc_id": doc_id,
        "title": title,
        "source": req.source,
        "byte_size_downloaded": byte_size,
        "text_length": len(text),
        "chunks_created": len(chunks_to_embed),
        "url": req.url,
        "preview": text[:300],
    }


# ─── Batch endpoint para testear multiple sources de una vez ─────────

SOURCE_TEST_PAYLOADS = [
    {
        "source": "defensoria",
        "url": "https://www.defensoria.gov.co/documents/20123/3783083/Cartilla-Medio-Ambiente-08-04-2026.pdf",
        "doc_type": "cartilla",
        "title": "Cartilla Medio Ambiente - Defensoria del Pueblo",
        "materia": "constitucional",
    },
    {
        "source": "icbf",
        "url": "https://www.icbf.gov.co/system/files/procesos/pt3.rc_protocolo_general_de_servicio_y_atencion_al_ciudadano_v2.pdf",
        "doc_type": "protocolo",
        "title": "Protocolo General Servicio y Atencion Ciudadano - ICBF",
        "materia": "familia",
    },
    {
        "source": "corte_suprema",
        "url": "https://cortesuprema.gov.co/sala-de-casacion-civil-y-agraria-relatoria-responsabilidad-civil-1886-2024/",
        "doc_type": "relatoria",
        "title": "Relatoria Responsabilidad Civil 1886-2024 - CSJ Sala Civil",
        "materia": "civil",
    },
    {
        "source": "dian",
        "url": "https://normograma.dian.gov.co/dian/",
        "doc_type": "compilacion_juridica",
        "title": "Compilacion Juridica DIAN - portal indice",
        "materia": "tributario",
    },
    {
        "source": "mintrabajo",
        "url": "https://www.mintrabajo.gov.co/normatividad/leyes",
        "doc_type": "indice_normativo",
        "title": "Indice Leyes - Ministerio del Trabajo",
        "materia": "laboral",
    },
    {
        "source": "minjusticia",
        "url": "https://www.minjusticia.gov.co/programas-co/MASC",
        "doc_type": "portal_masc",
        "title": "Portal MASC - Conciliacion y Arbitraje",
        "materia": "civil",
    },
]


@router.post("/test-ingest-batch")
async def test_ingest_batch(
    _claims: dict = Depends(_require_session),
) -> dict[str, Any]:
    """
    Test batch: ingesta 1 doc real de cada una de las 6 fuentes con URLs validadas.
    Devuelve resultado por fuente (ok / error / stage).
    """
    results = []
    for payload in SOURCE_TEST_PAYLOADS:
        req = IngestURLRequest(**payload)
        try:
            r = await ingest_url(req, _claims={})
        except Exception as e:
            r = {"ok": False, "error": str(e)[:300], "stage": "exception"}
        r["source"] = payload["source"]
        results.append(r)
        await asyncio.sleep(2)  # throttle entre fuentes

    total = len(results)
    passed = sum(1 for r in results if r.get("ok"))
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "results": results,
    }
