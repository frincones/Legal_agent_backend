"""Sprint 11 · Document Q&A API.

Chat sobre uno o varios documentos del caso. Reutiliza el pipeline RAG
sobre `chunks` cuando el documento esta ingestado; si no, usa resumen_ia
del matter_document como contexto directo.

  POST /v1/doc-qa/sessions            · crea sesion (scope: document|matter|custom)
  GET  /v1/doc-qa/sessions
  GET  /v1/doc-qa/sessions/{id}        · cabecera + mensajes
  POST /v1/doc-qa/sessions/{id}/ask    · turno usuario → respuesta assistant
  DELETE /v1/doc-qa/sessions/{id}
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from utils.auth import Principal, get_current_firm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/doc-qa", tags=["doc_qa"])


def _serialize_session(r) -> dict:
    return {
        "id": str(r["id"]),
        "user_id": str(r["user_id"]) if r["user_id"] else None,
        "matter_id": str(r["matter_id"]) if r["matter_id"] else None,
        "scope_kind": r["scope_kind"],
        "scope_document_ids": [str(x) for x in (r["scope_document_ids"] or [])],
        "title": r["title"],
        "message_count": r["message_count"],
        "llm_model": r["llm_model"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
    }


class CreateSessionRequest(BaseModel):
    scope_kind: str = Field(default="document", pattern="^(document|matter|custom)$")
    scope_document_ids: list[str] = Field(default_factory=list)
    matter_id: Optional[str] = None
    title: Optional[str] = None


@router.post("/sessions")
async def create_session(
    body: CreateSessionRequest,
    principal: Principal = Depends(get_current_firm),
):
    if body.scope_kind == "document" and not body.scope_document_ids:
        raise HTTPException(400, "scope=document requiere scope_document_ids")
    if body.scope_kind == "matter" and not body.matter_id:
        raise HTTPException(400, "scope=matter requiere matter_id")
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into doc_qa_sessions
              (firm_id, user_id, matter_id, scope_kind, scope_document_ids, title, llm_model)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid[], $6, 'gpt-4o')
            returning id, user_id, matter_id, scope_kind, scope_document_ids,
                      title, message_count, llm_model, created_at, updated_at
            """,
            principal.firm_id, principal.user_id, body.matter_id,
            body.scope_kind, body.scope_document_ids,
            body.title or "Consulta sobre documento",
        )
    return _serialize_session(row)


@router.get("/sessions")
async def list_sessions(
    matter_id: Optional[str] = None,
    limit: int = Query(default=30, le=200),
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    where = ["firm_id = $1::uuid"]
    params: list = [principal.firm_id]
    if matter_id:
        params.append(matter_id); where.append(f"matter_id = ${len(params)}::uuid")
    params.append(limit)
    sql = f"""
        select id, user_id, matter_id, scope_kind, scope_document_ids,
               title, message_count, llm_model, created_at, updated_at
          from doc_qa_sessions
         where {' and '.join(where)}
         order by updated_at desc
         limit ${len(params)}
    """
    async with storage.pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return {"count": len(rows), "items": [_serialize_session(r) for r in rows]}


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        s = await conn.fetchrow(
            """
            select id, user_id, matter_id, scope_kind, scope_document_ids,
                   title, message_count, llm_model, created_at, updated_at
              from doc_qa_sessions
             where id = $1::uuid and firm_id = $2::uuid
            """,
            session_id, principal.firm_id,
        )
        if not s:
            raise HTTPException(404, "not found")
        msgs = await conn.fetch(
            """
            select id, role, content, citations, prompt_tokens, completion_tokens, created_at
              from doc_qa_messages where session_id = $1::uuid
             order by created_at asc
            """,
            session_id,
        )
    return {
        "session": _serialize_session(s),
        "messages": [
            {
                "id": str(m["id"]), "role": m["role"], "content": m["content"],
                "citations": m["citations"],
                "tokens": {"prompt": m["prompt_tokens"], "completion": m["completion_tokens"]},
                "created_at": m["created_at"].isoformat() if m["created_at"] else None,
            }
            for m in msgs
        ],
    }


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=4000)


@router.post("/sessions/{session_id}/ask")
async def ask(
    session_id: str,
    body: AskRequest,
    principal: Principal = Depends(get_current_firm),
):
    """Turno: guarda user message → LLM → guarda assistant message con citations."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")

    # 1. Load session
    async with storage.pool.acquire() as conn:
        session = await conn.fetchrow(
            """
            select id, scope_kind, scope_document_ids, matter_id
              from doc_qa_sessions
             where id = $1::uuid and firm_id = $2::uuid
            """,
            session_id, principal.firm_id,
        )
        if not session:
            raise HTTPException(404, "session not found")
        prev = await conn.fetch(
            """
            select role, content from doc_qa_messages
             where session_id = $1::uuid
             order by created_at asc limit 20
            """,
            session_id,
        )

    # 2. Cargar contexto (texto de los documentos del scope)
    doc_texts: list[dict] = await _load_scope_context(
        principal.firm_id,
        session["scope_kind"],
        list(session["scope_document_ids"] or []),
        str(session["matter_id"]) if session["matter_id"] else None,
    )
    if not doc_texts:
        raise HTTPException(409, "scope sin texto procesable. Verifica que los documentos esten ingestados.")

    context_blob = "\n\n".join(
        f"### Documento {i+1}: {d['titulo']} (id={d['document_id']})\n{d['text'][:8000]}"
        for i, d in enumerate(doc_texts[:5])
    )
    citations_pool = [
        {"document_id": d["document_id"], "titulo": d["titulo"]}
        for d in doc_texts
    ]

    # 3. Guardar user message
    async with storage.pool.acquire() as conn:
        await conn.execute(
            """insert into doc_qa_messages (firm_id, session_id, role, content)
               values ($1::uuid, $2::uuid, 'user', $3)""",
            principal.firm_id, session_id, body.question,
        )

    # 4. LLM
    system_prompt = (
        "Eres un asistente jurídico colombiano respondiendo preguntas SOBRE documentos del cliente. "
        "Usa ÚNICAMENTE la información en los documentos provistos abajo. "
        "Si el dato no está en el documento, dilo explícitamente — NO inventes. "
        "Cuando hagas una afirmación, indica al final del párrafo entre paréntesis "
        "(Doc N) o (Doc N, p. X) cuando sepas la página."
    )
    messages = [{"role": "system", "content": f"{system_prompt}\n\n{context_blob}"}]
    for p in prev:
        messages.append({"role": p["role"], "content": p["content"]})
    messages.append({"role": "user", "content": body.question})

    answer = ""
    prompt_tokens, completion_tokens = 0, 0
    try:
        from utils.llm import get_openai_client
        client = get_openai_client()
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.2,
            max_tokens=1500,
        )
        answer = resp.choices[0].message.content or ""
        prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
        completion_tokens = resp.usage.completion_tokens if resp.usage else 0
    except Exception as e:
        logger.exception("doc_qa llm failed")
        raise HTTPException(502, f"LLM error: {e}")

    # 5. Persist assistant + actualizar message_count
    async with storage.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            insert into doc_qa_messages
              (firm_id, session_id, role, content, citations, prompt_tokens, completion_tokens)
            values ($1::uuid, $2::uuid, 'assistant', $3, $4::jsonb, $5, $6)
            returning id, created_at
            """,
            principal.firm_id, session_id, answer,
            json.dumps(citations_pool), prompt_tokens, completion_tokens,
        )
        await conn.execute(
            """update doc_qa_sessions
                  set message_count = message_count + 2, updated_at = now()
                where id = $1::uuid""",
            session_id,
        )
    return {
        "id": str(row["id"]),
        "answer": answer,
        "citations": citations_pool,
        "tokens": {"prompt": prompt_tokens, "completion": completion_tokens},
    }


async def _load_scope_context(firm_id, scope_kind: str, doc_ids: list, matter_id: Optional[str]) -> list[dict]:
    """Devuelve [{document_id, titulo, text}]."""
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return []
    docs: list[dict] = []
    async with storage.pool.acquire() as conn:
        ids: list[str] = list(doc_ids or [])
        if scope_kind == "matter" and matter_id:
            rows = await conn.fetch(
                "select id from matter_documents where matter_id = $1::uuid and firm_id = $2::uuid limit 10",
                matter_id, firm_id,
            )
            ids = [str(r["id"]) for r in rows]
        for did in ids:
            row = await conn.fetchrow(
                """
                select id, titulo, resumen_ia, ingest_doc_id
                  from matter_documents where id = $1::uuid and firm_id = $2::uuid
                """,
                did, firm_id,
            )
            if not row:
                continue
            text = row["resumen_ia"] or ""
            if not text and row["ingest_doc_id"]:
                chunks = await conn.fetch(
                    "select content from chunks where document_id = $1::text order by chunk_index limit 60",
                    str(row["ingest_doc_id"]),
                )
                text = "\n\n".join(c["content"] for c in chunks if c["content"])
            if text:
                docs.append({"document_id": str(row["id"]), "titulo": row["titulo"], "text": text})
    return docs


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    principal: Principal = Depends(get_current_firm),
):
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        raise HTTPException(503, "storage unavailable")
    async with storage.pool.acquire() as conn:
        await conn.execute(
            "delete from doc_qa_sessions where id = $1::uuid and firm_id = $2::uuid",
            session_id, principal.firm_id,
        )
    return {"deleted": True}


# ════════════════════════════════════════════════════════════════════════
# Voice tool
# ════════════════════════════════════════════════════════════════════════


async def ask_about_document_tool(args: dict, ctx: dict) -> dict:
    """Voice: 'LexAI, ¿qué dice el contrato sobre la cláusula de terminación?'."""
    firm_id = ctx.get("firm_id")
    user_id = ctx.get("user_id")
    question = (args.get("question") or "").strip()
    document_id = args.get("document_id") or ctx.get("document_id")
    matter_id = args.get("matter_id") or ctx.get("matter_id")
    if not (firm_id and question):
        return {"error": "firm_id y question requeridos"}
    if not (document_id or matter_id):
        return {"error": "document_id o matter_id requerido"}

    # Crear sesion ephemera + hacer ask
    from utils.db import get_storage
    storage = await get_storage()
    if not hasattr(storage, "pool"):
        return {"error": "storage no disponible"}

    scope_kind = "document" if document_id else "matter"
    async with storage.pool.acquire() as conn:
        s = await conn.fetchrow(
            """
            insert into doc_qa_sessions
              (firm_id, user_id, matter_id, scope_kind, scope_document_ids, title, llm_model)
            values ($1::uuid, $2::uuid, $3::uuid, $4, $5::uuid[], 'Voice query', 'gpt-4o')
            returning id
            """,
            firm_id, user_id, matter_id, scope_kind,
            [document_id] if document_id else [],
        )

    # Reusar lógica del endpoint /ask vía función directa.
    # NOTE: a nested `class _P: firm_id = firm_id` doesn't work because
    # Python evaluates the RHS in the class-body scope (which has no
    # outer-scope visibility · classic NameError). Use SimpleNamespace.
    from types import SimpleNamespace
    principal = SimpleNamespace(
        firm_id=firm_id,
        user_id=user_id,
        role=ctx.get("role", "lawyer"),
    )
    return await ask(str(s["id"]), AskRequest(question=question), principal)  # type: ignore
