"""Configuration schema with Pydantic validation and YAML loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field


class AgentSettings(BaseModel):
    name: str = "RAG Agent"
    role: str = "Knowledge Assistant"
    primary_model: str = "gpt-4o"
    utility_model: str = "gpt-4o-mini"
    temperature: float = 0.3
    max_tokens: int = 2000
    db_tables_schema: Optional[str] = None
    system_prompt_template: Optional[str] = None


class ChunkingSettings(BaseModel):
    strategy: str = "auto"  # auto | hybrid | code | semantic | simple
    max_tokens: int = 512
    chunk_size: int = 1000
    chunk_overlap: int = 200
    min_chunk_size: int = 100
    merge_peers: bool = True


class EnrichmentSettings(BaseModel):
    enabled: bool = True
    model: str = "gpt-4o-mini"
    max_concurrent: int = 5
    max_document_chars: int = 4000


class EmbeddingSettings(BaseModel):
    model: str = "text-embedding-3-small"
    dimensions: int = 1536
    batch_size: int = 100
    max_retries: int = 3
    use_cache: bool = True


class FormatSettings(BaseModel):
    documents: bool = True   # PDF, DOCX, PPTX, XLSX, HTML
    text: bool = True        # TXT, MD
    code: bool = True        # PY, TS, JS, etc.
    structured: bool = True  # CSV, JSON, XML, YAML
    audio: bool = False      # MP3, WAV (requires Whisper)
    images: bool = False     # PNG, JPG (requires Vision LLM)
    subtitles: bool = True   # SRT, VTT


class IngestionSettings(BaseModel):
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    enrichment: EnrichmentSettings = Field(default_factory=EnrichmentSettings)
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    formats: FormatSettings = Field(default_factory=FormatSettings)


class IntentRouterSettings(BaseModel):
    enabled: bool = True
    model: str = "gpt-4o-mini"


class QueryExpansionSettings(BaseModel):
    enabled: bool = True
    model: str = "gpt-4o-mini"


class MultiQuerySettings(BaseModel):
    enabled: bool = True
    num_variations: int = 3
    parallel: bool = True


class RerankingSettings(BaseModel):
    enabled: bool = True
    model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    candidates: int = 20
    top_k: int = 5


class SelfReflectionSettings(BaseModel):
    enabled: bool = True
    model: str = "gpt-4o-mini"
    threshold: int = 3
    max_retries: int = 1


class HybridSearchSettings(BaseModel):
    enabled: bool = True
    text_weight: float = 0.3


class ConversationMemorySettings(BaseModel):
    enabled: bool = False     # Whether to search past conversations as RAG context
    save_to_rag: bool = False  # Whether to embed/save exchanges to conversation_chunks


class RetrievalSettings(BaseModel):
    intent_router: IntentRouterSettings = Field(default_factory=IntentRouterSettings)
    query_expansion: QueryExpansionSettings = Field(default_factory=QueryExpansionSettings)
    multi_query: MultiQuerySettings = Field(default_factory=MultiQuerySettings)
    reranking: RerankingSettings = Field(default_factory=RerankingSettings)
    self_reflection: SelfReflectionSettings = Field(default_factory=SelfReflectionSettings)
    hybrid_search: HybridSearchSettings = Field(default_factory=HybridSearchSettings)
    conversation_memory: ConversationMemorySettings = Field(
        default_factory=ConversationMemorySettings
    )


class StorageSettings(BaseModel):
    provider: str = "supabase"  # supabase | postgres
    connection_string: str = ""
    pool_min: int = 2
    pool_max: int = 10
    similarity_threshold: float = 0.7


class LegalSourcesSettings(BaseModel):
    """Configuration for live legal source connections."""
    enabled: bool = True
    datos_gov_co: bool = True
    senado: bool = True
    funcion_publica: bool = True
    corte_constitucional: bool = True
    corte_suprema: bool = False       # Needs Playwright (heavy)
    diario_oficial: bool = False       # Needs Playwright (heavy)
    max_concurrent_sources: int = 3
    search_timeout_seconds: int = 15
    auto_check_vigencia: bool = True
    auto_ingest_found_norms: bool = False
    scrape_do_token: Optional[str] = None  # scrape.do API token for proxy scraping


class DerogationSettings(BaseModel):
    """Configuration for the derogation graph."""
    enabled: bool = True
    auto_detect: bool = True
    verify_on_response: bool = True


class ClarificationSettings(BaseModel):
    """Optional pre-pipeline LLM gate that asks the user for missing critical
    context (e.g. contract type, salary, antiquity) instead of guessing.
    Only fires for legal queries that pass cheap pre-filters. Disabled
    silently degrades to the previous behavior."""
    enabled: bool = True
    model: str = "gpt-4o-mini"
    max_questions: int = 3
    skip_on_followup: bool = True


class WebResearchSettings(BaseModel):
    """Pre-response fact-verification pass powered by OpenAI tool use.

    When enabled the agent runs a secondary LLM (default gpt-4o-mini)
    that can call `search_web` and `fetch_url` tools. The returned
    "verified facts" block is injected into the main prompt. This is
    what catches stale RAG (a law modified by a newer law) and
    surfaces recent jurisprudence the RAG hasn't ingested yet."""
    enabled: bool = True
    model: str = "gpt-4o-mini"
    max_tool_calls: int = 5            # hard ceiling to bound latency/cost
    only_for_legal: bool = True        # skip on chitchat/clarify turns


class ConversationContextSettings(BaseModel):
    """Long-conversation hygiene: compact old turns when history grows past
    a threshold, and detect when the user switches to a different case so
    we can archive the previous case_state instead of contaminating it.

    Both features are independent (each has its own enable flag) and degrade
    silently to the previous behavior when disabled — no other pipeline
    stage depends on them."""
    compaction_enabled: bool = True
    compaction_model: str = "gpt-4o-mini"
    compaction_threshold_messages: int = 30   # raw messages, ≈ 15 turns
    compaction_keep_recent: int = 10          # recent messages kept verbatim
    compaction_min_to_summarize: int = 8      # don't bother if older slice < N

    case_shift_enabled: bool = True
    case_shift_model: str = "gpt-4o-mini"
    case_shift_min_confidence: float = 0.75   # below this → treat as continuation


class AppConfig(BaseModel):
    """Root configuration for the Agent RAG Template."""

    agent: AgentSettings = Field(default_factory=AgentSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    legal_sources: LegalSourcesSettings = Field(default_factory=LegalSourcesSettings)
    derogation: DerogationSettings = Field(default_factory=DerogationSettings)
    clarification: ClarificationSettings = Field(default_factory=ClarificationSettings)
    conversation_context: ConversationContextSettings = Field(
        default_factory=ConversationContextSettings
    )
    web_research: WebResearchSettings = Field(default_factory=WebResearchSettings)


def _resolve_env_vars(data: Any) -> Any:
    """Recursively resolve ${ENV_VAR} references in config values."""
    if isinstance(data, str) and data.startswith("${") and data.endswith("}"):
        env_var = data[2:-1]
        return os.getenv(env_var, data)
    elif isinstance(data, dict):
        return {k: _resolve_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_env_vars(item) for item in data]
    return data


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file with env var resolution."""
    if config_path is None:
        config_path = os.getenv(
            "RAG_CONFIG_PATH",
            str(Path(__file__).parent / "default.yaml"),
        )

    path = Path(config_path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        resolved = _resolve_env_vars(raw)
        return AppConfig(**resolved)

    return AppConfig()
