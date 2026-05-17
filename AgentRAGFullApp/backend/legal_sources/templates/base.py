"""Shared base for template scrapers.

A TemplateSource pulls documents from an external source (SECOP, Rama
Judicial, MinTrabajo, etc.), normalizes them to markdown, and yields
TemplateCandidate dicts ready to insert into `template_candidates`.

The base is deliberately thin: each subclass implements `fetch()` as an
async generator and reuses helpers for HTTP, dedup hashing, and
normalization.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)


@dataclass
class TemplateCandidate:
    """Row shape inserted into `template_candidates` table.

    Mirrors the columns declared in 2026_05_24_templates_specialization.sql.
    Enrichment (suggested_materia, suggested_doc_type, llm_report_jsonb,
    quality_score) is added later by the batch_enrichment pipeline, not here.
    """
    source: str
    source_ref: str            # stable id (URL hash or source's own id)
    source_url: Optional[str]
    raw_text: str              # raw HTML / PDF text before normalization
    normalized_md: str         # cleaned markdown
    suggested_materia: Optional[str] = None     # the scraper's best guess
    suggested_doc_type: Optional[str] = None
    suggested_subtype: Optional[str] = None
    suggested_norms: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def content_hash(self) -> str:
        """Stable hash of the normalized content for dedup."""
        return hashlib.sha256(self.normalized_md.encode("utf-8")).hexdigest()[:16]


@dataclass
class FetchResult:
    """Aggregate stats returned by TemplateSource.run()."""
    source: str
    items_found: int = 0
    items_emitted: int = 0
    items_skipped: int = 0
    errors: list[str] = field(default_factory=list)


class TemplateSourceBase(ABC):
    """Base class · subclass per data source."""

    name: str = "base"
    description: str = ""
    base_url: str = ""

    # Rate limit · all scrapers should respect robots.txt and 1 req/sec default.
    request_delay_seconds: float = 1.0
    # Polite User-Agent (identifies LexAI, suggests contact).
    user_agent: str = (
        "LexAI-Template-Ingestor/1.0 "
        "(+https://lexai.co · contacto: ingest@lexai.co)"
    )

    @abstractmethod
    async def fetch(self, *, limit: int = 100) -> AsyncIterator[TemplateCandidate]:
        """Yield TemplateCandidate one at a time. Subclasses implement.

        Implementations should:
          - honor `limit` (stop after that many emitted)
          - sleep `request_delay_seconds` between HTTP calls
          - catch+log per-item errors and continue (don't raise)
        """
        if False:                # pragma: no cover · type-checker hint
            yield  # type: ignore[unreachable]
        raise NotImplementedError

    async def run(
        self,
        *,
        limit: int = 100,
        on_item: Optional[callable] = None,
    ) -> FetchResult:
        """Convenience wrapper: drain the iterator and tally results.

        on_item: optional async callback invoked with each TemplateCandidate ·
                 typically writes to template_candidates table.
        """
        result = FetchResult(source=self.name)
        try:
            async for cand in self.fetch(limit=limit):
                result.items_found += 1
                try:
                    if on_item:
                        await on_item(cand)
                    result.items_emitted += 1
                except Exception as e:
                    logger.warning(
                        "%s · on_item failed for ref=%s: %s",
                        self.name, cand.source_ref, e,
                    )
                    result.errors.append(f"{cand.source_ref}: {e}")
        except Exception as e:
            logger.exception("%s · run failed: %s", self.name, e)
            result.errors.append(f"run: {e}")
        return result

    # ──────────────────────────────────────────────────────────────
    # Shared helpers used by every scraper.
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def normalize_whitespace(text: str) -> str:
        """Collapse weird whitespace · strip · keep paragraph breaks."""
        # Normalize newlines, collapse runs of spaces, but keep blank lines.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def html_to_markdown(html: str) -> str:
        """Lightweight HTML → markdown converter.

        Uses BeautifulSoup if installed (graceful fallback otherwise).
        We DON'T pull markdownify as a hard dep just for scrapers; this is
        good enough for ingestion (LLM enrichment cleans up the rest).
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            # No BS4 · strip tags naively.
            return re.sub(r"<[^>]+>", " ", html)

        soup = BeautifulSoup(html, "html.parser")
        # Drop script/style noise.
        for bad in soup(["script", "style", "noscript", "iframe"]):
            bad.decompose()

        # Convert common structural tags to markdown.
        for h_level in range(1, 7):
            for h in soup.find_all(f"h{h_level}"):
                h.insert_before("\n\n" + "#" * h_level + " ")
                h.insert_after("\n\n")
                h.unwrap()
        for p in soup.find_all("p"):
            p.insert_before("\n\n")
            p.insert_after("\n\n")
            p.unwrap()
        for li in soup.find_all("li"):
            li.insert_before("\n- ")
            li.insert_after("")
            li.unwrap()
        for br in soup.find_all("br"):
            br.replace_with("\n")

        text = soup.get_text(separator=" ")
        return TemplateSourceBase.normalize_whitespace(text)

    async def polite_sleep(self) -> None:
        """Honor the per-source rate limit."""
        if self.request_delay_seconds > 0:
            await asyncio.sleep(self.request_delay_seconds)
