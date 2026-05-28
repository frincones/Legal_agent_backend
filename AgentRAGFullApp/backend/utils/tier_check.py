"""Sprint M19.26.B · tier gate para skills marketplace.

Reglas:
  - tier='public': cualquier firma activa puede ejecutar
  - tier='premium': requiere firms.plan IN ('estudio_pro','enterprise')

Devolvemos un dataclass con la decisión + razón para que el caller decida si
emite 402 Payment Required, log, o silencia.

Si la firma no existe en BD (multi-tenancy con firms.plan absent) → asumimos
'public' acceso libre (default seguro para builtin).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


PREMIUM_PLANS = {"estudio_pro", "enterprise"}


@dataclass
class TierDecision:
    allowed: bool
    reason: str
    tier: str          # 'public' | 'premium' | 'unknown'
    firm_plan: Optional[str] = None
    skill_command: Optional[str] = None


async def check_skill_tier_allowed(
    pool,
    *,
    firm_id: str,
    skill_id: Optional[str] = None,
    skill_command: Optional[str] = None,
) -> TierDecision:
    """Verifica si la firma puede ejecutar este skill según su tier.

    Una de skill_id o skill_command debe darse. skill_id es preferido (más rápido).
    """
    if not skill_id and not skill_command:
        return TierDecision(allowed=True, reason="no_skill_specified", tier="unknown")

    try:
        async with pool.acquire() as conn:
            # 1. Leer tier del skill
            if skill_id:
                tier_row = await conn.fetchrow(
                    "select tier, command from firm_skills where id = $1::uuid",
                    skill_id,
                )
            else:
                tier_row = await conn.fetchrow(
                    """
                    select tier, command from firm_skills
                     where command = $1 and status = 'published'
                     order by (firm_id is null) asc  -- prefer custom (non-null) first
                     limit 1
                    """,
                    skill_command,
                )

            if not tier_row:
                # Skill no encontrado o sin columna tier (migración aún no corrida)
                return TierDecision(allowed=True, reason="skill_not_found_assume_public", tier="unknown")

            tier = (tier_row["tier"] or "public").strip().lower()
            cmd = tier_row["command"]

            if tier == "public":
                return TierDecision(allowed=True, reason="tier_public", tier="public", skill_command=cmd)

            if tier == "premium":
                plan_row = await conn.fetchrow(
                    "select plan from firms where id = $1::uuid",
                    firm_id,
                )
                plan = (plan_row["plan"] if plan_row else None) or "unknown"
                if plan in PREMIUM_PLANS:
                    return TierDecision(allowed=True, reason="tier_premium_plan_ok", tier="premium", firm_plan=plan, skill_command=cmd)
                return TierDecision(
                    allowed=False,
                    reason=f"tier_premium_requires_plan_estudio_pro_or_enterprise (have: {plan})",
                    tier="premium",
                    firm_plan=plan,
                    skill_command=cmd,
                )

            # tier desconocido (futuro: 'beta', etc.) → permitir por defecto
            logger.warning("Unknown tier %r on skill %s — allowing", tier, cmd)
            return TierDecision(allowed=True, reason=f"unknown_tier_{tier}_allowed", tier=tier, skill_command=cmd)

    except Exception as e:
        # Si la columna tier no existe aún (migración pendiente) o cualquier error
        # de BD: failsafe = permitir (no romper en runtime por bug del gate).
        msg = str(e)
        if "column" in msg.lower() and "tier" in msg.lower():
            logger.info("firm_skills.tier column missing — running migration soon. Allowing skill exec.")
            return TierDecision(allowed=True, reason="tier_column_missing_allowed", tier="unknown")
        logger.warning("tier_check failed (%s) — defaulting to allowed", e)
        return TierDecision(allowed=True, reason=f"tier_check_error_{type(e).__name__}_allowed", tier="unknown")
