import { useState, useEffect, useCallback, useRef } from 'react';
import { Sidebar } from './components/Sidebar';
import { Navbar } from './components/Navbar';
import { ChatArea } from './components/ChatArea';
import { MessageInput } from './components/MessageInput';
import { SettingsPanel } from './components/SettingsPanel';
import { KnowledgePanel } from './components/KnowledgePanel';
import ActivityPanel from './components/ActivityPanel';
import SessionStats from './components/SessionStats';
import type { VigenciaItem, SourceRef } from './components/ActivityPanel';
import type { ThinkingStep } from './components/AgentThinking';
import { Toaster } from './components/ui/sonner';
import { toast } from 'sonner';
import {
  sendChatStream,
  resetSession,
  listSessions,
  getSessionMessages,
  deleteSession,
  type SessionItem,
} from '../lib/api';

export interface ClarifyQuestion {
  id: string;
  label: string;
  type: 'radio' | 'text';
  options?: string[];
}

/**
 * Inline marker rendered between turns when the agent archives a case
 * (case_shift) or compacts older history (compact). Lives as a Message
 * with role='assistant' but an empty content; ChatMessage detects the
 * marker and renders a divider instead of the normal avatar+bubble.
 */
export interface SystemMarker {
  kind: 'case_shift' | 'compact';
  // case_shift fields
  archivedIndex?: number;
  newIndex?: number;
  reason?: string;
  confidence?: number;
  // compact fields
  summarizedMessages?: number;
  keptRecent?: number;
}

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
  sources?: string[];
  thinkingSteps?: ThinkingStep[];
  thinkingDuration?: number;
  vigencia?: VigenciaItem[];
  sourceRefs?: SourceRef[];
  jurisprudencia?: Record<string, unknown>[];
  clarifyQuestions?: ClarifyQuestion[];
  clarifyReason?: string;
  // Multi-case grouping (for ActivityPanel header)
  caseIndex?: number;
  turnInCase?: number;
  // Inline chat marker (no content rendered as bubble)
  systemMarker?: SystemMarker;
}

function fmtTime(d: Date = new Date()): string {
  return d.toLocaleTimeString('es-ES', { hour: '2-digit', minute: '2-digit' });
}

export default function App() {
  const [darkMode, setDarkMode] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isKnowledgeOpen, setIsKnowledgeOpen] = useState(false);
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);
  const [isActivityOpen, setIsActivityOpen] = useState(false);
  const [activityMessageId, setActivityMessageId] = useState<string | null>(null);

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [streamAbort, setStreamAbort] = useState<(() => void) | null>(null);

  // Thinking state — live during streaming
  const [thinkingSteps, setThinkingSteps] = useState<ThinkingStep[]>([]);
  const [caseState, setCaseState] = useState<Record<string, unknown> | null>(null);

  // Responsive: open sidebar by default on desktop
  useEffect(() => {
    setIsSidebarOpen(window.innerWidth >= 768);
  }, []);

  useEffect(() => {
    if (darkMode) document.documentElement.classList.add('dark');
    else document.documentElement.classList.remove('dark');
  }, [darkMode]);

  const refreshSessions = useCallback(async () => {
    try {
      const data = await listSessions();
      setSessions(data.sessions);
    } catch (e) {
      console.warn('Could not load sessions:', e);
    }
  }, []);

  useEffect(() => { refreshSessions(); }, [refreshSessions]);

  // ─── Parse streaming protocol ───
  const handleSendMessage = useCallback(
    async (content: string) => {
      const userMessage: Message = {
        id: crypto.randomUUID(), role: 'user', content, timestamp: fmtTime(),
      };
      const assistantId = crypto.randomUUID();
      const placeholder: Message = {
        id: assistantId, role: 'assistant', content: '', timestamp: fmtTime(),
        thinkingSteps: [], vigencia: [],
      };

      setMessages(prev => [...prev, userMessage, placeholder]);
      setIsLoading(true);
      setThinkingSteps([]);

      let aborted = false;
      setStreamAbort(() => () => { aborted = true; });

      // Close sidebar on mobile when sending
      if (window.innerWidth < 768) setIsSidebarOpen(false);

      try {
        let acc = '';
        let sc = 0;
        const steps: ThinkingStep[] = [];
        const vig: VigenciaItem[] = [];
        const srcs: string[] = [];
        const refs: SourceRef[] = [];
        const juris: Record<string, unknown>[] = [];
        let dur: number | undefined;
        let buf = '';
        // Throttle setMessages to every ~60ms so we don't re-render on
        // every token. For long dictamenes (1000+ tokens) the naive
        // "render per token" path was firing 1000+ React updates with
        // array copies each, which locked the tab and made the answer
        // look like it never arrived. ~16 FPS is plenty for stream UX.
        let lastMsgUpdate = 0;
        const MSG_UPDATE_MS = 60;

        for await (const chunk of sendChatStream(
          { message: content, session_id: sessionId, stream: true },
          // Capture the X-Session-Id header on the first chunk so the
          // next turn lands in the same conversation row. Without this
          // every message minted a fresh session_id server-side and the
          // clarify-loop guard, case_state and history all broke.
          (sid) => { if (sid && sid !== sessionId) setSessionId(sid); },
        )) {
          if (aborted) break;
          buf += chunk;

          // Split NDJSON lines
          const lines = buf.split('\n');
          buf = lines.pop() || '';

          for (const line of lines) {
            const t = line.trim();
            if (!t) continue;

            // Try JSON parse (NDJSON protocol)
            try {
              const evt = JSON.parse(t);
              switch (evt.type) {
                case 'status':
                  if (steps.length > 0) steps[steps.length - 1].status = 'completed';
                  steps.push({ id: `s-${sc++}`, text: evt.text, status: 'active', type: 'status', timestamp: Date.now() });
                  setThinkingSteps([...steps]);
                  break;
                case 'ingest':
                  steps.push({ id: `i-${sc++}`, text: evt.norm, status: 'completed', type: 'ingest', timestamp: Date.now() });
                  setThinkingSteps([...steps]);
                  break;
                case 'token':
                  acc += evt.text;
                  break;
                case 'vigencia':
                  vig.push(evt.data);
                  break;
                case 'jurisprudencia':
                  juris.push(evt.data);
                  break;
                case 'sources':
                  srcs.push(...evt.data);
                  break;
                case 'sourcerefs':
                  refs.push(...evt.data);
                  break;
                case 'casestate': {
                  setCaseState(evt.data);
                  // Also stamp the assistant message with case_index +
                  // turn_in_case so the Activity Panel header can show
                  // "Caso N • Turno M" even on a fresh stream (without
                  // round-tripping through the DB).
                  const ci = (evt.data as Record<string, unknown>)?.case_index as number | undefined;
                  if (ci !== undefined) {
                    const archived = ((evt.data as Record<string, unknown>)?.archived_cases as Array<{ turn_count_at_archive?: number }> | undefined) || [];
                    const consumed = archived.reduce((s, c) => s + (c.turn_count_at_archive || 0), 0);
                    const totalTurns = ((evt.data as Record<string, unknown>)?.turn_count as number | undefined) || 0;
                    const turnInCase = Math.max(1, totalTurns - consumed);
                    setMessages(prev => prev.map(m =>
                      m.id === assistantId ? { ...m, caseIndex: ci, turnInCase } : m
                    ));
                  }
                  break;
                }
                case 'clarify':
                  // Backend asked the user for missing context. Attach the
                  // questions to the assistant placeholder; ChatMessage
                  // renders them as an inline form. No tokens follow this
                  // event — the stream closes with `done.clarified=true`.
                  setMessages(prev => prev.map(m =>
                    m.id === assistantId ? {
                      ...m,
                      clarifyQuestions: evt.questions,
                      clarifyReason: evt.reason,
                    } : m
                  ));
                  break;
                case 'case_shift': {
                  // Insert a permanent marker in the chat (Claude Code's
                  // compact_boundary style) so the user always sees where
                  // the case rolled over, even after refresh. Toast stays
                  // for immediate feedback.
                  const marker: Message = {
                    id: `marker-shift-${crypto.randomUUID()}`,
                    role: 'assistant',
                    content: '',
                    systemMarker: {
                      kind: 'case_shift',
                      archivedIndex: evt.archived_index,
                      newIndex: evt.new_index,
                      reason: evt.reason,
                      confidence: evt.confidence,
                    },
                  };
                  setMessages(prev => {
                    // Insert marker BEFORE the streaming assistant placeholder
                    // (so the order reads: …user → ── Caso N → assistant…).
                    const idx = prev.findIndex(m => m.id === assistantId);
                    if (idx < 0) return [...prev, marker];
                    return [...prev.slice(0, idx), marker, ...prev.slice(idx)];
                  });
                  toast.info(
                    `Caso ${evt.archived_index} archivado. Iniciando caso ${evt.new_index}.`,
                    { description: evt.reason || undefined, duration: 4000 }
                  );
                  break;
                }
                case 'compact': {
                  // Same pattern: a permanent inline marker so the user
                  // sees in the chat that older context was summarized.
                  const marker: Message = {
                    id: `marker-compact-${crypto.randomUUID()}`,
                    role: 'assistant',
                    content: '',
                    systemMarker: {
                      kind: 'compact',
                      summarizedMessages: evt.summarized_messages,
                      keptRecent: evt.kept_recent,
                    },
                  };
                  setMessages(prev => {
                    const idx = prev.findIndex(m => m.id === assistantId);
                    if (idx < 0) return [...prev, marker];
                    return [...prev.slice(0, idx), marker, ...prev.slice(idx)];
                  });
                  console.info(
                    `[compact] summarized ${evt.summarized_messages} older messages, kept ${evt.kept_recent} recent`
                  );
                  break;
                }
                case 'done':
                  dur = evt.duration;
                  steps.forEach(s => s.status = 'completed');
                  setThinkingSteps([...steps]);
                  break;
              }
              continue;
            } catch {
              // Not JSON — treat as plain text (chitchat fallback)
              acc += t;
            }
          }

          // Update UI
          const now = Date.now();
          if (now - lastMsgUpdate >= MSG_UPDATE_MS) {
            lastMsgUpdate = now;
            setMessages(prev => prev.map(m =>
              m.id === assistantId ? {
                ...m, content: acc,
                thinkingSteps: [...steps],
                thinkingDuration: dur,
                vigencia: vig.length > 0 ? [...vig] : undefined,
                sources: srcs.length > 0 ? [...srcs] : undefined,
                sourceRefs: refs.length > 0 ? [...refs] : undefined,
                jurisprudencia: juris.length > 0 ? [...juris] : undefined,
              } : m
            ));
          }
        }

        // Process remaining buffer
        if (buf.trim()) {
          try {
            const evt = JSON.parse(buf.trim());
            if (evt.type === 'token') acc += evt.text;
            else if (evt.type === 'done') dur = evt.duration;
          } catch {
            acc += buf.trim();
          }
        }

        // Final update
        steps.forEach(s => s.status = 'completed');
        setMessages(prev => prev.map(m =>
          m.id === assistantId ? {
            ...m, content: acc.trim(),
            thinkingSteps: [...steps],
            thinkingDuration: dur,
            vigencia: vig.length > 0 ? [...vig] : undefined,
            sources: srcs.length > 0 ? [...srcs] : undefined,
            sourceRefs: refs.length > 0 ? [...refs] : undefined,
          } : m
        ));
        setThinkingSteps([]);
        refreshSessions();

      } catch (err) {
        console.error('Chat error:', err);
        toast.error(`Error: ${(err as Error).message}`);
        setMessages(prev => prev.map(m =>
          m.id === assistantId ? { ...m, content: `Error: ${(err as Error).message}` } : m
        ));
      } finally {
        setIsLoading(false);
        setStreamAbort(null);
      }
    },
    [sessionId, refreshSessions]
  );

  const handleNewChat = useCallback(async () => {
    try {
      const result = await resetSession();
      setSessionId(result.new_session_id);
    } catch { setSessionId(null); }
    setMessages([]);
    setThinkingSteps([]);
    toast.success('Nueva conversacion iniciada');
  }, []);

  const handleSelectSession = useCallback(async (id: string) => {
    try {
      setIsLoading(true);
      const data = await getSessionMessages(id);
      setSessionId(id);

      // Rebuild rich messages from persisted activity_metadata. For each
      // assistant row we walk the saved events and reconstruct the same
      // arrays the live stream would have produced (thinkingSteps,
      // vigencia, sourceRefs, jurisprudencia, clarify form). For
      // case_shift / compact events we splice synthetic SystemMarker
      // messages into the chat so the visual chronology matches what
      // the user saw originally.
      const rebuilt: Message[] = [];
      let lastCaseState: Record<string, unknown> | null = null;
      for (const m of data.messages) {
        const base: Message = {
          id: m.id,
          role: m.role,
          content: m.content,
          timestamp: m.timestamp ? fmtTime(new Date(m.timestamp)) : fmtTime(),
          sources: m.sources,
        };
        const am = m.activity_metadata;
        if (m.role === 'assistant' && am && Array.isArray(am.events)) {
          const steps: ThinkingStep[] = [];
          const vig: VigenciaItem[] = [];
          const refs: SourceRef[] = [];
          const juris: Record<string, unknown>[] = [];
          let sc = 0;
          for (const e of am.events) {
            switch (e.type) {
              case 'status':
                steps.push({ id: `r-s-${m.id}-${sc++}`, text: String(e.text || ''), status: 'completed', type: 'status', timestamp: Date.now() });
                break;
              case 'ingest':
                steps.push({ id: `r-i-${m.id}-${sc++}`, text: String(e.norm || ''), status: 'completed', type: 'ingest', timestamp: Date.now() });
                break;
              case 'vigencia':
                if (e.data) vig.push(e.data as VigenciaItem);
                break;
              case 'jurisprudencia':
                if (e.data) juris.push(e.data as Record<string, unknown>);
                break;
              case 'sourcerefs':
                if (Array.isArray(e.data)) refs.push(...(e.data as SourceRef[]));
                break;
              case 'clarify':
                base.clarifyQuestions = e.questions as ClarifyQuestion[];
                base.clarifyReason = String(e.reason || '');
                break;
              case 'casestate':
                lastCaseState = e.data as Record<string, unknown>;
                break;
              case 'case_shift':
                rebuilt.push({
                  id: `marker-shift-restored-${m.id}-${sc++}`,
                  role: 'assistant',
                  content: '',
                  systemMarker: {
                    kind: 'case_shift',
                    archivedIndex: e.archived_index as number,
                    newIndex: e.new_index as number,
                    reason: String(e.reason || ''),
                    confidence: e.confidence as number,
                  },
                });
                break;
              case 'compact':
                rebuilt.push({
                  id: `marker-compact-restored-${m.id}-${sc++}`,
                  role: 'assistant',
                  content: '',
                  systemMarker: {
                    kind: 'compact',
                    summarizedMessages: e.summarized_messages as number,
                    keptRecent: e.kept_recent as number,
                  },
                });
                break;
            }
          }
          base.thinkingSteps = steps;
          base.thinkingDuration = am.duration_seconds;
          base.vigencia = vig.length ? vig : undefined;
          base.sourceRefs = refs.length ? refs : undefined;
          base.jurisprudencia = juris.length ? juris : undefined;
          base.caseIndex = am.case_index;
          base.turnInCase = am.turn_in_case;
        }
        rebuilt.push(base);
      }

      setMessages(rebuilt);
      if (lastCaseState) setCaseState(lastCaseState);
      if (window.innerWidth < 768) setIsSidebarOpen(false);
    } catch (e) {
      toast.error(`No se pudo cargar la sesion: ${(e as Error).message}`);
    } finally { setIsLoading(false); }
  }, []);

  const handleDeleteSession = useCallback(async (id: string) => {
    try {
      await deleteSession(id);
      toast.success('Conversacion eliminada');
      if (id === sessionId) { setMessages([]); setSessionId(null); }
      refreshSessions();
    } catch (e) { toast.error(`Error al eliminar: ${(e as Error).message}`); }
  }, [sessionId, refreshSessions]);

  const handleShare = () => {
    if (!sessionId) { toast.info('Inicia una conversacion primero'); return; }
    navigator.clipboard.writeText(`${window.location.origin}/?session=${sessionId}`);
    toast.success('Enlace copiado');
  };

  const handleStop = () => {
    if (streamAbort) streamAbort();
    setIsLoading(false);
    toast.info('Generacion detenida');
  };

  const handleOpenActivity = useCallback((messageId: string) => {
    setActivityMessageId(messageId);
    setIsActivityOpen(true);
  }, []);

  const toggleDarkMode = () => setDarkMode(v => !v);
  const toggleSidebar = () => setIsSidebarOpen(v => !v);

  // Get activity data for selected message
  const activityMessage = activityMessageId
    ? messages.find(m => m.id === activityMessageId) : null;

  return (
    <div className="h-screen flex overflow-hidden bg-background">
      {/* Sidebar — fixed overlay, never disrupts flex layout */}
      {isSidebarOpen && (
        <>
          <div className="fixed inset-0 bg-black/30 z-40" onClick={() => setIsSidebarOpen(false)} />
          <div className="fixed left-0 top-0 h-full z-50">
            <Sidebar
              onNewChat={handleNewChat}
              darkMode={darkMode}
              onToggleDarkMode={toggleDarkMode}
              onClose={() => setIsSidebarOpen(false)}
              sessions={sessions}
              activeSessionId={sessionId}
              onSelectSession={handleSelectSession}
              onDeleteSession={handleDeleteSession}
              onOpenKnowledge={() => setIsKnowledgeOpen(true)}
            />
          </div>
        </>
      )}

      {/* Main area — always full width, never affected by sidebar */}
      <div className="flex-1 flex flex-col overflow-hidden w-full">
        <Navbar
          title={messages.length > 0 ? messages[0]?.content.slice(0, 50) + '...' : 'Nueva Conversacion'}
          onShare={handleShare}
          onSettings={() => setIsSettingsOpen(true)}
          onToggleSidebar={toggleSidebar}
          isSidebarOpen={isSidebarOpen}
          onOpenKnowledge={() => setIsKnowledgeOpen(true)}
        />

        <SessionStats caseState={caseState} />

        <ChatArea
          messages={messages}
          isLoading={isLoading}
          thinkingSteps={thinkingSteps}
          onOpenActivity={handleOpenActivity}
          onSendClarifyAnswer={handleSendMessage}
        />

        <MessageInput
          onSend={handleSendMessage}
          isLoading={isLoading}
          onStop={handleStop}
          isEmpty={messages.length === 0}
          sessionId={sessionId}
        />
      </div>

      {/* Panels */}
      {isSettingsOpen && (
        <SettingsPanel isOpen={isSettingsOpen} onClose={() => setIsSettingsOpen(false)}
                       darkMode={darkMode} onToggleDarkMode={toggleDarkMode} />
      )}
      {isKnowledgeOpen && (
        <KnowledgePanel isOpen={isKnowledgeOpen} onClose={() => setIsKnowledgeOpen(false)} />
      )}

      {/* Activity Panel - right sidebar */}
      <ActivityPanel
        isOpen={isActivityOpen}
        onClose={() => setIsActivityOpen(false)}
        steps={activityMessage?.thinkingSteps || []}
        duration={activityMessage?.thinkingDuration}
        vigencia={activityMessage?.vigencia}
        sources={activityMessage?.sources}
        sourceRefs={activityMessage?.sourceRefs}
        jurisprudencia={activityMessage?.jurisprudencia}
        caseIndex={activityMessage?.caseIndex}
        turnInCase={activityMessage?.turnInCase}
      />

      <Toaster position="top-center" />
    </div>
  );
}
