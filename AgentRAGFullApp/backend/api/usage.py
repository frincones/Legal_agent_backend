"""Usage tracking endpoints: token consumption and OpenAI cost stats."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from utils.usage_tracker import tracker, PRICING

router = APIRouter(prefix="/usage", tags=["usage"])


def _format_stats(stats) -> dict:
    """Convert UsageStats dataclass to JSON-friendly dict."""
    return {
        "total_calls": stats.total_calls,
        "total_input_tokens": stats.total_input_tokens,
        "total_output_tokens": stats.total_output_tokens,
        "total_tokens": stats.total_input_tokens + stats.total_output_tokens,
        "total_cost_usd": round(stats.total_cost_usd, 6),
        "by_model": {
            k: {
                "calls": v["calls"],
                "input_tokens": v["input"],
                "output_tokens": v["output"],
                "total_tokens": v["input"] + v["output"],
                "cost_usd": round(v["cost"], 6),
            }
            for k, v in stats.by_model.items()
        },
        "by_purpose": {
            k: {
                "calls": v["calls"],
                "input_tokens": v["input"],
                "output_tokens": v["output"],
                "total_tokens": v["input"] + v["output"],
                "cost_usd": round(v["cost"], 6),
            }
            for k, v in stats.by_purpose.items()
        },
    }


@router.get("/")
async def get_usage(session_id: Optional[str] = None):
    """Get aggregated usage stats. Filter by session_id if provided."""
    stats = tracker.stats(session_id=session_id)
    result = _format_stats(stats)
    if session_id is None:
        # Include per-session breakdown only for global view
        result["by_session"] = {
            k: {
                "calls": v["calls"],
                "input_tokens": v["input"],
                "output_tokens": v["output"],
                "total_tokens": v["input"] + v["output"],
                "cost_usd": round(v["cost"], 6),
            }
            for k, v in stats.by_session.items()
        }
    return result


@router.get("/pricing")
async def get_pricing():
    """Show the OpenAI pricing table used for cost calculation."""
    return {
        "currency": "USD",
        "unit": "per 1M tokens",
        "models": PRICING,
    }


@router.post("/reset")
async def reset_usage():
    """Clear all recorded usage events."""
    cleared = tracker.reset()
    return {"status": "reset", "events_cleared": cleared}
