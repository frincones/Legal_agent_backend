"""
Web search for the legal agent's fact-verification pass.

Uses DuckDuckGo's HTML endpoint (no API key needed) proxied through
scrape.do when a token is available, falling back to a direct fetch.
Returns a small list of {title, url, snippet} dicts that the LLM
orchestrator in agent/web_research.py consumes as tool output.

Why DDG HTML and not Google/Bing/Tavily:
- no API key to manage,
- works with the scrape.do token we already have in env,
- the top 5-10 organic results are enough to ground a fact check.

Query tuning: callers usually bias the query with Colombian legal
domains via `site_filter=True`, which appends the top 5 official sites
as `site:` operators. That keeps results close to SUIN, Senado, Corte CC
and Función Pública and discards forum chatter.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger(__name__)

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

# Trusted Colombian legal domains. When site_filter=True we OR them
# into a DuckDuckGo `site:(a OR b OR c)` clause so the agent sticks to
# primary sources.
LEGAL_DOMAINS = [
    "corteconstitucional.gov.co",
    "secretariasenado.gov.co",
    "funcionpublica.gov.co",
    "suin-juriscol.gov.co",
    "mintrabajo.gov.co",
    "minjusticia.gov.co",
    "ramajudicial.gov.co",
    "datos.gov.co",
]


def _build_query(query: str, site_filter: bool) -> str:
    if not site_filter:
        return query
    sites = " OR ".join(f"site:{d}" for d in LEGAL_DOMAINS)
    return f"{query} ({sites})"


def _extract_results_from_html(html: str, limit: int) -> list[dict]:
    """Parse DuckDuckGo HTML results into simple dicts.

    The DDG HTML layout is stable enough that a regex over the result
    anchors + snippet divs is reliable; we avoid a BeautifulSoup dep
    on the hot path.
    """
    results: list[dict] = []

    # DDG renders results inside <div class="result ...">. Pull each block
    # and then extract title, URL (unwrap the `uddg` redirect), snippet.
    blocks = re.findall(
        r'<div class="result[^"]*?"[^>]*>.*?</div>\s*</div>\s*</div>',
        html,
        re.DOTALL,
    )

    for block in blocks[: limit * 2]:  # over-fetch, we'll filter empties
        title_m = re.search(
            r'<a[^>]+class="result__a"[^>]*>(.*?)</a>', block, re.DOTALL
        )
        url_m = re.search(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', block
        )
        snippet_m = re.search(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            block,
            re.DOTALL,
        ) or re.search(
            r'<div class="result__snippet"[^>]*>(.*?)</div>', block, re.DOTALL
        )

        if not (title_m and url_m):
            continue

        title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
        url = url_m.group(1)
        # DDG wraps outbound URLs in a redirect: /l/?uddg=<encoded>
        m = re.search(r"uddg=([^&]+)", url)
        if m:
            from urllib.parse import unquote

            url = unquote(m.group(1))
        snippet = ""
        if snippet_m:
            snippet = re.sub(r"<[^>]+>", "", snippet_m.group(1)).strip()

        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet[:400]})
        if len(results) >= limit:
            break

    return results


async def search_web(
    query: str,
    limit: int = 5,
    site_filter: bool = True,
    scrape_do_token: Optional[str] = None,
) -> list[dict]:
    """Search the web and return up to `limit` result dicts.

    Args:
        query: natural-language search query.
        limit: max number of results (1-10).
        site_filter: if True, restrict results to the Colombian legal
            domain allowlist via DDG `site:` operators.
        scrape_do_token: when provided, route through scrape.do so we
            don't hammer DDG directly from Railway (which DDG sometimes
            rate-limits).

    Returns an empty list on any failure — never raises.
    """
    limit = max(1, min(limit, 10))
    q = _build_query(query, site_filter)
    ddg_url = f"{DDG_HTML_URL}?q={quote_plus(q)}"

    if scrape_do_token:
        # scrape.do wraps the request; API: https://scrape.do/?token=X&url=Y
        fetch_url = (
            f"https://api.scrape.do/?token={scrape_do_token}"
            f"&url={quote_plus(ddg_url)}"
        )
    else:
        fetch_url = ddg_url

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
                )
            },
        ) as client:
            response = await client.get(fetch_url)
            if response.status_code != 200:
                logger.warning(
                    "web_search: DDG returned %d for %r", response.status_code, query[:60]
                )
                return []

            results = _extract_results_from_html(response.text, limit)
            logger.info(
                "web_search: %d results for %r (site_filter=%s)",
                len(results), query[:80], site_filter,
            )
            return results
    except Exception as e:
        logger.warning("web_search failed for %r: %s", query[:60], e)
        return []
