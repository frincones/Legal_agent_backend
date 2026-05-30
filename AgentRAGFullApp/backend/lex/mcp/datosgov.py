"""MCP Server · datos.gov.co (Socrata API)."""
from __future__ import annotations

import os

from .base import BaseMCPClient


class DatosgovClient(BaseMCPClient):
    server_name = "datosgov"
    max_per_minute = 30   # Socrata es generoso con rate limit

    def _register_methods(self):
        return {
            "query_dataset": self.query_dataset,
            "download_csv": self.download_csv,
        }

    async def query_dataset(
        self, *, dataset_id: str,
        where: str | None = None,
        select: str | None = None,
        limit: int = 100,
    ) -> dict:
        """Query Socrata SoQL API.

        URL base: https://www.datos.gov.co/resource/{dataset_id}.json
        Docs: https://dev.socrata.com/docs/queries/
        """
        api_key = os.getenv("DATOSGOV_APP_TOKEN", "")
        base = f"https://www.datos.gov.co/resource/{dataset_id}.json"
        params = []
        if where:
            params.append(f"$where={where}")
        if select:
            params.append(f"$select={select}")
        params.append(f"$limit={int(limit)}")
        url = base + "?" + "&".join(params)

        headers = {"User-Agent": self.user_agent}
        if api_key:
            headers["X-App-Token"] = api_key

        try:
            import httpx
            client = self.http or httpx.AsyncClient(timeout=20.0)
            try:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                rows = resp.json()
                return {
                    "dataset_id": dataset_id,
                    "fuente_url": url,
                    "rows": rows[:limit] if isinstance(rows, list) else [],
                    "count": len(rows) if isinstance(rows, list) else 0,
                }
            finally:
                if self.http is None:
                    await client.aclose()
        except Exception as e:
            return {
                "dataset_id": dataset_id, "fuente_url": url,
                "rows": [], "error": str(e)[:200],
            }

    async def download_csv(self, *, dataset_id: str) -> dict:
        """Devuelve URL directa al CSV del dataset (no descarga, solo URL)."""
        url = f"https://www.datos.gov.co/resource/{dataset_id}.csv"
        return {
            "dataset_id": dataset_id,
            "csv_url": url,
            "note": "Descargar manualmente o via httpx.get(csv_url)",
        }
