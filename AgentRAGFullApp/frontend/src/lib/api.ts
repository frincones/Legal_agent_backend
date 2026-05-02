/**
 * API client for the Agent RAG backend.
 * Talks to the FastAPI server defined in backend/main.py
 */

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000';

export interface ActivityEvent {
  type: string;
  ts: number;
  // Loose payload — concrete fields depend on `type` (text, norm, data,
  // questions, archived_index, etc.). The frontend reconstructs panel
  // state by walking these in order.
  [key: string]: unknown;
}

export interface ActivityMetadata {
  events?: ActivityEvent[];
  case_index?: number;
  turn_in_case?: number;
  duration_seconds?: number;
  clarified?: boolean;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
  sources?: string[];
  activity_metadata?: ActivityMetadata;
}

export interface ChatRequestBody {
  message: string;
  session_id?: string | null;
  stream?: boolean;
}

export interface ChatResponse {
  response: string;
  intent?: string | null;
  sources: string[];
  session_id: string;
}

export type DocumentStatus = 'pending' | 'processing' | 'completed' | 'failed';

export interface DocumentItem {
  id: string;
  title: string;
  source: string;
  doc_type: string;
  status: DocumentStatus;
  ingestion_error: string | null;
  chunk_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface SessionItem {
  session_id: string;
  title: string;
  message_count: number;
  last_message_at: string | null;
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export async function sendChat(body: ChatRequestBody): Promise<ChatResponse> {
  const res = await fetch(`${API_BASE_URL}/api/chat/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, stream: false }),
  });
  return jsonOrThrow<ChatResponse>(res);
}

/**
 * Streaming chat. Returns an async iterator of text chunks.
 * Backend currently returns plain-text streaming (not SSE) so we
 * just decode chunks as they arrive.
 *
 * Optional `onSessionId` callback receives the X-Session-Id response
 * header before the first chunk arrives. The backend mints a new UUID
 * when `body.session_id` is null/missing — without this hook the
 * frontend would lose track of the session and every turn would land
 * in a fresh conversation row, breaking history, case_state and the
 * clarify-loop guard (which keys off prior assistant rows).
 */
export async function* sendChatStream(
  body: ChatRequestBody,
  onSessionId?: (id: string) => void,
): AsyncGenerator<string> {
  // Long AbortController timeout: complex dictamenes can take
  // 90-180s between web_research, vigencia verification, and the
  // main LLM stream. Default browser / Vercel fetch tolerances
  // sometimes sit at 60-90s and cut the response mid-stream,
  // which surfaces as "the agent never replied".
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10 * 60 * 1000);
  const res = await fetch(`${API_BASE_URL}/api/chat/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, stream: true }),
    signal: controller.signal,
  }).catch((err) => {
    clearTimeout(timer);
    throw err;
  });
  // Clear the timer once we have the response stream; the reader
  // loop below manages its own lifecycle.
  clearTimeout(timer);

  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
  }

  if (onSessionId) {
    const sid = res.headers.get('X-Session-Id') || res.headers.get('x-session-id');
    if (sid) onSessionId(sid);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    yield decoder.decode(value, { stream: true });
  }
}

export async function resetSession(): Promise<{ status: string; new_session_id: string }> {
  const res = await fetch(`${API_BASE_URL}/api/chat/reset`, { method: 'POST' });
  return jsonOrThrow(res);
}

export async function listDocuments(): Promise<{ documents: DocumentItem[]; total: number }> {
  const res = await fetch(`${API_BASE_URL}/api/documents/`);
  return jsonOrThrow(res);
}

export async function deleteDocument(id: string): Promise<{ status: string; id: string }> {
  const res = await fetch(`${API_BASE_URL}/api/documents/${id}`, { method: 'DELETE' });
  return jsonOrThrow(res);
}

export async function clearAllDocuments(): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE_URL}/api/documents/`, { method: 'DELETE' });
  return jsonOrThrow(res);
}

export async function ingestText(
  text: string,
  title = 'Untitled',
  source = 'api_input',
  doc_type = 'text'
): Promise<{ status: string; document_id: string }> {
  const res = await fetch(`${API_BASE_URL}/api/ingest/text`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, title, source, doc_type }),
  });
  return jsonOrThrow(res);
}

export async function ingestFiles(files: File[]): Promise<{
  total: number;
  queued: number;
  failed: number;
  results: { file: string; status: 'queued' | 'failed'; document_id?: string; error?: string }[];
}> {
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  const res = await fetch(`${API_BASE_URL}/api/ingest/files`, {
    method: 'POST',
    body: fd,
  });
  return jsonOrThrow(res);
}

export async function listSessions(): Promise<{ sessions: SessionItem[]; total: number }> {
  const res = await fetch(`${API_BASE_URL}/api/sessions/`);
  return jsonOrThrow(res);
}

export async function getSessionMessages(session_id: string): Promise<{
  session_id: string;
  messages: ChatMessage[];
}> {
  const res = await fetch(`${API_BASE_URL}/api/sessions/${encodeURIComponent(session_id)}`);
  return jsonOrThrow(res);
}

export async function deleteSession(session_id: string): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE_URL}/api/sessions/${encodeURIComponent(session_id)}`, {
    method: 'DELETE',
  });
  return jsonOrThrow(res);
}

export async function checkHealth(): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE_URL}/api/health`);
  return jsonOrThrow(res);
}

// ─────────────────────────────────────────────────────────────────────
// LEGAL ENDPOINTS
// ─────────────────────────────────────────────────────────────────────

export interface LegalSearchResult {
  source: string;
  tipo: string | null;
  numero: number | null;
  anio: number | null;
  titulo: string | null;
  estado: string | null;
  url: string | null;
  preview: string | null;
  metadata: Record<string, unknown>;
}

export interface VigenciaResult {
  norma_id: string | null;
  tipo: string;
  numero: number | null;
  anio: number | null;
  titulo: string | null;
  estado: string;
  encontrada: boolean;
  derogaciones: Record<string, unknown>[];
  cadena_completa: Record<string, unknown>[];
  badge: string;
}

export interface NormaItem {
  id: string;
  tipo: string;
  numero: number | null;
  anio: number | null;
  titulo: string | null;
  estado: string;
  fecha_expedicion: string | null;
  fuente_url: string | null;
  sector: string | null;
  temas: string[];
  created_at: string | null;
}

export interface SentenciaItem {
  source: string;
  tipo: string | null;
  numero: number | null;
  anio: number | null;
  titulo: string | null;
  estado: string | null;
  url: string | null;
  preview: string | null;
  metadata: Record<string, unknown>;
}

export async function searchLegalSources(
  query: string,
  sources?: string[],
  limit = 10,
): Promise<{ results: LegalSearchResult[]; sources_consulted: string[]; duration_ms: number; errors: unknown[] }> {
  const res = await fetch(`${API_BASE_URL}/api/legal/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, sources, limit }),
  });
  return jsonOrThrow(res);
}

export async function checkVigencia(
  tipo: string,
  numero: number,
  anio: number,
): Promise<VigenciaResult> {
  const res = await fetch(`${API_BASE_URL}/api/legal/vigencia`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tipo, numero, anio }),
  });
  return jsonOrThrow(res);
}

export async function ingestNorma(
  source: string,
  tipo: string,
  numero: number,
  anio: number,
): Promise<{ status: string; norma_id: string; titulo: string; derogations_detected: number; rag_chunks: number }> {
  const res = await fetch(`${API_BASE_URL}/api/legal/ingest-norma`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source, tipo, numero, anio }),
  });
  return jsonOrThrow(res);
}

export async function listNormas(
  tipo?: string,
  estado?: string,
  limit = 50,
): Promise<{ normas: NormaItem[]; total: number }> {
  const params = new URLSearchParams();
  if (tipo) params.set('tipo', tipo);
  if (estado) params.set('estado', estado);
  params.set('limit', String(limit));
  const res = await fetch(`${API_BASE_URL}/api/legal/normas?${params}`);
  return jsonOrThrow(res);
}

export async function getDerogaciones(
  normaId: string,
): Promise<{ norma_id: string; chain: Record<string, unknown>[] }> {
  const res = await fetch(`${API_BASE_URL}/api/legal/normas/${normaId}/derogaciones`);
  return jsonOrThrow(res);
}

export async function searchJurisprudencia(
  query: string,
  corte?: string,
  tipo_sentencia?: string,
  limit = 10,
): Promise<{ sentencias: SentenciaItem[]; total: number }> {
  const res = await fetch(`${API_BASE_URL}/api/legal/jurisprudencia/search`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, corte, tipo_sentencia, limit }),
  });
  return jsonOrThrow(res);
}

export async function listJurisprudencia(
  corte?: string,
  tipo?: string,
  limit = 50,
): Promise<{ sentencias: Record<string, unknown>[]; total: number }> {
  const params = new URLSearchParams();
  if (corte) params.set('corte', corte);
  if (tipo) params.set('tipo', tipo);
  params.set('limit', String(limit));
  const res = await fetch(`${API_BASE_URL}/api/legal/jurisprudencia?${params}`);
  return jsonOrThrow(res);
}

export async function attachFileToChat(
  file: File,
  sessionId: string,
): Promise<{ status: string; doc_id: string; filename: string; chunk_count: number }> {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('session_id', sessionId);
  const res = await fetch(`${API_BASE_URL}/api/chat/attach`, {
    method: 'POST',
    body: fd,
  });
  return jsonOrThrow(res);
}

export async function checkLegalHealth(): Promise<{ status: string; sources: Record<string, boolean> }> {
  const res = await fetch(`${API_BASE_URL}/api/legal/health`);
  return jsonOrThrow(res);
}
