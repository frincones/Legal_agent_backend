import { User, Copy, ThumbsUp, ThumbsDown, RotateCcw, Circle, Clock } from 'lucide-react';
import { Button } from './ui/button';
import { motion } from 'motion/react';
import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Message } from '../App';
import ClarifyForm from './ClarifyForm';

interface ChatMessageProps {
  message: Message;
  onOpenActivity?: (messageId: string) => void;
  onSendClarifyAnswer?: (formattedAnswer: string) => void;
}

function SystemMarkerView({ marker }: { marker: NonNullable<Message['systemMarker']> }) {
  if (marker.kind === 'case_shift') {
    return (
      <div className="my-4 flex items-center gap-3" title={marker.reason || ''}>
        <div className="flex-1 h-px bg-amber-200/60 dark:bg-amber-700/40" />
        <span className="text-[11px] font-medium text-amber-700 dark:text-amber-400 px-2 py-0.5 rounded bg-amber-50 dark:bg-amber-950/30 border border-amber-200/60 dark:border-amber-700/40">
          Caso {marker.archivedIndex} archivado · Inicia caso {marker.newIndex}
        </span>
        <div className="flex-1 h-px bg-amber-200/60 dark:bg-amber-700/40" />
      </div>
    );
  }
  // compact
  return (
    <div className="my-4 flex items-center gap-3" title="El historial fue resumido automáticamente para mantener el contexto manejable.">
      <div className="flex-1 h-px bg-blue-200/60 dark:bg-blue-700/40" />
      <span className="text-[11px] font-medium text-blue-700 dark:text-blue-400 px-2 py-0.5 rounded bg-blue-50 dark:bg-blue-950/30 border border-blue-200/60 dark:border-blue-700/40">
        ✻ {marker.summarizedMessages ?? 0} mensajes previos resumidos automáticamente
      </span>
      <div className="flex-1 h-px bg-blue-200/60 dark:bg-blue-700/40" />
    </div>
  );
}

export default function ChatMessage({ message, onOpenActivity, onSendClarifyAnswer }: ChatMessageProps) {
  const [copied, setCopied] = useState(false);
  const { role, content, thinkingSteps, thinkingDuration, vigencia, clarifyQuestions, clarifyReason, systemMarker } = message;

  // Inline system markers (case_shift / compact) render as a divider
  // instead of the normal chat bubble — same Message shape so ChatArea
  // doesn't need to special-case the iteration.
  if (systemMarker) {
    return <SystemMarkerView marker={systemMarker} />;
  }

  const handleCopy = () => {
    navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const isUser = role === 'user';
  const hasActivity = !isUser && thinkingSteps && thinkingSteps.length > 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25, ease: 'easeOut' }}
      className="group py-3 sm:py-4 border-b border-border/20 last:border-0"
    >
      <div className="flex gap-2 sm:gap-3 items-start">
        {/* Avatar */}
        <div className={`flex-shrink-0 w-5 h-5 sm:w-6 sm:h-6 rounded-full flex items-center justify-center mt-0.5
          ${isUser ? 'bg-muted text-foreground' : 'bg-foreground text-background'}`}>
          {isUser
            ? <User className="w-3 h-3 sm:w-3.5 sm:h-3.5" />
            : <Circle className="w-3 h-3 sm:w-3.5 sm:h-3.5 fill-current" />}
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0 space-y-2">
          {/* "Pensó durante Xs" — clickable link to activity panel */}
          {hasActivity && thinkingDuration !== undefined && (
            <button
              onClick={() => onOpenActivity?.(message.id)}
              className="flex items-center gap-1.5 text-[11px] sm:text-xs text-muted-foreground hover:text-foreground transition-colors rounded-md hover:bg-muted/50 px-1.5 py-0.5 -ml-1.5"
            >
              <Clock className="w-3 h-3" />
              <span>Penso durante {thinkingDuration}s</span>
              <span className="text-[10px]">&rsaquo;</span>
            </button>
          )}

          {/* Vigencia badges inline */}
          {vigencia && vigencia.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {vigencia.map((v, i) => (
                <span key={i} className={`inline-flex items-center gap-1 text-[10px] sm:text-[11px] font-medium px-1.5 py-0.5 rounded
                  ${v.estado === 'VIGENTE' ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400' :
                    v.estado === 'DEROGADA' ? 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-400' :
                    'bg-amber-100 dark:bg-amber-900/30 text-amber-700 dark:text-amber-400'}`}>
                  {v.estado === 'VIGENTE' ? '✅' : v.estado === 'DEROGADA' ? '❌' : '⚠️'}
                  {v.tipo} {v.numero}/{v.anio}
                </span>
              ))}
            </div>
          )}

          {/* Inline clarify questionnaire — agent paused waiting for user input */}
          {!isUser && clarifyQuestions && clarifyQuestions.length > 0 && (
            <ClarifyForm
              questions={clarifyQuestions}
              reason={clarifyReason}
              onSubmit={(formatted) => onSendClarifyAnswer?.(formatted)}
            />
          )}

          {/* Message text — react-markdown rendering */}
          {content && (
            <div className="text-[13px] sm:text-sm text-foreground/90 leading-relaxed">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  h1: ({children}) => <h3 className="text-base font-semibold text-foreground mt-5 mb-1 pt-3 border-t border-border/30 first:mt-0 first:border-0 first:pt-0">{children}</h3>,
                  h2: ({children}) => <h3 className="text-base font-semibold text-foreground mt-5 mb-1 pt-3 border-t border-border/30">{children}</h3>,
                  h3: ({children}) => <h4 className="text-sm font-semibold text-foreground mt-4 mb-1">{children}</h4>,
                  p: ({children}) => <p className="my-2 break-words leading-relaxed">{children}</p>,
                  strong: ({children}) => <span className="font-semibold text-foreground">{children}</span>,
                  ol: ({children}) => <ol className="my-2 ml-5 space-y-1.5 list-decimal">{children}</ol>,
                  ul: ({children}) => <ul className="my-2 ml-5 space-y-1">{children}</ul>,
                  li: ({children}) => <li className="break-words">{children}</li>,
                  a: ({href, children}) => <a href={href} target="_blank" rel="noopener noreferrer" className="text-blue-600 dark:text-blue-400 underline">{children}</a>,
                  blockquote: ({children}) => <blockquote className="border-l-2 border-border pl-3 my-2 text-muted-foreground italic">{children}</blockquote>,
                  hr: () => <hr className="my-3 border-border/30" />,
                }}
              >
                {content}
              </ReactMarkdown>
            </div>
          )}

          {/* Actions */}
          {!isUser && content && (
            <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
              <Button variant="ghost" size="icon" onClick={handleCopy} className="h-6 w-6 rounded-md" title={copied ? 'Copiado!' : 'Copiar'}>
                <Copy className="w-3 h-3" />
              </Button>
              <Button variant="ghost" size="icon" className="h-6 w-6 rounded-md"><ThumbsUp className="w-3 h-3" /></Button>
              <Button variant="ghost" size="icon" className="h-6 w-6 rounded-md"><ThumbsDown className="w-3 h-3" /></Button>
              <Button variant="ghost" size="icon" className="h-6 w-6 rounded-md"><RotateCcw className="w-3 h-3" /></Button>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}

// Keep named export for backward compat
export { ChatMessage };
