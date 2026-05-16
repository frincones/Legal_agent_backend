"""Sprint J · Yjs WebSocket relay para colaboración real-time en Canvas.

Implementa un relay binario sin estado propio: cada cliente Yjs mantiene su
propio Y.Doc, y este servidor solo retransmite los mensajes binarios (updates
+ awareness) a los demás clientes en el mismo room.

Endpoints:
  POST /v1/canvas/ws-ticket       · emite ticket HMAC (Supabase JWT requerido)
  WS   /v1/canvas/collab/{room}?ticket=...  · conexión y-websocket-compatible

Room name: el frontend usa `lexai-matter:{matter_id}`; aceptamos cualquier
string. Se valida que el ticket tenga acceso al firm_id (no a un matter
específico; los abogados de la misma firma pueden colaborar).

Implementación protocol y-websocket:
  - Mensajes son binary frames opacos para este server.
  - Broadcast: enviar a TODOS los demás clientes del mismo room.
  - Cuando un cliente entra: NO se envía estado · y-websocket clients hacen
    sync step 1 al conectar; cada cliente existente responde con sync step 2.
  - Mantenemos solo set de WebSockets por room; sin persistencia.

Persistencia: el autosave de TipTap a Supabase (3s debounce) cubre el caso
de "todos se desconectaron y queremos recuperar". El frontend recarga del
último matter_document_version al abrir.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from utils.auth import (
    Principal,
    get_current_firm,
    issue_voice_ticket,  # reutilizamos el HMAC del ticket de voz
    verify_voice_ticket,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/canvas", tags=["canvas-collab"])


# room → set of WebSockets
_rooms: dict[str, set[WebSocket]] = {}
_room_lock = asyncio.Lock()


@router.post("/ws-ticket")
async def issue_canvas_ws_ticket(
    matter_id: Optional[str] = None,
    principal: Principal = Depends(get_current_firm),
):
    """Emite un ticket HMAC corto para abrir la conexión WS colaborativa.

    Reutilizamos issue_voice_ticket porque el formato es idéntico (sub, firm_id,
    matter_id, exp). El tipo de uso lo discrimina el endpoint que valida.
    """
    return issue_voice_ticket(principal, matter_id=matter_id)


@router.websocket("/collab/{room}")
async def canvas_collab_ws(websocket: WebSocket, room: str, ticket: str = Query(...)):
    """y-websocket relay puro · broadcast binary frames a peers del mismo room.

    El protocolo y-websocket es opaco para este server; solo reenviamos bytes.
    """
    # Validar ticket
    try:
        payload = verify_voice_ticket(ticket)
    except HTTPException as e:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=str(e.detail))
        return

    firm_id = payload.get("firm_id")
    if not firm_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="missing firm_id")
        return

    # Room namespace: si el frontend manda `lexai-matter:<uuid>`, lo aceptamos
    # tal cual; lo prefijamos con firm_id para aislar entre tenants.
    full_room = f"firm:{firm_id}::{room}"

    await websocket.accept()
    conn_id = uuid.uuid4().hex[:8]
    logger.info("[canvas-collab] %s connected to %s (user=%s)", conn_id, full_room, payload.get("sub"))

    async with _room_lock:
        peers = _rooms.setdefault(full_room, set())
        peers.add(websocket)
        peer_count = len(peers)

    try:
        # Bucle de relay · cada mensaje del cliente se reenvía a los demás.
        while True:
            msg = await websocket.receive()
            # Soporta tanto frames binarios como textos JSON (awareness puede ser
            # texto en algunos clientes; y-websocket usa binary frames principal).
            if msg.get("type") != "websocket.receive":
                continue
            bytes_data = msg.get("bytes")
            text_data = msg.get("text")
            # Broadcast a peers
            async with _room_lock:
                peers_snapshot = list(_rooms.get(full_room, set()))
            for peer in peers_snapshot:
                if peer is websocket:
                    continue
                try:
                    if bytes_data is not None:
                        await peer.send_bytes(bytes_data)
                    elif text_data is not None:
                        await peer.send_text(text_data)
                except Exception as e:
                    logger.debug("[canvas-collab] peer send failed (will drop): %s", e)
    except WebSocketDisconnect:
        logger.info("[canvas-collab] %s disconnected from %s", conn_id, full_room)
    except Exception as e:
        logger.warning("[canvas-collab] %s error: %s", conn_id, e)
    finally:
        async with _room_lock:
            peers = _rooms.get(full_room)
            if peers:
                peers.discard(websocket)
                if not peers:
                    _rooms.pop(full_room, None)
