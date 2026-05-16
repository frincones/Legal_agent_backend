"""Sprint E · Skill loader · resuelve y carga skill desde firm_skills table.

Patrón:
  1. Resolver skill por command (custom firm > builtin global)
  2. Parse YAML frontmatter
  3. Resolve includes/references
  4. Return SkillDefinition para skill_runner
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class SkillDefinition:
    id: str
    command: str
    name: str
    system_prompt: str
    output_schema: Optional[dict[str, Any]]
    references_md: Optional[str]
    frontmatter: dict[str, Any]
    is_custom: bool

    @property
    def category(self) -> str:
        return self.frontmatter.get("category", "other")

    @property
    def jurisdiction(self) -> str:
        return self.frontmatter.get("jurisdiction", "co")

    @property
    def argument_hint(self) -> Optional[str]:
        return self.frontmatter.get("argument-hint")

    @property
    def allowed_tools(self) -> list[str]:
        v = self.frontmatter.get("allowed-tools", [])
        return v if isinstance(v, list) else []

    @property
    def model(self) -> str:
        return self.frontmatter.get("model", "gpt-4o-mini")


async def resolve_skill(pool, firm_id: str, command: str) -> Optional[SkillDefinition]:
    """Llama RPC lexai_resolve_skill que prefiere custom firm sobre builtin."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select * from lexai_resolve_skill($1::uuid, $2)",
            firm_id, command,
        )
    if not row:
        return None
    fm = row["frontmatter"]
    if isinstance(fm, str):
        try:
            fm = json.loads(fm)
        except Exception:
            fm = {}
    os = row.get("output_schema")
    if isinstance(os, str):
        try:
            os = json.loads(os)
        except Exception:
            os = None
    return SkillDefinition(
        id=str(row["id"]),
        command=row["command"],
        name=row["name"],
        system_prompt=row["system_prompt"],
        output_schema=os,
        references_md=row.get("references_md"),
        frontmatter=fm or {},
        is_custom=row["is_custom"],
    )


async def list_active_skills(pool, firm_id: str) -> list[dict[str, Any]]:
    """Lista skills disponibles para la firma."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "select * from lexai_get_active_skills($1::uuid)",
            firm_id,
        )
    result = []
    for r in rows:
        fm = r["frontmatter"]
        if isinstance(fm, str):
            try:
                fm = json.loads(fm)
            except Exception:
                fm = {}
        result.append({
            "id": str(r["id"]),
            "command": r["command"],
            "name": r["name"],
            "description": r["description"],
            "category": r["category"],
            "jurisdiction": r["jurisdiction"],
            "user_invocable": r["user_invocable"],
            "frontmatter": fm,
            "is_custom": r["is_custom"],
        })
    return result
