"""
Generic URL fetcher for the web-research tool.

Given any public URL (typically a search result the LLM wants to read
in full), downloads the page and returns a cleaned, size-capped text
extract. Routes through scrape.do when a token is available so we
get consistent IPs and bypass basic bot blocks on government sites.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
MAX_CHARS_RETURNED = 8000  # cap so the tool response fits in the LLM context


def _clean_html_to_text(html: str) -> str:
    """Strip scripts/styles/nav chrome, collapse whitespace.

    Deliberately simple: no BS4 dep on this hot path. Real semantic
    extraction can be added later (e.g. trafilatura) if needed.
    """
    # Drop script and style blocks entirely.
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # Drop common chrome tags that rarely hold useful text.
    text = re.sub(r"<(nav|header|footer|aside|form)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    # Convert breaks and list items to newlines so paragraphs survive.
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text, flags=re.IGNORECASE)
    # Strip all remaining tags.
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities without pulling html.parser.
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#160;", " ")
        .replace("&quot;", '"')
    )
    # Collapse whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


async def _fetch_single(url: str, target_url: str) -> tuple[bool, Optional[httpx.Response], Optional[str]]:
    """Low-level GET that returns (ok, response_or_none, error_reason).

    SSL verification is disabled because several Colombian .gov.co sites
    (suin-juriscol.gov.co, funcionpublica.gov.co on older paths) serve
    certificates with incomplete chains that httpx rejects but curl/
    browsers accept. We don't handle payment data here; the data is
    public legal text.
    """
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=True,
            verify=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
                "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
            },
        ) as client:
            response = await client.get(target_url)
        if response.status_code == 200:
            return True, response, None
        return False, response, f"http_{response.status_code}"
    except Exception as e:
        return False, None, str(e)[:200]


async def fetch_url(
    url: str,
    scrape_do_token: Optional[str] = None,
    max_chars: int = MAX_CHARS_RETURNED,
) -> dict:
    """Fetch a URL and return `{ok, url, title, text, status, bytes}`.

    Tries scrape.do first when a token is present; on any failure
    (non-200, timeout, empty body) falls back to a direct request.
    Never raises — on total failure returns ok=False with a short
    error reason.
    """
    if not url or not url.startswith(("http://", "https://")):
        return {"ok": False, "url": url or "", "error": "invalid_url"}

    # Direct first: for .gov.co sites (suin-juriscol, funcionpublica,
    # corteconstitucional, secretariasenado) direct fetch returns 200
    # with full HTML, while scrape.do often 301/403s them. scrape.do
    # only earns its slot as fallback for rare cases where direct is
    # blocked (bot detection, Cloudflare, etc.).
    attempts: list[tuple[str, str]] = [("direct", url)]
    if scrape_do_token:
        attempts.append(
            ("scrape.do", f"https://api.scrape.do/?token={scrape_do_token}&url={quote_plus(url)}")
        )

    last_error = "no_attempts"
    last_status = None
    for label, target in attempts:
        ok, response, err = await _fetch_single(url, target)
        if ok and response is not None:
            html = response.text
            if not html or len(html) < 200:
                last_error = f"{label}_empty_body"
                continue
            title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
            text = _clean_html_to_text(html)
            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars] + "\n\n[...contenido truncado...]"
            return {
                "ok": True,
                "url": url,
                "title": title[:200],
                "text": text,
                "status": 200,
                "truncated": truncated,
                "bytes": len(html),
                "via": label,
            }
        last_error = f"{label}:{err or 'unknown'}"
        if response is not None:
            last_status = response.status_code
        logger.debug("fetch_url %s via %s failed: %s", url[:60], label, err)

    logger.info("fetch_url failed for %s after %d attempts: %s", url[:80], len(attempts), last_error)
    return {"ok": False, "url": url, "error": last_error, "status": last_status}
