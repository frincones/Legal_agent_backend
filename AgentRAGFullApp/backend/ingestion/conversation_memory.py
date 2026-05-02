"""Conversational memory: stores relevant conversation exchanges back into RAG."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from ingestion.embedder import EmbeddingGenerator
from storage.base import BaseStorage
from utils.llm import llm_generate

logger = logging.getLogger(__name__)

RELEVANCE_PROMPT = """Analyze this conversation exchange and determine if it contains information worth remembering for future queries. Consider:
- Does it contain factual answers about the business/domain?
- Does it reveal user preferences or decisions?
- Does it contain analysis, insights, or conclusions?
- Would this be useful context for similar future questions?

Conversation:
User: {user_message}
Assistant: {assistant_message}

Rate relevance for future memory on a scale of 1-5 (1=not worth saving, 5=highly valuable).
Respond with ONLY the number."""

SUMMARY_PROMPT = """Summarize this conversation exchange into a concise, self-contained knowledge entry that would be useful for future reference. Include the key question, the answer, and any important details or decisions.

User: {user_message}
Assistant: {assistant_message}

Write a concise summary (2-4 sentences) that captures the essential information."""


class ConversationMemory:
    """Manages storing and retrieving conversation-based knowledge."""

    def __init__(
        self,
        storage: BaseStorage,
        embedder: EmbeddingGenerator,
        utility_model: str = "gpt-4o-mini",
        relevance_threshold: int = 3,
        auto_save: bool = True,
    ):
        self.storage = storage
        self.embedder = embedder
        self.utility_model = utility_model
        self.relevance_threshold = relevance_threshold
        self.auto_save = auto_save

    async def process_exchange(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
        intent: Optional[str] = None,
        sources_used: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
    ) -> Optional[str]:
        """
        Evaluate a conversation exchange and store it if relevant.
        Returns conversation_id if stored, None if skipped.
        """
        if not self.auto_save:
            return None

        # Skip very short or trivial exchanges
        if len(assistant_message) < 50 or len(user_message) < 10:
            return None

        # Evaluate relevance
        relevance = await self._evaluate_relevance(user_message, assistant_message)

        if relevance < self.relevance_threshold:
            logger.debug("Conversation skipped (relevance=%d < threshold=%d)", relevance, self.relevance_threshold)
            return None

        # Generate summary for embedding
        summary = await self._generate_summary(user_message, assistant_message)

        # Embed the summary
        embedding = await self.embedder.generate_embedding(summary)

        # Store in database
        conv_id = await self.storage.save_conversation(
            session_id=session_id,
            user_message=user_message,
            assistant_message=assistant_message,
            embedding=embedding,
            intent=intent,
            sources_used=sources_used,
            relevance_score=float(relevance),
            metadata={
                "summary": summary,
                **(metadata or {}),
            },
        )

        logger.info("Stored conversation memory (relevance=%d, session=%s)", relevance, session_id)
        return conv_id

    async def search_past_conversations(
        self,
        query: str,
        limit: int = 3,
        session_id: Optional[str] = None,
    ) -> List[Dict]:
        """Search past conversations for relevant context."""
        embedding = await self.embedder.generate_embedding(query)

        results = await self.storage.search_conversations(
            query_embedding=embedding,
            limit=limit,
            session_id=session_id,
        )

        return [
            {
                "content": r.content,
                "similarity": r.similarity,
                "source": r.document_title,
                "metadata": r.metadata,
            }
            for r in results
        ]

    async def _evaluate_relevance(self, user_message: str, assistant_message: str) -> int:
        """Use LLM to evaluate if conversation is worth remembering."""
        try:
            prompt = RELEVANCE_PROMPT.format(
                user_message=user_message[:500],
                assistant_message=assistant_message[:1000],
            )
            response = await llm_generate(
                prompt=prompt,
                model=self.utility_model,
                temperature=0.0,
                max_tokens=5,
                purpose="conv_memory_relevance",
            )
            score = int(response.strip().split()[0])
            return max(1, min(5, score))
        except (ValueError, IndexError):
            return 3  # Default to borderline

    async def _generate_summary(self, user_message: str, assistant_message: str) -> str:
        """Generate a concise summary of the conversation for embedding."""
        try:
            prompt = SUMMARY_PROMPT.format(
                user_message=user_message[:500],
                assistant_message=assistant_message[:2000],
            )
            return await llm_generate(
                prompt=prompt,
                model=self.utility_model,
                temperature=0.0,
                max_tokens=200,
                purpose="conv_memory_summary",
            )
        except Exception as e:
            logger.warning("Summary generation failed: %s", e)
            # Fallback: use truncated original
            return f"Q: {user_message[:200]}\nA: {assistant_message[:300]}"
