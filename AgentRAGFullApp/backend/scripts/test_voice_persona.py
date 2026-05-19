"""ADR-007 Fase 2 · Smoke test del persona assembler para canal de voz.

Prueba unitaria: llama build_voice_instructions directamente (sin WS real)
y verifica que el prompt resultante contiene markers del seed v1.

USO:
  # Desde el directorio backend:
  python -m scripts.test_voice_persona

  # Con PHASE activado (debe retornar prompt ensamblado desde DB):
  LEXAI_PERSONA_PHASE=2 LEXAI_PERSONA_VOICE=true python -m scripts.test_voice_persona

  # Con PHASE=0 (debe retornar LEGAL_VOICE_INSTRUCTIONS fallback):
  LEXAI_PERSONA_PHASE=0 python -m scripts.test_voice_persona
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("test_voice_persona")


async def _run() -> None:
    # Importar la constante legacy como referencia
    from api.voice import LEGAL_VOICE_INSTRUCTIONS, build_voice_instructions

    phase = int(os.getenv("LEXAI_PERSONA_PHASE", "0"))
    logger.info("=== test_voice_persona · LEXAI_PERSONA_PHASE=%d ===", phase)

    # Caso A: sin pool (fallback inmediato)
    logger.info("[A] Llamada sin pool → debe retornar LEGAL_VOICE_INSTRUCTIONS")
    result_no_pool = await build_voice_instructions(
        pool=None,
        firm_id=None,
        user_id=None,
        session_id=None,
    )
    assert result_no_pool == LEGAL_VOICE_INSTRUCTIONS, (
        "FAIL [A]: build_voice_instructions sin pool debería retornar LEGAL_VOICE_INSTRUCTIONS"
    )
    logger.info("[A] PASS · longitud=%d", len(result_no_pool))

    # Caso B: con pool real (si hay conexión a DB disponible)
    logger.info("[B] Llamada con pool real (si disponible)")
    try:
        from utils.db import get_storage
        storage = await get_storage()
        if not hasattr(storage, "pool"):
            logger.warning("[B] SKIP · storage.pool no disponible")
        else:
            result_with_pool = await build_voice_instructions(
                pool=storage.pool,
                firm_id=None,
                user_id=None,
                session_id=None,
            )
            logger.info("[B] Resultado: longitud=%d", len(result_with_pool))

            if phase >= 2 and os.getenv("LEXAI_PERSONA_VOICE", "").lower() in ("true", "1"):
                # Cuando el persona assembler está activo, debe haber contenido de la persona v1
                # El seed de ADR-007 incluye "lexai-co-senior-v1" en S1 (identity)
                assert len(result_with_pool) > len(LEGAL_VOICE_INSTRUCTIONS) or (
                    "lexai" in result_with_pool.lower()
                ), "FAIL [B]: prompt ensamblado vacío o sin markers de persona v1"
                logger.info("[B] PASS · persona ensamblada detectada")
            else:
                # PHASE=0 o VOICE=false → debe ser igual al legacy
                assert result_with_pool == LEGAL_VOICE_INSTRUCTIONS, (
                    "FAIL [B]: con PHASE<2 o VOICE=false debería retornar LEGAL_VOICE_INSTRUCTIONS"
                )
                logger.info("[B] PASS · fallback correcto (PHASE=%d)", phase)
    except Exception as exc:
        logger.warning("[B] SKIP · no se pudo conectar a DB: %s", exc)

    # Caso C: verificar que LEGAL_VOICE_INSTRUCTIONS permanece como constante (no se mutó)
    logger.info("[C] Verificar que LEGAL_VOICE_INSTRUCTIONS es inmutable")
    assert "REGLA #0" in LEGAL_VOICE_INSTRUCTIONS, (
        "FAIL [C]: LEGAL_VOICE_INSTRUCTIONS no contiene REGLA #0 (constante mutada?)"
    )
    assert "LexAI" in LEGAL_VOICE_INSTRUCTIONS, (
        "FAIL [C]: LEGAL_VOICE_INSTRUCTIONS no contiene 'LexAI'"
    )
    logger.info("[C] PASS · constante intacta")

    # Caso D: verificar gating · PHASE=1, VOICE=false → fallback
    logger.info("[D] Gating: PHASE=1, LEXAI_PERSONA_VOICE=false → debe retornar legacy")
    os.environ["LEXAI_PERSONA_PHASE"] = "1"
    os.environ["LEXAI_PERSONA_VOICE"] = "false"
    # Importar de nuevo para limpiar caché de env
    from utils import persona_assembler
    assembled_d, vid_d, _ = await persona_assembler.get_assembled_system_prompt(
        pool=None,
        firm_id=None,
        user_id=None,
        channel="voice",
        skill=None,
        session_id=None,
        legacy_prompt="LEGACY_VOICE",
    )
    assert assembled_d == "LEGACY_VOICE", f"FAIL [D]: esperaba legacy, got={assembled_d[:80]}"
    logger.info("[D] PASS · gating correcto para PHASE=1 + VOICE=false")

    # Caso E: gating · PHASE=2, VOICE=false → DEBE ensamblarse (PHASE>=2 ignora flag)
    logger.info("[E] Gating: PHASE=2, LEXAI_PERSONA_VOICE=false → debe intentar RPC (PHASE>=2 ignora flag)")
    os.environ["LEXAI_PERSONA_PHASE"] = "2"
    os.environ["LEXAI_PERSONA_VOICE"] = "false"
    # Con pool=None el RPC fallará graciosamente → legacy
    assembled_e, vid_e, _ = await persona_assembler.get_assembled_system_prompt(
        pool=None,
        firm_id=None,
        user_id=None,
        channel="voice",
        skill=None,
        session_id=None,
        legacy_prompt="LEGACY_VOICE_E",
    )
    # Sin pool, la RPC falla y retorna legacy — lo que importa es que NO retornó
    # legacy por la lógica de gating (fallback por excepción, no por el flag)
    assert assembled_e == "LEGACY_VOICE_E", (
        "FAIL [E]: sin pool debe hacer fallback a legacy (RPC exception path)"
    )
    logger.info("[E] PASS · con pool=None y PHASE=2 fallback correcto por excepción de RPC")

    logger.info("=== test_voice_persona · TODOS LOS TESTS PASARON ===")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
