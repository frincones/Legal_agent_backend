"""Sprint P0/ADR-007 - Persona assembler · gating de feature flags por canal y skill.

Expone una sola función pública:
  get_assembled_system_prompt(pool, firm_id, user_id, channel, skill, session_id, legacy_prompt)
  -> tuple[str, str | None, str | None]

La función implementa la lógica de evaluación de §4.2 del ADR-007.
Cuando se activa, llama la RPC lexai_assemble_system_prompt en Supabase/Postgres
y retorna (system_prompt, version_id, checksum).
Si la RPC falla (o los flags dicen que no aplica), retorna (legacy_prompt, None, None).

Variables de entorno controladas:
  LEXAI_PERSONA_PHASE     int  0..5  (default 0)
  LEXAI_PERSONA_CHAT_ASK  bool       (default false)
  LEXAI_PERSONA_CHAT_LEX  bool       (default false)
  LEXAI_PERSONA_VOICE     bool       (default false)
  LEXAI_PERSONA_SUBAGENT  bool       (default false)
"""

from __future__ import annotations

import logging
import os
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers para leer env vars tipadas
# ---------------------------------------------------------------------------

def _get_phase() -> int:
    """Lee LEXAI_PERSONA_PHASE como int. Default 0."""
    try:
        return int(os.getenv("LEXAI_PERSONA_PHASE", "0"))
    except ValueError:
        return 0


def _get_flag(name: str) -> bool:
    """Lee una variable de entorno bool. Acepta 'true'/'1'/'yes' (case-insensitive)."""
    val = os.getenv(name, "").strip().lower()
    return val in ("true", "1", "yes")


def _firm_uuid(v: Optional[str]) -> Optional[UUID]:
    """Convierte str a UUID. Retorna None si v es None, vacío o no parseable."""
    if not v:
        return None
    try:
        return UUID(str(v))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

async def get_assembled_system_prompt(
    pool,
    firm_id: Optional[str],
    user_id: Optional[str],
    channel: str,              # 'chat' | 'voice'
    skill: Optional[str],      # '/ask' | '/lex' | '/redactar/*' | 'subagent' | None
    session_id: Optional[str],
    legacy_prompt: str,
) -> tuple[str, Optional[str], Optional[str]]:
    """Retorna (system_prompt, version_id, checksum).

    Si los flags dicen que no aplica la RPC, retorna (legacy_prompt, None, None).
    Si la RPC falla por cualquier causa, hace fallback a (legacy_prompt, None, None)
    y loguea warning — nunca propaga excepción al caller.
    """
    phase = _get_phase()

    # Fase 0 → siempre legacy, sin consultar RPC.
    if phase == 0:
        return (legacy_prompt, None, None)

    # Evaluar si este canal/skill debe usar la RPC según §4.2.
    should_use_rpc = _should_use_rpc(phase, channel, skill)
    if not should_use_rpc:
        return (legacy_prompt, None, None)

    # Intentar llamar la RPC.
    try:
        return await _call_rpc(pool, firm_id, user_id, channel, skill, session_id, legacy_prompt)
    except Exception as exc:
        logger.warning(
            "persona_assembler: RPC failed · fallback to legacy. "
            "channel=%s skill=%s firm_id=%s error=%s",
            channel, skill, firm_id, exc,
        )
        return (legacy_prompt, None, None)


def _should_use_rpc(phase: int, channel: str, skill: Optional[str]) -> bool:
    """Evalúa la lógica de gating de §4.2 del ADR-007.

    Si PHASE >= 3 todos los skills de chat usan RPC (ignorar flags individuales).
    """
    # PHASE >= 3 → todos los canales/skills.
    if phase >= 3:
        return True

    # PHASE >= 2 → voz se activa sin importar LEXAI_PERSONA_VOICE.
    if channel == "voice":
        if phase >= 2:
            return True
        return _get_flag("LEXAI_PERSONA_VOICE")

    # Sub-agentes.
    if skill == "subagent":
        return _get_flag("LEXAI_PERSONA_SUBAGENT")

    # Canal chat · gating por skill.
    if channel == "chat":
        if skill == "/ask":
            return _get_flag("LEXAI_PERSONA_CHAT_ASK")
        if skill in ("/lex",) or (skill and (
            skill.startswith("/redactar/") or skill.startswith("/revisar/")
            or skill in ("/redactar/*", "/revisar/*")
        )):
            return _get_flag("LEXAI_PERSONA_CHAT_LEX")
        # Otros skills de chat: solo se activan cuando PHASE >= 3 (ya cubierto arriba).
        return False

    return False


async def _call_rpc(
    pool,
    firm_id: Optional[str],
    user_id: Optional[str],
    channel: str,
    skill: Optional[str],
    session_id: Optional[str],
    legacy_prompt: str,
) -> tuple[str, Optional[str], Optional[str]]:
    """Llama lexai_assemble_system_prompt via asyncpg y retorna (prompt, version_id, checksum)."""
    if pool is None or not hasattr(pool, "acquire"):
        raise RuntimeError("pool no disponible")

    firm_uuid = _firm_uuid(firm_id)
    user_uuid = _firm_uuid(user_id)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT system_prompt, version_id, checksum
            FROM lexai_assemble_system_prompt($1::uuid, $2::uuid, $3, $4, $5)
            """,
            firm_uuid,
            user_uuid,
            channel,
            skill,
            session_id,
        )

    if not row:
        logger.warning(
            "persona_assembler: RPC retornó sin filas · fallback. "
            "firm_id=%s channel=%s skill=%s",
            firm_id, channel, skill,
        )
        return (legacy_prompt, None, None)

    assembled_prompt = row["system_prompt"]
    version_id = str(row["version_id"]) if row["version_id"] else None
    checksum = row["checksum"] if row["checksum"] else None

    if not assembled_prompt:
        logger.warning(
            "persona_assembler: RPC retornó system_prompt vacío · fallback. "
            "firm_id=%s channel=%s skill=%s version_id=%s",
            firm_id, channel, skill, version_id,
        )
        return (legacy_prompt, None, None)

    logger.info(
        "persona_assembler: RPC OK · channel=%s skill=%s "
        "version_id=%s checksum=%s prompt_len=%d",
        channel, skill, version_id, (checksum or "")[:16], len(assembled_prompt),
    )
    return (assembled_prompt, version_id, checksum)
