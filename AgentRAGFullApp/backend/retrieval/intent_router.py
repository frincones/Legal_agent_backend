"""Intent router: classifies user queries to determine the retrieval strategy.

Strategy:
1. Fast pre-check for obvious greetings (no LLM call)
2. Otherwise call LLM with strict rules and KNOWLEDGE as the default
"""

from __future__ import annotations

import logging
import re

from models.search import IntentType
from utils.llm import llm_generate

logger = logging.getLogger(__name__)

# Words that strongly indicate a casual greeting (no real question)
_GREETING_WORDS = {
    "hola", "holi", "holis", "hi", "hello", "hey", "buenos", "buenas", "saludos",
    "dias", "días", "tardes", "noches",
    "gracias", "thanks", "ok", "okay", "vale", "perfecto", "genial", "bien",
    "adios", "adiós", "bye", "chao", "chau", "hasta", "luego", "pronto",
    "como", "cómo", "que", "qué", "tal", "estas", "estás", "estais", "estáis",
    "va", "vas", "andas", "pasa", "cuentas", "sigues",
    "mucho", "muchas", "mil",
}

ROUTER_PROMPT = """You are a query classifier for a RAG (Retrieval-Augmented Generation) agent.

The agent has a knowledge base of documents (PDFs, Word, etc.) uploaded by the user.

Classify this user message into EXACTLY ONE category:

KNOWLEDGE - Default for ANY question that could potentially be answered by uploaded documents:
  • Questions about products, services, prices, quotes, invoices
  • Questions about policies, procedures, contracts, agreements
  • Questions like "what does X say", "how does X work", "tell me about X"
  • Questions asking for facts, recommendations, or information
  • ANY question containing specific entities (product names, codes, people, places)
  • When in doubt, USE THIS.

STRUCTURED - Only when explicitly asking for database metrics from these tables: {tables}

HYBRID - Both documents AND structured data are needed.

ACTION - Imperative request to DO something concrete (create, send, assign, generate, delete).
  Example: "send an email", "create a quote", "assign lead 5"

CONVERSATION - Pure social interactions with NO concrete information request:
  • Greetings: "Hola", "Hello", "Hi", "Buenos días", "Buenas tardes"
  • How-are-you variants (ALL are conversation when alone):
    - "Cómo estás", "Cómo está", "Cómo estan"
    - "Cómo vas", "Cómo te va", "Qué tal", "Qué tal todo", "Qué pasa"
    - "Cómo andas", "Qué cuentas", "How are you", "How's it going"
  • Thanks: "Gracias", "Muchas gracias", "Thanks"
  • Farewells: "Adiós", "Bye", "Hasta luego", "Nos vemos"
  • Acknowledgments: "Ok", "Vale", "Perfecto", "Entendido"
  • Combinations of the above: "Hola como vas", "Buenos dias que tal"
  IMPORTANT: ONLY promote to KNOWLEDGE when the message references a specific
  entity, product name, legal code, article number, person, place, or concrete
  topic. A simple "cómo está" without context IS conversation.

User message: "{message}"

Respond with ONLY the category name (one word, uppercase)."""


def _is_obvious_greeting(message: str) -> bool:
    """Fast pre-check for obvious greetings without an LLM call."""
    msg = message.lower().strip()
    if not msg:
        return True

    # Very short messages with only social words (raised cap to 5 tokens to
    # cover "hola como vas", "buenos dias como estas", "muchas gracias amigo")
    words = re.findall(r"\b\w+\b", msg)
    if len(words) <= 5 and words and all(w in _GREETING_WORDS for w in words):
        return True

    # Single greeting word + punctuation
    if len(words) == 1 and words[0] in _GREETING_WORDS:
        return True

    return False


async def classify_intent(
    message: str,
    model: str = "gpt-4o-mini",
    db_tables_schema: str | None = None,
) -> IntentType:
    """Classify user message intent. Defaults to KNOWLEDGE on any uncertainty."""

    # Fast path: obvious greetings skip the LLM call
    if _is_obvious_greeting(message):
        logger.debug("Intent (fast path): CONVERSATION for '%s'", message[:50])
        return IntentType.CONVERSATION

    try:
        prompt = ROUTER_PROMPT.format(
            message=message[:500],
            tables=db_tables_schema or "(no structured tables configured)",
        )

        response = await llm_generate(
            prompt=prompt,
            model=model,
            temperature=0.0,
            max_tokens=10,
            purpose="intent_router",
        )

        intent_str = response.strip().upper().split()[0] if response.strip() else "KNOWLEDGE"

        intent_map = {
            "KNOWLEDGE": IntentType.KNOWLEDGE,
            "STRUCTURED": IntentType.STRUCTURED,
            "HYBRID": IntentType.HYBRID,
            "ACTION": IntentType.ACTION,
            "CONVERSATION": IntentType.CONVERSATION,
        }

        intent = intent_map.get(intent_str, IntentType.KNOWLEDGE)
        logger.info("Intent: %s for query: '%s'", intent.value, message[:80])
        return intent

    except Exception as e:
        logger.warning("Intent classification failed: %s. Defaulting to KNOWLEDGE.", e)
        return IntentType.KNOWLEDGE
