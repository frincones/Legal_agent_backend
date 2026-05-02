"""Base abstract para sub-agentes especializados.

Cada subagente:
  - Tiene un nombre, prompt sistémico, set de tools permitidas y modelo.
  - Recibe (task: str, ctx: dict) y devuelve {summary, tool_calls, tokens}.
  - Internamente hace llm_generate con sus tools subset, ejecuta tool calls
    contra el _tool_registry global del backend, y persiste un agent_run.

Loop guard: ctx.subagent_chain limita profundidad para evitar recursión.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC
from typing import Optional

logger = logging.getLogger(__name__)

MAX_DEPTH = 3
MAX_TOOL_ITERATIONS = 6


class BaseSubAgent(ABC):
    name: str = "base"
    description: str = ""
    system_prompt: str = ""
    allowed_tools: list[str] = []
    model: str = "gpt-4o-mini"
    temperature: float = 0.2
    max_tokens: int = 1500

    async def run(self, task: str, ctx: dict) -> dict:
        """Ejecuta la tarea. Devuelve dict con summary, tool_calls, tokens."""
        from openai import AsyncOpenAI
        import os

        chain = list(ctx.get("subagent_chain") or [])
        if len(chain) >= MAX_DEPTH:
            return {
                "error": f"max delegation depth {MAX_DEPTH} reached",
                "chain": chain,
            }
        chain.append(self.name)
        sub_ctx = {**ctx, "subagent_chain": chain}

        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        # Filtra _tool_registry al subset permitido por este subagente.
        from api.voice import _tool_registry, _tool_descriptors
        all_descriptors = _tool_descriptors()
        my_descriptors = [d for d in all_descriptors if d["name"] in self.allowed_tools]
        # Normalizar al shape de Chat Completions tools
        chat_tools = [{"type": "function", "function": d} for d in my_descriptors]

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]

        started = time.time()
        tool_calls_log: list[dict] = []
        tokens_in = 0
        tokens_out = 0

        for _ in range(MAX_TOOL_ITERATIONS):
            try:
                resp = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=chat_tools or None,
                    tool_choice="auto" if chat_tools else "none",
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except Exception as e:
                logger.exception("subagent %s LLM error: %s", self.name, e)
                return {"error": f"LLM error: {e}", "summary": None}

            if resp.usage:
                tokens_in += resp.usage.prompt_tokens
                tokens_out += resp.usage.completion_tokens

            choice = resp.choices[0]
            message = choice.message
            messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in (message.tool_calls or [])
                ] if message.tool_calls else None,
            })

            if not message.tool_calls:
                # Modelo terminó · respuesta final.
                break

            # Ejecuta cada tool call y mete el resultado en messages.
            for tc in message.tool_calls:
                fn_name = tc.function.name
                try:
                    fn_args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fn_args = {}

                fn = _tool_registry.get(fn_name)
                if fn is None or fn_name not in self.allowed_tools:
                    tool_result = {"error": f"tool '{fn_name}' not allowed for subagent {self.name}"}
                else:
                    try:
                        tool_result = await fn(args=fn_args, ctx=sub_ctx)
                    except Exception as e:
                        logger.exception("subagent %s tool %s raised: %s", self.name, fn_name, e)
                        tool_result = {"error": str(e)}

                # _ui_command no aplica para subagent calls (no hay browser asociado),
                # lo strippeamos para no contaminar el contexto.
                if isinstance(tool_result, dict) and "_ui_command" in tool_result:
                    tool_result.pop("_ui_command", None)

                tool_calls_log.append({
                    "name": fn_name,
                    "args": fn_args,
                    "result_preview": _trim(tool_result, 240),
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                })

        duration_ms = int((time.time() - started) * 1000)
        final = next(
            (m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
            None,
        )

        return {
            "subagent": self.name,
            "summary": (final["content"] if final else "(sin respuesta)"),
            "tool_calls": tool_calls_log,
            "duration_ms": duration_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }


def _trim(obj, max_chars: int = 240) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        s = str(obj)
    return s if len(s) <= max_chars else s[:max_chars] + "…"
