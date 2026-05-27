"""Sprint M19.5 · Persistencia de threads del agente en chat_messages.

Convierte el buffer de agent_thoughts (recolectado durante el run) en
2 rows en chat_messages:
  1. role='user'      → con el intent + brief
  2. role='assistant' → con segments (paragraphs + tool calls) ordenados

Esto permite que el usuario reabra el documento más tarde y vea el
thread completo igual que en Claude.ai.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _thoughts_to_segments(thoughts: list[dict]) -> list[dict]:
    """Convierte agent_thoughts buffer en segments para AssistantNarrativeMessage.

    Reglas:
      - kind='tool_call' → segment type='tool' (merge por tool_id si running→done)
      - resto → segment type='paragraph' (markdown del message)
    """
    segments: list[dict] = []
    tool_by_id: dict[str, dict] = {}

    for t in thoughts:
        kind = t.get("kind", "info")
        ts = t.get("_ts", 0)

        if kind == "tool_call":
            tool_id = t.get("tool_id") or f"tool-{ts}-{len(segments)}"
            existing = tool_by_id.get(tool_id)
            is_error = t.get("tool_error") is not None
            is_done = t.get("tool_response") is not None and not is_error

            if existing:
                # Update segmento existente (running → done/error)
                tool_seg = existing
                tool_seg["tool"]["status"] = "error" if is_error else "done" if is_done else "running"
                if t.get("tool_request") is not None:
                    tool_seg["tool"]["request"] = t["tool_request"]
                if t.get("tool_response") is not None:
                    tool_seg["tool"]["response"] = t["tool_response"]
                if t.get("tool_error"):
                    tool_seg["tool"]["error"] = t["tool_error"]
                if t.get("tool_duration_ms") is not None:
                    tool_seg["tool"]["durationMs"] = t["tool_duration_ms"]
            else:
                tool_seg = {
                    "type": "tool",
                    "id": f"tool-{tool_id}",
                    "timestamp": ts,
                    "tool": {
                        "id": tool_id,
                        "name": t.get("tool") or "tool",
                        "status": "error" if is_error else "done" if is_done else "running",
                        "request": t.get("tool_request"),
                        "response": t.get("tool_response"),
                        "error": t.get("tool_error"),
                        "durationMs": t.get("tool_duration_ms"),
                        "startedAt": ts,
                    },
                }
                tool_by_id[tool_id] = tool_seg
                segments.append(tool_seg)
            continue

        # Paragraph segment
        msg = t.get("message", "")
        if not msg:
            continue
        segments.append({
            "type": "paragraph",
            "id": f"p-{ts}-{len(segments)}",
            "timestamp": ts,
            "markdown": _format_paragraph(t, msg),
        })

    return segments


def _format_paragraph(thought: dict, msg: str) -> str:
    """Aplica prefijos visuales por kind para legibilidad."""
    kind = thought.get("kind", "info")
    if kind in ("narration", "success"):
        return msg
    if kind == "correction":
        s = f"💡 **Sugerencia:** {msg}"
        if thought.get("suggestion"):
            s += f"\n\n→ Usar en su lugar: `{thought['suggestion']}`"
        return s
    if kind == "warning":
        return f"⚖ {msg}"
    if kind == "error":
        return f"⚠ {msg}"
    return msg


def _summarize_segments(segments: list[dict]) -> tuple[str, int]:
    """Extrae primer paragraph (preview) + count tools."""
    first_para = ""
    n_tools = 0
    for seg in segments:
        if seg["type"] == "tool":
            n_tools += 1
        elif seg["type"] == "paragraph" and not first_para:
            first_para = seg.get("markdown", "")[:500]
    return first_para or "(sin contenido)", n_tools


async def persist_chat_thread(
    pool,
    thread_id: str,
    generation_id: str,
    document_id: Optional[str],
    firm_id: Optional[str],
    matter_id: Optional[str],
    user_intent: str,
    user_brief: str,
    agent_thoughts: list[dict],
    duration_ms: int,
) -> bool:
    """Persiste 2 rows en chat_messages: user prompt + assistant thread.

    Idempotente: si el thread_id ya existe, hace UPDATE.
    """
    if pool is None:
        return False

    try:
        # 1. User message (siempre)
        user_content = user_intent.strip()
        if user_brief:
            user_content += f"\n\n--- Brief ---\n{user_brief.strip()}"

        # 2. Assistant message: convertir thoughts a segments
        segments = _thoughts_to_segments(agent_thoughts)
        preview, n_tools = _summarize_segments(segments)

        firm_uuid = uuid.UUID(firm_id) if firm_id else None
        doc_uuid = uuid.UUID(document_id) if document_id else None
        matter_uuid = uuid.UUID(matter_id) if matter_id else None

        async with pool.acquire() as conn:
            # User row
            await conn.execute(
                """
                INSERT INTO chat_messages (
                    thread_id, firm_id, generation_id, document_id, matter_id,
                    role, channel, content, segments,
                    duration_ms, total_tools_used
                )
                VALUES ($1, $2, $3, $4, $5, 'user', 'composer', $6, '[]'::jsonb, 0, 0)
                ON CONFLICT DO NOTHING
                """,
                f"{thread_id}:user",
                firm_uuid, generation_id, doc_uuid, matter_uuid,
                user_content,
            )

            # Assistant row
            await conn.execute(
                """
                INSERT INTO chat_messages (
                    thread_id, firm_id, generation_id, document_id, matter_id,
                    role, channel, content, segments,
                    duration_ms, total_tools_used
                )
                VALUES ($1, $2, $3, $4, $5, 'assistant', 'composer', $6, $7::jsonb, $8, $9)
                ON CONFLICT DO NOTHING
                """,
                thread_id,
                firm_uuid, generation_id, doc_uuid, matter_uuid,
                preview,
                json.dumps(segments, ensure_ascii=False, default=str),
                duration_ms,
                n_tools,
            )
        logger.info(
            "chat_messages persisted: thread=%s, segments=%d, tools=%d",
            thread_id, len(segments), n_tools,
        )
        return True
    except Exception as e:
        logger.warning("persist_chat_thread failed for thread=%s: %s", thread_id, e)
        return False


async def fetch_thread(pool, thread_id: str) -> Optional[dict]:
    """Lee un thread por id. Retorna {messages: [user, assistant]} o None."""
    if pool is None or not thread_id:
        return None
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text, thread_id, role, content, segments,
                       duration_ms, total_tools_used, created_at
                FROM chat_messages
                WHERE thread_id IN ($1, $2)
                ORDER BY
                  CASE WHEN role = 'user' THEN 0 ELSE 1 END,
                  created_at ASC
                """,
                thread_id,
                f"{thread_id}:user",
            )
        if not rows:
            return None
        msgs = []
        for r in rows:
            segs = r["segments"]
            if isinstance(segs, str):
                segs = json.loads(segs)
            msgs.append({
                "id": r["id"],
                "thread_id": r["thread_id"],
                "role": r["role"],
                "content": r["content"],
                "segments": segs or [],
                "duration_ms": r["duration_ms"],
                "total_tools_used": r["total_tools_used"],
                "created_at": str(r["created_at"]),
            })
        return {"thread_id": thread_id, "messages": msgs}
    except Exception as e:
        logger.warning("fetch_thread failed for %s: %s", thread_id, e)
        return None
