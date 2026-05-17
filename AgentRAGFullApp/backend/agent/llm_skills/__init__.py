"""LLM Skills · reusable primitives for the multi-agent pipeline.

A "skill" here is a SINGLE-PURPOSE call to an LLM with a typed input,
a typed output, and a clear tier preference (Router / Worker / Judge).

These are used by:
  - agent/workers/document_generator.py  · Sprint 3 LangGraph orchestrator
  - agent/tools/quality_validator.py      · Sprint 3 post-generation gate
  - agent/tools/slot_filler.py            · Sprint 3 inference helper

These are NOT used by:
  - utils/skill_runner.py (single-shot skill execution from firm_skills) ·
    that system is independent and continues to work unchanged.

Each primitive lives in its own file for tree-shake-friendly imports.
"""

from .base import LLMSkillResult, SkillExecutionError
from .router import classify_intent, RouterDecision
from .extractor import extract_structured, ExtractionField
from .judge import judge_quality, JudgeVerdict
from .critic import critique_section, CritiqueFinding

__all__ = [
    "LLMSkillResult",
    "SkillExecutionError",
    "classify_intent",
    "RouterDecision",
    "extract_structured",
    "ExtractionField",
    "judge_quality",
    "JudgeVerdict",
    "critique_section",
    "CritiqueFinding",
]
