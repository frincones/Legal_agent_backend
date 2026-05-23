"""Híbrido corpus: hot-fetch trigger + worker auto-ingest."""
from lex.hybrid.auto_ingest_worker import auto_ingest_tick, start_auto_ingest_worker

__all__ = ["auto_ingest_tick", "start_auto_ingest_worker"]
