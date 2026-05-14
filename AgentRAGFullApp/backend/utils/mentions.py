"""Sprint 16 · Mention parsing utilities.

Encuentra patrones `@username`, `@nombre.apellido`, `@"Pedro Rojas"` en un
texto y los resuelve a `user_id` consultando la tabla `users` filtrada por
firm_id (RLS no aplica porque corremos desde la conexión service).

Estrategia:
  1. Regex captura tres formas:
       @"frase con espacios"     → match exact en full_name (case-insensitive)
       @firstname.lastname       → match exact (case-insensitive) en full_name
       @handle                   → match contra primera palabra de full_name
                                    o email username (lowercased)
  2. Cada token único se busca en `users` filtrado por firm_id.
  3. Devolvemos `list[user_id]` deduplicado · ignoramos tokens sin match.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

logger = logging.getLogger(__name__)


# El orden de los grupos importa: el primero que matchee gana.
# Grupo 1: nombre entre comillas dobles · permite espacios y acentos
# Grupo 2: handle simple · letras + dígitos + . _ -
MENTION_REGEX = re.compile(
    r"(?:^|\s)@(?:\"([^\"]{1,80})\"|([A-Za-zÀ-ÿ][\w.\-]{1,40}))",
    re.UNICODE,
)


def extract_tokens(body: str) -> list[str]:
    """Devuelve la lista de tokens @ encontrados en el texto (sin el @, sin comillas)."""
    if not body:
        return []
    out: list[str] = []
    for m in MENTION_REGEX.finditer(body):
        token = m.group(1) or m.group(2)
        if token:
            out.append(token.strip())
    # Dedup preservando orden
    seen: set[str] = set()
    deduped: list[str] = []
    for t in out:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            deduped.append(t)
    return deduped


async def resolve_mentions(conn, firm_id: str, body: str) -> list[str]:
    """Parsea `body` y devuelve los user_id de las menciones que existen en la firm.

    `conn` es una conexión asyncpg ya adquirida del pool.
    """
    tokens = extract_tokens(body)
    if not tokens:
        return []

    # Construimos los candidatos en lower-case para comparar contra:
    #   - full_name completo
    #   - primera palabra de full_name
    #   - email (parte antes del @)
    # Igualamos con LOWER y comparación literal o ILIKE para forma "first.last".
    candidates_full = [t.lower() for t in tokens]
    candidates_dot = [t.replace(".", " ").lower() for t in tokens]

    try:
        rows = await conn.fetch(
            """
            select id, full_name, email
              from users
             where firm_id = $1::uuid
               and (
                 lower(full_name) = any($2::text[])
                 or lower(full_name) = any($3::text[])
                 or lower(split_part(coalesce(full_name,''),' ',1)) = any($2::text[])
                 or lower(split_part(coalesce(email,''),'@',1)) = any($2::text[])
               )
            """,
            firm_id, candidates_full, candidates_dot,
        )
    except Exception as e:
        logger.warning("resolve_mentions query failed: %s", e)
        return []

    user_ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        sid = str(r["id"])
        if sid not in seen:
            seen.add(sid)
            user_ids.append(sid)
    return user_ids


def render_preview(body: str, max_chars: int = 280) -> str:
    """Snippet seguro · trunca a max_chars + … sin cortar a media palabra (best effort)."""
    if not body:
        return ""
    body = body.strip()
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars]
    sp = cut.rfind(" ")
    if sp > max_chars * 0.6:
        cut = cut[:sp]
    return cut + "…"
