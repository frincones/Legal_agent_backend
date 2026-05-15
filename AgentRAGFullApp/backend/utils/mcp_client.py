"""Sprint A · Cliente MCP genérico (JSON-RPC 2.0 sobre HTTP).

Para providers que ofrecen MCP server hosted (DocuSign, Notion, Zoom, etc.).
Implementación minimalista del protocolo Model Context Protocol.

Spec: https://spec.modelcontextprotocol.io/

Métodos soportados:
  - initialize           · handshake inicial
  - tools/list           · enumera tools disponibles
  - tools/call           · invoca un tool con argumentos
  - resources/list       · enumera recursos (opcional)

Uso típico (DocuSign):
  client = MCPClient(
      server_url="https://mcp-d.docusign.com/mcp",
      access_token="ya29...",
  )
  await client.initialize()
  result = await client.call_tool("send_envelope", {
      "signers": [...], "documents": [...]
  })
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class MCPError(Exception):
    """Error retornado por el servidor MCP."""
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP[{code}]: {message}")


class MCPClient:
    """Cliente JSON-RPC 2.0 para Model Context Protocol.

    Stateless · cada call hace una request HTTP POST. Si el server
    requiere session, se persiste en self.session_id.
    """

    def __init__(
        self,
        *,
        server_url: str,
        access_token: Optional[str] = None,
        timeout: float = 30.0,
        client_name: str = "lexai",
        client_version: str = "1.0.0",
    ):
        self.server_url = server_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout
        self.client_name = client_name
        self.client_version = client_version
        self.session_id: Optional[str] = None
        self._initialized = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def _request(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Hace una request JSON-RPC y retorna result. Lanza MCPError si hay error."""
        payload = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(8),
            "method": method,
            "params": params or {},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.server_url,
                headers=self._headers(),
                content=json.dumps(payload),
            )

        # Capturar Mcp-Session-Id si lo asigna el server (post-initialize)
        sess = response.headers.get("Mcp-Session-Id")
        if sess and not self.session_id:
            self.session_id = sess

        response.raise_for_status()
        ctype = response.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            # Server-Sent Events · MCP a veces los usa para streaming
            text = response.text
            return self._parse_sse(text)

        data = response.json()
        if "error" in data:
            err = data["error"]
            raise MCPError(err.get("code", -1), err.get("message", ""), err.get("data"))
        return data.get("result")

    def _parse_sse(self, body: str) -> Any:
        """Parser SSE muy simple · busca último evento 'message' con JSON."""
        result = None
        for line in body.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                    if "result" in msg:
                        result = msg["result"]
                    elif "error" in msg:
                        err = msg["error"]
                        raise MCPError(
                            err.get("code", -1),
                            err.get("message", ""),
                            err.get("data"),
                        )
                except json.JSONDecodeError:
                    continue
        return result

    # ─────────────────────────────────────────────────────────────────
    # MCP protocol methods
    # ─────────────────────────────────────────────────────────────────

    async def initialize(self) -> dict[str, Any]:
        """Handshake inicial · obligatorio antes de cualquier otra call."""
        if self._initialized:
            return {}
        result = await self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                    "resources": {},
                },
                "clientInfo": {
                    "name": self.client_name,
                    "version": self.client_version,
                },
            },
        )
        # Enviar notification "initialized" (sin id, sin esperar respuesta)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    self.server_url,
                    headers=self._headers(),
                    content=json.dumps({
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                    }),
                )
        except Exception as e:
            logger.debug("notifications/initialized failed (non-fatal): %s", e)
        self._initialized = True
        return result or {}

    async def list_tools(self) -> list[dict[str, Any]]:
        """Lista los tools disponibles en este server MCP."""
        if not self._initialized:
            await self.initialize()
        result = await self._request("tools/list")
        return (result or {}).get("tools", [])

    async def call_tool(
        self,
        name: str,
        arguments: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Invoca un tool con argumentos. Retorna dict con keys
        'content' (list de items) y 'isError' (bool)."""
        if not self._initialized:
            await self.initialize()
        result = await self._request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
        )
        return result or {}

    async def list_resources(self) -> list[dict[str, Any]]:
        """Lista recursos disponibles (opcional · no todos los servers)."""
        if not self._initialized:
            await self.initialize()
        try:
            result = await self._request("resources/list")
            return (result or {}).get("resources", [])
        except MCPError as e:
            if e.code == -32601:  # method not found
                return []
            raise

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Lee un resource por URI."""
        if not self._initialized:
            await self.initialize()
        result = await self._request("resources/read", {"uri": uri})
        return result or {}
