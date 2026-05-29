"""Stage 1.4 · skill_loader.

Carga el SKILL.md del doc_type desde `firm_skills` (cascade firm → builtin)
y lo parsea en un `SkillContext` que los stages siguientes consumen.

Si NO encuentra SKILL.md, retorna None y el orchestrator usa su flujo
clásico (estructura inferida por LLM en structure_discovery).

NO depende de LLM externo: solo BD + parser puro Python → siempre rápido
(<50ms típico).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from lex.orchestrator.skill_context import SkillContext
from lex.skill_parser import (
    ParsedSkill,
    SkillFrontmatter,
    parse_skill_md,
)

logger = logging.getLogger(__name__)


# Process-local cache para evitar parsear el mismo SKILL.md múltiples veces
# en la misma generación (preview + pipeline) o entre generaciones cercanas.
# Clave: (firm_id_str_or_none, doc_type)
_CACHE: dict[tuple[Optional[str], str], tuple[float, SkillContext]] = {}
_CACHE_TTL_S = 300.0  # 5 minutos


def _cache_get(firm_id: Optional[str], doc_type: str) -> Optional[SkillContext]:
    key = (firm_id, doc_type.lower())
    entry = _CACHE.get(key)
    if not entry:
        return None
    ts, ctx = entry
    if (time.time() - ts) > _CACHE_TTL_S:
        _CACHE.pop(key, None)
        return None
    return ctx


def _cache_put(firm_id: Optional[str], doc_type: str, ctx: SkillContext) -> None:
    if len(_CACHE) > 500:
        try:
            oldest = next(iter(_CACHE))
            _CACHE.pop(oldest, None)
        except Exception:
            pass
    _CACHE[(firm_id, doc_type.lower())] = (time.time(), ctx)


async def _load_row_from_firm_skills(
    pool,
    *,
    doc_type: str,
    firm_id: Optional[str],
) -> Optional[dict]:
    """Busca firm_skills.row con frontmatter.doc_type == doc_type.

    Cascade: si firm_id != None, primero busca custom de la firma; si no
    encuentra, busca builtin (firm_id IS NULL).
    """
    if pool is None or not doc_type:
        return None
    sql_custom = """
        select id, command, name, description, category, jurisdiction,
               frontmatter, system_prompt, references_md, version, tier, status
          from firm_skills
         where firm_id = $1::uuid
           and status = 'published'
           and frontmatter->>'doc_type' = $2
         order by version desc
         limit 1
    """
    sql_builtin = """
        select id, command, name, description, category, jurisdiction,
               frontmatter, system_prompt, references_md, version, tier, status
          from firm_skills
         where firm_id is null
           and status = 'published'
           and frontmatter->>'doc_type' = $1
         order by version desc
         limit 1
    """
    try:
        async with pool.acquire() as conn:
            if firm_id:
                row = await conn.fetchrow(sql_custom, firm_id, doc_type)
                if row:
                    return dict(row)
            row = await conn.fetchrow(sql_builtin, doc_type)
            if row:
                return dict(row)
    except Exception as e:
        logger.debug("skill_loader BD lookup failed: %s", e)
    return None


def _frontmatter_dict_from_row(row: dict) -> dict:
    fm = row.get("frontmatter")
    if isinstance(fm, dict):
        return fm
    if isinstance(fm, str):
        try:
            return json.loads(fm)
        except Exception:
            return {}
    return {}


def _compose_skill_md(row: dict) -> str:
    """Reconstruye el texto MD a partir de los campos persistidos en firm_skills.

    El sistema H del sprint H NO almacenó el frontmatter como YAML al principio
    del system_prompt, así que tenemos que sintetizarlo a partir del jsonb
    `frontmatter`.
    """
    fm = _frontmatter_dict_from_row(row)
    yaml_lines = ["---"]
    for k in ("name", "description", "category", "doc_family", "doc_type",
              "default_scope", "language", "jurisdiction", "version", "tier"):
        v = fm.get(k)
        if v is None or v == "":
            continue
        if isinstance(v, str) and "\n" in v:
            yaml_lines.append(f"{k}: |")
            for ln in v.splitlines():
                yaml_lines.append(f"  {ln}")
        else:
            yaml_lines.append(f"{k}: {v}")
    sources = fm.get("sources") or []
    if isinstance(sources, list) and sources:
        yaml_lines.append("sources:")
        for s in sources:
            yaml_lines.append(f"  - {s}")
    yaml_lines.append("---")
    yaml_lines.append("")
    body_md = row.get("system_prompt") or ""
    refs = row.get("references_md") or ""
    composed = "\n".join(yaml_lines) + "\n" + body_md
    if refs:
        composed += "\n\n---\n\n## References\n\n" + refs
    return composed


async def load_skill_context(
    pool,
    *,
    doc_type: str,
    firm_id: Optional[str] = None,
    use_cache: bool = True,
) -> Optional[SkillContext]:
    """Carga SkillContext para un doc_type.

    Retorna None si no hay SKILL.md disponible para ese doc_type.
    Nunca tira excepción.
    """
    if not doc_type:
        return None

    started = time.time()
    if use_cache:
        cached = _cache_get(firm_id, doc_type)
        if cached is not None:
            logger.info(
                "skill_loader cache HIT doc_type=%s firm_id=%s",
                doc_type, firm_id,
            )
            return cached

    row = await _load_row_from_firm_skills(pool, doc_type=doc_type, firm_id=firm_id)
    if not row:
        logger.info(
            "skill_loader: no SKILL.md found for doc_type=%s firm_id=%s",
            doc_type, firm_id,
        )
        return None

    md_text = _compose_skill_md(row)
    try:
        parsed: ParsedSkill = parse_skill_md(md_text)
    except Exception as e:
        logger.warning("skill_loader parse failed for doc_type=%s: %s", doc_type, e)
        return None

    # Inyectar doc_type del query si frontmatter venía vacío
    if not parsed.frontmatter.doc_type:
        parsed.frontmatter.doc_type = doc_type

    ctx = SkillContext(
        doc_type=doc_type,
        skill_id=str(row["id"]) if row.get("id") else None,
        skill_command=row.get("command"),
        firm_id=str(row.get("firm_id")) if row.get("firm_id") else None,
        source="firm_skills",
        frontmatter=parsed.frontmatter,
        sections=parsed.body.document_structure,
        style_conventions_raw=parsed.body.style_conventions_raw,
        placeholders=parsed.body.placeholders,
        style_hints=parsed.body.style_hints,
        risk_warnings_raw=parsed.body.risk_warnings_raw,
        raw_system_prompt=row.get("system_prompt"),
        raw_references_md=row.get("references_md"),
        parse_warnings=list(parsed.parse_warnings),
    )

    if use_cache:
        _cache_put(firm_id, doc_type, ctx)

    elapsed = int((time.time() - started) * 1000)
    logger.info(
        "skill_loader OK doc_type=%s sections=%d placeholders=%d %dms",
        doc_type, len(ctx.sections), len(ctx.placeholders), elapsed,
    )
    return ctx


__all__ = ["load_skill_context"]
