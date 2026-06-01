"""Sprint M21.S4.A · Agent: learn_from_seed_docs.

Toma docs pendientes de firm_seed_documents y extrae estructura via LLM
para crear un nuevo registro en firm_skills con tier='learned' (custom
para esa firma). Marca el seed doc como processed.

Trigger: event (cuando se sube un seed doc, el endpoint upload puede
disparar este agent) o manual via /v2/agents/{name}/run.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import uuid4

from .base import AgentContext, AgentRunResult, BackgroundAgent

logger = logging.getLogger(__name__)


EXTRACT_PROMPT = """Eres un experto en derecho colombiano. Analiza el documento legal adjunto y extrae su estructura para crear un SKILL reutilizable.

Devuelve JSON con:
{
  "doc_type": "string (poder_especial, contrato_arrendamiento, demanda_laboral, etc.)",
  "doc_family": "notarial|judicial|contractual|administrativo|penal",
  "name": "string (nombre legible del tipo de documento)",
  "sections": [{"key": "encabezado", "label": "Encabezado", "required": true}, ...],
  "placeholders": [{"key": "otorgante_nombre", "label": "Nombre del otorgante", "required": true}, ...],
  "required_citations": ["Art. 2158 CC", "Decreto 960 de 1970"],
  "blacklist_citations": [],
  "summary": "Resumen 1 parrafo de para que sirve este tipo de documento."
}

Solo responde JSON valido. Sin texto explicativo."""


class LearnFromSeedDocsAgent(BackgroundAgent):
    name = "learn_from_seed_docs"
    description = "Analiza seed_docs pendientes y crea SKILLs learned custom para la firma."
    trigger_kind = "event"
    default_event = "seed_doc_uploaded"
    timeout_seconds = 180.0

    async def run(self, ctx: AgentContext) -> AgentRunResult:
        if ctx.pool is None:
            return AgentRunResult(status="skipped", output_summary="no_pool")

        firm_id = str(ctx.firm_id)
        processed = 0
        succeeded = 0
        failed = 0
        learned_skills: list[dict] = []

        async with ctx.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select seed_doc_id, title, area, doc_type, content_bytes, content_mime, notes_md
                  from firm_seed_documents
                 where firm_id = $1 and status = 'pending'
                 order by created_at asc
                 limit 10
                """,
                firm_id,
            )

        if not rows:
            return AgentRunResult(status="ok", output_summary="no_pending_seed_docs")

        for row in rows:
            processed += 1
            seed_id = str(row["seed_doc_id"])
            try:
                extracted = await self._extract_with_llm(ctx, row)
                if not extracted:
                    raise RuntimeError("LLM extraction returned empty")

                async with ctx.pool.acquire() as conn:
                    async with conn.transaction():
                        # Insert into firm_skills as tier='learned'
                        skill_id = await self._upsert_learned_skill(conn, firm_id, extracted, row)
                        await conn.execute(
                            """
                            update firm_seed_documents
                               set status = 'processed',
                                   processed_at = now(),
                                   extracted_positions = $2::jsonb
                             where seed_doc_id = $1::uuid
                            """,
                            seed_id,
                            json.dumps({"learned_skill_id": str(skill_id), "doc_type": extracted.get("doc_type")}),
                        )
                succeeded += 1
                learned_skills.append({"seed_doc_id": seed_id, "skill_id": str(skill_id), "doc_type": extracted.get("doc_type")})
                logger.info("learn_from_seed_docs: doc=%s -> skill=%s doc_type=%s", seed_id, skill_id, extracted.get("doc_type"))
            except Exception as e:
                failed += 1
                logger.warning("learn_from_seed_docs: doc=%s failed: %s", seed_id, e)
                try:
                    async with ctx.pool.acquire() as conn:
                        await conn.execute(
                            "update firm_seed_documents set status='failed' where seed_doc_id = $1::uuid",
                            seed_id,
                        )
                except Exception:
                    pass

        summary = f"Procesados {processed} seed_docs, {succeeded} learned skills creados, {failed} fallidos"
        return AgentRunResult(
            status="ok" if failed == 0 else ("error" if succeeded == 0 else "ok"),
            items_processed=processed,
            items_succeeded=succeeded,
            items_failed=failed,
            output_summary=summary,
            metadata={"learned_skills": learned_skills},
        )

    async def _extract_with_llm(self, ctx: AgentContext, row) -> Optional[dict]:
        """Llama a Anthropic con el doc y pide JSON extraido."""
        if ctx.anthropic_client is None:
            logger.warning("learn_from_seed_docs: anthropic_client no disponible, usando heuristica")
            return self._heuristic_extract(row)

        # Para MVP solo trabajamos con texto plano (txt/md). Pdf requiere parser separado (Sprint 4.1).
        mime = row["content_mime"] or "text/plain"
        if not mime.startswith("text/"):
            logger.info("learn_from_seed_docs: %s mime=%s no soportado todavia, heuristica", row["seed_doc_id"], mime)
            return self._heuristic_extract(row)

        try:
            text = row["content_bytes"].decode("utf-8", errors="ignore")[:30000]
        except Exception:
            return self._heuristic_extract(row)

        try:
            resp = await ctx.anthropic_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                temperature=0.1,
                system=EXTRACT_PROMPT,
                messages=[{"role": "user", "content": f"Documento titulo: {row['title']}\nArea: {row['area']}\n\n---\n{text}"}],
            )
            raw = "".join(b.text for b in resp.content if hasattr(b, "text")).strip()
            if raw.startswith("```"):
                raw = raw.strip("`").lstrip("json").strip()
            return json.loads(raw)
        except Exception as e:
            logger.warning("LLM extract failed: %s, fallback heuristica", e)
            return self._heuristic_extract(row)

    @staticmethod
    def _heuristic_extract(row) -> dict:
        """Fallback sin LLM: estructura basica desde campos del seed_doc."""
        return {
            "doc_type": row["doc_type"] or f"custom_{row['seed_doc_id'][:8]}",
            "doc_family": "custom",
            "name": row["title"],
            "sections": [
                {"key": "encabezado", "label": "Encabezado", "required": True},
                {"key": "cuerpo", "label": "Cuerpo", "required": True},
                {"key": "cierre", "label": "Cierre", "required": True},
            ],
            "placeholders": [],
            "required_citations": [],
            "blacklist_citations": [],
            "summary": f"Skill aprendido del seed doc '{row['title']}' (extraccion heuristica)",
        }

    async def _upsert_learned_skill(self, conn, firm_id: str, extracted: dict, row) -> str:
        """Inserta o update firm_skills con tier='learned'."""
        doc_type = extracted.get("doc_type") or "custom"
        name = extracted.get("name") or row["title"]
        frontmatter = {
            "doc_type": doc_type,
            "name": name,
            "jurisdiction": "CO",
            "doc_family": extracted.get("doc_family") or "custom",
            "tier": "learned",
            "source_seed_doc_id": str(row["seed_doc_id"]),
        }
        system_prompt = (
            f"# {name}\n\n{extracted.get('summary','')}\n\n"
            f"## Sections\n" + "\n".join(f"- {s.get('key')}: {s.get('label','')}" for s in extracted.get("sections", []))
        )
        references_md = ""
        cits = extracted.get("required_citations") or []
        if cits:
            references_md = "## Citas obligatorias\n" + "\n".join(f"- {c}" for c in cits)

        skill_id = await conn.fetchval(
            """
            insert into firm_skills
                (firm_id, command, name, description, category, jurisdiction,
                 frontmatter, system_prompt, references_md, version, tier, status)
            values
                ($1, $2, $3, $4, 'learned', 'CO', $5::jsonb, $6, $7, 1, 'learned', 'published')
            on conflict on constraint firm_skills_firm_id_command_key do update set
                name = excluded.name,
                description = excluded.description,
                frontmatter = excluded.frontmatter,
                system_prompt = excluded.system_prompt,
                references_md = excluded.references_md,
                version = firm_skills.version + 1,
                updated_at = now()
            returning id
            """,
            firm_id, f"/{doc_type}_learned", name,
            extracted.get("summary") or f"SKILL aprendido de seed doc {row['title']}",
            json.dumps(frontmatter, ensure_ascii=False),
            system_prompt, references_md,
        )
        return str(skill_id)


def build_agent(**_: Any) -> BackgroundAgent:
    return LearnFromSeedDocsAgent()
