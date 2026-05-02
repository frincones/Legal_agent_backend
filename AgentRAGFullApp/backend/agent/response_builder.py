"""Response builder: formats retrieval results and constructs agent context."""

from __future__ import annotations

import re
from typing import List, Optional

from models.search import RetrievalResult


# Norms that frequently appear as cross-references inside loaded chunks
# but are NOT themselves loaded in the corpus. These are sanitized out
# of the context before sending to the LLM to prevent hallucinated
# citations like "(fuente: CST, Artículo X del Decreto 1295 de 1994)".
#
# We use re.DOTALL/re.MULTILINE-friendly patterns and allow whitespace
# (including newlines) between tokens because PDF chunking often inserts
# arbitrary line breaks inside reference strings.
CROSS_REFERENCE_PATTERNS = [
    # "Decreto 1295 de 1994", "Decreto 1295\n del 22 de junio de 1994" — capture
    # the full reference with trailing date even when wrapped across lines.
    (r"(?is)\bdel\s+decreto\s+\d{1,5}\s*(?:del?\s+\d+\s+de\s+\w+\s+)?de\s+\d{4}\b", ""),
    (r"(?is)\bdecreto\s+\d{1,5}\s*(?:del?\s+\d+\s+de\s+\w+\s+)?de\s+\d{4}\b", "decreto reglamentario"),
    # "Decreto 1295" or "Decreto 1295 de 1994" without the inner date
    (r"(?is)\bdel\s+decreto\s+\d{1,5}\b", ""),
    (r"(?is)\bdecreto\s+\d{1,5}\b", "decreto reglamentario"),
    # "Decisión Andina 486", etc.
    (r"(?is)\bdecisi[óo]n\s+andina\s+\d+(?:\s+de\s+\d{4})?\b", "norma andina"),
    # Standalone references to other codes when used as primary source markers
    (r"(?is)\bdel\s+c[óo]digo\s+penal\b", ""),
    (r"(?is)\bdel\s+c[óo]digo\s+civil\b", ""),
    (r"(?is)\bdel\s+c[óo]digo\s+de\s+comercio\b", ""),
    (r"(?is)\bdel\s+c[óo]digo\s+general\s+del\s+proceso\b", ""),
    # "Constitución Política", "Constitución Política de Colombia"
    (r"(?is)\bconstituci[óo]n\s+pol[íi]tica\s+(?:de\s+colombia\s+)?(?:de\s+\d{4}\s*)?", ""),
    # Strip leftover "del/según" + "ley NNNN de YYYY" if not in our allow-list (general cleanup)
    # We can't easily filter "loaded" laws via regex; rely on prompt + above rules.
]


def _sanitize_chunk_content(content: str) -> str:
    """Remove references to non-loaded norms from a chunk to prevent the
    LLM from copying them into its citation parentheticals.

    Limitation: this is a defensive measure on top of the prompt-level rules.
    Some references in PDF-extracted text may slip through if their formatting
    is unusual.
    """
    if not content:
        return content
    cleaned = content
    for pattern, replacement in CROSS_REFERENCE_PATTERNS:
        cleaned = re.sub(pattern, replacement, cleaned)
    # Collapse double spaces/commas left behind by deletions
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r",\s*,", ",", cleaned)
    cleaned = re.sub(r"\(\s*,\s*", "(", cleaned)
    return cleaned


def build_context(retrieval_result: RetrievalResult) -> str:
    """Build formatted context string from retrieval results for LLM consumption.

    Sanitizes chunk content to remove cross-references to norms not in the
    loaded corpus, preventing the LLM from constructing citations like
    "(fuente: CST, Artículo del Decreto X de Y)" where Decreto X isn't loaded.
    """
    # Sanitize each result's content in-place (cheap mutation)
    for r in retrieval_result.results:
        r.content = _sanitize_chunk_content(r.content)
    return retrieval_result.format_context()


def get_sources(retrieval_result: RetrievalResult) -> List[str]:
    """Extract unique source document names from results."""
    sources = set()
    for r in retrieval_result.results:
        if r.document_title:
            sources.add(r.document_title)
        elif r.document_source:
            sources.add(r.document_source)
    return sorted(sources)


def get_confidence(retrieval_result: RetrievalResult) -> str:
    """Determine confidence level based on retrieval quality."""
    if not retrieval_result.has_results:
        return "No data found"

    if retrieval_result.reflection_score is not None:
        score = retrieval_result.reflection_score
        if score >= 4:
            return f"High ({score}/5)"
        elif score >= 3:
            return f"Medium ({score}/5)"
        else:
            return f"Low ({score}/5)"

    if retrieval_result.results:
        avg_sim = sum(r.best_score for r in retrieval_result.results) / len(retrieval_result.results)
        if avg_sim >= 0.85:
            return "High"
        elif avg_sim >= 0.7:
            return "Medium"
        else:
            return "Low"

    return "Unknown"


def sanitize_llm_response(response: str, allowed_documents: list | None = None) -> str:
    """Strip hallucinated norm references from the LLM output.

    Last-line defense: even with strict prompts, the LLM may construct citations
    like "(fuente: CST, Artículo 9° del Decreto 1295 de 1994)" by combining
    chunk text with training data. This post-processor removes those references
    inside `(fuente: ...)` blocks where they reference non-loaded norms.
    """
    if not response:
        return response

    # Inside any (fuente: ...) parenthetical, remove references to other norms
    def _clean_fuente(match: re.Match) -> str:
        block = match.group(0)
        # Strip "del Decreto NNNN de YYYY" or "del Decreto NNNN" etc.
        cleaned = re.sub(
            r"(?is),?\s*Art[íi]?culo\s+\d+[°º]?\s+del\s+Decreto\s+\d{1,5}(?:\s+de\s+\d{4})?",
            "",
            block,
        )
        cleaned = re.sub(
            r"(?is),?\s*del\s+Decreto\s+\d{1,5}(?:\s+de\s+\d{4})?",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?is),?\s*del\s+C[óo]digo\s+(?:Penal|Civil|de\s+Comercio|General\s+del\s+Proceso)",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?is),?\s*de\s+la\s+Constituci[óo]n\s+Pol[íi]tica(?:\s+de\s+Colombia)?",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?is),?\s*Decisi[óo]n\s+Andina\s+\d+(?:\s+de\s+\d{4})?",
            "",
            cleaned,
        )
        # Tidy up trailing punctuation/spaces inside the block
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = re.sub(r",\s*\)", ")", cleaned)
        return cleaned

    cleaned_response = re.sub(
        r"\(fuente:[^)]*\)",
        _clean_fuente,
        response,
        flags=re.IGNORECASE,
    )

    return cleaned_response


def format_response_with_sources(
    response: str,
    retrieval_result: RetrievalResult,
) -> str:
    """Append source citations to the agent's response."""
    sources = get_sources(retrieval_result)
    if not sources:
        return response

    source_list = "\n".join(f"- {s}" for s in sources)
    return f"{response}\n\n---\n**Sources:**\n{source_list}"


# ─────────────────────────────────────────────────────────────────────────────
# LEGAL: Live sources and vigencia formatting
# ─────────────────────────────────────────────────────────────────────────────

def format_live_source_results(live_results: list) -> str:
    """Format live source results as context block for the LLM.

    Args:
        live_results: List of LiveSourceResult from source_router.search()
    """
    if not live_results:
        return ""

    lines = ["## RESULTADOS DE FUENTES VIVAS\n"]
    lines.append("Los siguientes resultados provienen de fuentes oficiales en línea:\n")

    for i, r in enumerate(live_results[:10], 1):
        source_label = r.source.replace("_", " ").title() if hasattr(r, 'source') else "Fuente"
        titulo = getattr(r, 'titulo', '') or ''
        preview = getattr(r, 'preview', '') or ''
        url = getattr(r, 'url', '') or ''
        estado = getattr(r, 'estado', '') or ''

        line = f"### Resultado {i} [{source_label}]"
        if titulo:
            line += f"\n**{titulo}**"
        if estado:
            line += f" — Estado: {estado}"
        if preview:
            line += f"\n{preview}"
        if url:
            line += f"\nURL: {url}"
        lines.append(line)

    return "\n\n".join(lines)


def format_vigencia_results(vigencia_results: list) -> str:
    """Format vigencia verification results as context block for the LLM.

    Args:
        vigencia_results: List of VigenciaResult from vigencia_checker
    """
    if not vigencia_results:
        return ""

    lines = ["## VIGENCIA VERIFICADA\n"]
    lines.append("Estado de vigencia de las normas relevantes:\n")

    for result in vigencia_results:
        nombre = f"{result.tipo} {result.numero or ''} de {result.anio or ''}"

        if not result.encontrada:
            lines.append(f"⚠️ {nombre} — Vigencia no verificada (no indexada en el grafo)")
            continue

        if result.estado == "VIGENTE":
            lines.append(f"✅ {nombre} — VIGENTE")
        elif result.estado == "DEROGADA":
            derogada_info = ""
            if result.derogaciones:
                d = result.derogaciones[0]
                derogada_info = f" por {d.get('norma_tipo', '')} {d.get('norma_numero', '')} de {d.get('norma_anio', '')}"
                if d.get('fecha_efecto'):
                    derogada_info += f" (desde {d['fecha_efecto']})"
            lines.append(f"❌ {nombre} — DEROGADA{derogada_info}")

            # Show what replaced it
            if result.derogaciones:
                d = result.derogaciones[0]
                lines.append(f"   → Norma vigente: {d.get('norma_tipo', '')} {d.get('norma_numero', '')} de {d.get('norma_anio', '')}")
        elif result.estado == "MODIFICADA":
            lines.append(f"⚠️ {nombre} — MODIFICADA (verificar artículos específicos)")
            if result.derogaciones:
                for d in result.derogaciones[:3]:
                    arts = ", ".join(d.get("articulos_afectados", [])) or "varios"
                    lines.append(f"   → {d.get('tipo_derogacion', 'Modificada')} por {d.get('norma_tipo', '')} {d.get('norma_numero', '')} de {d.get('norma_anio', '')} (artículos: {arts})")
        else:
            lines.append(f"❓ {nombre} — Estado: {result.estado}")

    return "\n".join(lines)


def build_legal_context(
    retrieval_result: RetrievalResult,
    live_results: Optional[list] = None,
    vigencia_results: Optional[list] = None,
) -> str:
    """Build complete legal context combining RAG results, live sources, and vigencia.

    This is the enriched version of build_context() for legal mode.
    """
    # Base RAG context (sanitized)
    base_context = build_context(retrieval_result)

    # Append live source results
    if live_results:
        live_block = format_live_source_results(live_results)
        if live_block:
            base_context += "\n\n" + live_block

    # Append vigencia verification
    if vigencia_results:
        vigencia_block = format_vigencia_results(vigencia_results)
        if vigencia_block:
            base_context += "\n\n" + vigencia_block

    return base_context
