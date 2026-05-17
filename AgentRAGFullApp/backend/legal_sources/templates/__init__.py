"""legal_sources.templates · scrapers + seeders for the system templates catalog.

Each scraper produces TemplateCandidate rows that land in the staging table
`template_candidates`. A curator reviews them via the Sprint 4 admin UI and
the approved rows are inserted into `user_templates` with firm_id=NULL
(system catalog visible to every firm).

Why this is separate from `legal_sources/` (norms/jurisprudence) base classes:
  · BaseLegalSource is for normative content searched by users at query time
  · This module is for batch ingestion of REUSABLE DOCUMENT TEMPLATES
    (contracts, demandas, tutelas, etc.) which feed a curation pipeline
  · Different lifecycle (batch-only, curator-reviewed, sourced once)
"""

from .base import TemplateCandidate, TemplateSourceBase, FetchResult

__all__ = ["TemplateCandidate", "TemplateSourceBase", "FetchResult"]
