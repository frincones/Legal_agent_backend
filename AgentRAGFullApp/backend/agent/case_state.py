"""
Case State Machine: maintains accumulated facts, conclusions, and context
across unlimited conversation turns. Survives the history_limit window.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from utils.llm import get_openai_client

logger = logging.getLogger(__name__)


class CaseState:
    """Accumulated legal case state across conversation turns.

    Supports multi-case sessions: when the case_shift detector decides the
    user moved on, the working state is archived into `archived_cases` and
    the working slots reset, while the underlying chat history (the
    `conversations` table) is preserved for cross-references.
    """

    def __init__(self, session_id: str, data: Optional[dict] = None):
        self.session_id = session_id
        d = data or {}
        # ── Working slots (current case) ────────────────────────────────
        self.facts: list[str] = d.get("facts", [])
        self.conclusions: list[str] = d.get("conclusions", [])
        self.norms_cited: list[str] = d.get("norms_cited", [])
        self.jurisprudence_cited: list[str] = d.get("jurisprudence_cited", [])
        self.areas_involved: list[str] = d.get("areas_involved", [])
        self.pending_questions: list[str] = d.get("pending_questions", [])
        self.turn_count: int = d.get("turn_count", 0)
        # ── Long-session hygiene ────────────────────────────────────────
        # `summary` is the LLM-produced compaction text; it stands in for
        # the older history slice that no longer travels in the messages.
        self.summary: str = d.get("summary", "")
        # Marks how far the compaction has consumed the chat history.
        # Empty = no compaction yet.
        self.last_compacted_message_id: str = d.get("last_compacted_message_id", "")
        # ── Multi-case ──────────────────────────────────────────────────
        self.case_index: int = d.get("case_index", 1)
        self.archived_cases: list[dict] = d.get("archived_cases", [])

    def archive_current_case(self) -> Optional[dict]:
        """Snapshot the working slots into `archived_cases` and reset.

        Returns the archived snapshot (or None if nothing to archive).
        Called when the case_shift detector confirms a topic change.
        Crucially does NOT touch the underlying chat history rows — the
        conversation transcript stays intact, only the structured state
        rolls over.
        """
        if not (self.facts or self.norms_cited or self.conclusions):
            return None
        snapshot = {
            "index": self.case_index,
            "facts": list(self.facts),
            "conclusions": list(self.conclusions),
            "norms_cited": list(set(self.norms_cited)),
            "jurisprudence_cited": list(set(self.jurisprudence_cited)),
            "areas_involved": list(set(self.areas_involved)),
            "pending_questions": list(self.pending_questions),
            "turn_count_at_archive": self.turn_count,
            "archived_at": datetime.now(timezone.utc).isoformat(),
        }
        self.archived_cases.append(snapshot)
        # Reset working slots — keep summary/last_compacted_message_id too,
        # since compaction also belonged to the previous case's narrative.
        self.facts = []
        self.conclusions = []
        self.norms_cited = []
        self.jurisprudence_cited = []
        self.areas_involved = []
        self.pending_questions = []
        self.summary = ""
        self.last_compacted_message_id = ""
        self.case_index += 1
        return snapshot

    def to_dict(self) -> dict:
        return {
            "facts": self.facts[-20:],  # Keep last 20 facts
            "conclusions": self.conclusions[-10:],
            "norms_cited": list(set(self.norms_cited))[-15:],
            "jurisprudence_cited": list(set(self.jurisprudence_cited))[-10:],
            "areas_involved": list(set(self.areas_involved)),
            "pending_questions": self.pending_questions[-5:],
            "turn_count": self.turn_count,
            # Long-session hygiene
            "summary": self.summary,
            "last_compacted_message_id": self.last_compacted_message_id,
            # Multi-case
            "case_index": self.case_index,
            "archived_cases": self.archived_cases[-10:],  # cap to last 10
        }

    def to_context_block(self) -> str:
        """Generate text block to inject into system prompt (~300-500 tokens).

        For multi-case sessions, only the CURRENT case is shown — archived
        cases stay in `archived_cases` for audit/UI purposes but do not
        contaminate the LLM context. The compacted summary, when present,
        prefixes the block so the LLM still has the narrative thread.
        """
        if self.turn_count == 0 and not self.summary:
            return ""

        parts = []
        # Multi-case header — only when we're past the first case
        if self.case_index > 1 or self.archived_cases:
            parts.append(f"CASO ACTUAL: #{self.case_index} (sesión con múltiples casos)")
        else:
            parts.append(f"ESTADO DEL CASO (turno {self.turn_count}):")

        # Compaction summary lives at the top so the LLM treats it as
        # backdrop before reading the structured slots.
        if self.summary:
            parts.append(f"\nResumen de turnos previos:\n{self.summary}")

        if self.facts:
            parts.append("\nHechos establecidos:")
            for f in self.facts[-10:]:
                parts.append(f"  - {f}")

        if self.conclusions:
            parts.append("\nConclusiones juridicas alcanzadas:")
            for c in self.conclusions[-5:]:
                parts.append(f"  - {c}")

        if self.norms_cited:
            norms = list(set(self.norms_cited))[-10:]
            parts.append(f"\nNormas analizadas: {', '.join(norms)}")

        if self.jurisprudence_cited:
            juris = list(set(self.jurisprudence_cited))[-5:]
            parts.append(f"Jurisprudencia citada: {', '.join(juris)}")

        if self.areas_involved:
            parts.append(f"Areas del derecho: {', '.join(set(self.areas_involved))}")

        if self.pending_questions:
            parts.append("\nPreguntas pendientes por resolver:")
            for q in self.pending_questions[-3:]:
                parts.append(f"  - {q}")

        return "\n".join(parts)

    async def update_from_exchange(
        self, user_message: str, assistant_message: str, model: str = "gpt-4o-mini"
    ):
        """Use LLM to extract facts, conclusions, and norms from the latest exchange."""
        self.turn_count += 1

        try:
            client = get_openai_client()
            current_state = json.dumps(self.to_dict(), ensure_ascii=False)

            response = await client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=500,
                messages=[
                    {"role": "system", "content": (
                        "Eres un asistente que extrae informacion estructurada de conversaciones legales. "
                        "Dado el estado actual del caso y el ultimo intercambio, extrae:\n"
                        "- new_facts: hechos nuevos mencionados por el usuario (max 3)\n"
                        "- new_conclusions: conclusiones juridicas del asistente (max 2)\n"
                        "- new_norms: normas citadas como 'Art. 62 CST' (max 5)\n"
                        "- new_jurisprudence: sentencias citadas como 'Sentencia T-388/2019' (max 3)\n"
                        "- areas: areas del derecho tocadas como 'laboral' (max 3)\n"
                        "- pending: preguntas que el asistente hizo al usuario (max 2)\n"
                        "Responde SOLO en JSON valido. Si no hay datos nuevos, usa listas vacias."
                    )},
                    {"role": "user", "content": (
                        f"Estado actual: {current_state}\n\n"
                        f"Usuario dijo: {user_message[:500]}\n\n"
                        f"Asistente respondio: {assistant_message[:800]}"
                    )},
                ],
            )

            text = response.choices[0].message.content.strip()
            # Clean markdown code fences if present
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            text = text.strip()

            data = json.loads(text)

            # Merge new data
            self.facts.extend(data.get("new_facts", []))
            self.conclusions.extend(data.get("new_conclusions", []))
            self.norms_cited.extend(data.get("new_norms", []))
            self.jurisprudence_cited.extend(data.get("new_jurisprudence", []))
            for area in data.get("areas", []):
                if area and area not in self.areas_involved:
                    self.areas_involved.append(area)
            self.pending_questions = data.get("pending", self.pending_questions)

            logger.info(
                f"CaseState updated: turn={self.turn_count}, facts={len(self.facts)}, "
                f"conclusions={len(self.conclusions)}, norms={len(self.norms_cited)}"
            )

        except Exception as e:
            logger.warning(f"CaseState update failed (non-fatal): {e}")
            # Still increment turn count even if extraction fails
