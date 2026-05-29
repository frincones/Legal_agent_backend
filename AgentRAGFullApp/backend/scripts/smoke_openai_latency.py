"""Sprint M19.30 · Smoke de latencia OpenAI.

Mide cuánto tarda gpt-4o (y gpt-4o-mini) en responder a un ping mínimo
para descartar si el "pegado" del chat es por degradación de la API o
por un bug nuestro.

Uso:
    set OPENAI_API_KEY=<key>
    python scripts/smoke_openai_latency.py

Salida:
    OK   gpt-4o          duration=2310ms
    OK   gpt-4o-mini     duration=480ms
    o:
    FAIL gpt-4o          duration=15012ms reason=timeout
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx


PROMPT = "ping"
TIMEOUT_S = 15.0
MODELS = ["gpt-4o", "gpt-4o-mini"]


async def ping_model(model: str) -> dict:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return {"model": model, "ok": False, "reason": "no_api_key"}
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 3,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as cli:
            r = await cli.post("https://api.openai.com/v1/chat/completions",
                               json=body, headers=headers)
        dur = int((time.perf_counter() - t0) * 1000)
        if r.status_code == 200:
            return {"model": model, "ok": True, "duration_ms": dur, "status": 200}
        return {
            "model": model, "ok": False, "duration_ms": dur,
            "status": r.status_code,
            "reason": (r.text or "")[:200],
        }
    except httpx.TimeoutException:
        return {
            "model": model, "ok": False,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "reason": f"timeout_{TIMEOUT_S}s",
        }
    except Exception as e:
        return {
            "model": model, "ok": False,
            "duration_ms": int((time.perf_counter() - t0) * 1000),
            "reason": f"{type(e).__name__}: {e}",
        }


async def main() -> int:
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("ERROR: OPENAI_API_KEY no está configurada")
        return 2
    rows = []
    for m in MODELS:
        r = await ping_model(m)
        rows.append(r)
        ok = "OK  " if r.get("ok") else "FAIL"
        info = f"duration={r.get('duration_ms','?')}ms"
        if not r.get("ok"):
            info += f" reason={r.get('reason','?')}"
        print(f"{ok} {r['model']:<14} {info}")
    if all(r.get("ok") for r in rows):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
