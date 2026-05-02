import { Search, Plus, X, MessageSquare, Clock, Trash2, Moon, Sun, BookOpen } from 'lucide-react';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { ScrollArea } from './ui/scroll-area';
import { useState } from 'react';
import { motion } from 'motion/react';
import type { SessionItem } from '../../lib/api';

interface SidebarProps {
  onNewChat?: () => void;
  darkMode?: boolean;
  onToggleDarkMode?: () => void;
  onClose?: () => void;
  sessions?: SessionItem[];
  activeSessionId?: string | null;
  onSelectSession?: (id: string) => void;
  onDeleteSession?: (id: string) => void;
  onOpenKnowledge?: () => void;
}

function fmtRelative(ts: string | null): string {
  if (!ts) return '';
  const date = new Date(ts);
  const now = Date.now();
  const diffMs = now - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'ahora';
  if (diffMin < 60) return `${diffMin}m`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 7) return `${diffDay}d`;
  const diffWk = Math.floor(diffDay / 7);
  return `${diffWk}sem`;
}

export function Sidebar({
  onNewChat,
  darkMode,
  onToggleDarkMode,
  onClose,
  sessions = [],
  activeSessionId,
  onSelectSession,
  onDeleteSession,
  onOpenKnowledge,
}: SidebarProps) {
  const [searchQuery, setSearchQuery] = useState('');

  const filtered = sessions.filter((s) =>
    s.title.toLowerCase().includes(searchQuery.toLowerCase())
  );

  return (
    <>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
        className="fixed inset-0 bg-black/20 z-40 md:hidden"
      />

      <motion.aside
        initial={{ x: -300 }}
        animate={{ x: 0 }}
        exit={{ x: -300 }}
        transition={{ duration: 0.3, ease: 'easeOut' }}
        className="w-60 h-screen bg-sidebar border-r border-sidebar-border flex flex-col"
      >
        {/* Header */}
        <div className="p-3 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-sidebar-foreground px-1">
              Agent RAG
            </span>
            <div className="flex items-center gap-1">
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={onToggleDarkMode}
                title={darkMode ? 'Modo claro' : 'Modo oscuro'}
              >
                {darkMode ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 md:hidden"
                onClick={onClose}
              >
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>

          <Button
            onClick={onNewChat}
            className="w-full bg-foreground hover:bg-foreground/90 text-background text-xs h-8"
          >
            <Plus className="w-3.5 h-3.5 mr-1.5" />
            Nueva conversación
          </Button>

          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
            <Input
              placeholder="Buscar..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-8 bg-sidebar-accent border-sidebar-border h-8 text-xs"
            />
          </div>
        </div>

        {/* Sessions list */}
        <ScrollArea className="flex-1 px-2">
          <div className="space-y-1 py-2">
            {filtered.length === 0 && (
              <p className="px-2 py-4 text-[11px] text-muted-foreground text-center">
                No hay conversaciones aún.
                <br />
                Empieza una nueva.
              </p>
            )}

            {filtered.length > 0 && (
              <h3 className="px-2 text-[10px] font-medium text-muted-foreground mb-1">
                CONVERSACIONES
              </h3>
            )}

            {filtered.map((s) => (
              <SessionItemComponent
                key={s.session_id}
                session={s}
                isActive={activeSessionId === s.session_id}
                onClick={() => onSelectSession?.(s.session_id)}
                onDelete={() => onDeleteSession?.(s.session_id)}
              />
            ))}
          </div>
        </ScrollArea>

        {/* Footer */}
        <div className="p-2 border-t border-sidebar-border">
          <Button
            variant="ghost"
            className="w-full justify-start text-sidebar-foreground hover:bg-sidebar-accent h-8 text-xs"
            onClick={onOpenKnowledge}
          >
            <BookOpen className="w-3.5 h-3.5 mr-2" />
            Base de conocimiento
          </Button>
        </div>
      </motion.aside>
    </>
  );
}

interface SessionItemProps {
  session: SessionItem;
  isActive: boolean;
  onClick: () => void;
  onDelete: () => void;
}

function SessionItemComponent({ session, isActive, onClick, onDelete }: SessionItemProps) {
  return (
    <motion.div
      whileHover={{ scale: 1.01 }}
      whileTap={{ scale: 0.99 }}
      className={`
        group w-full px-2 py-2 rounded-md transition-all duration-200 cursor-pointer
        ${isActive
          ? 'bg-sidebar-accent text-sidebar-accent-foreground'
          : 'text-sidebar-foreground hover:bg-sidebar-accent/50'
        }
      `}
      onClick={onClick}
    >
      <div className="flex items-start gap-2">
        <MessageSquare
          className={`w-3.5 h-3.5 mt-0.5 flex-shrink-0 ${
            isActive ? 'text-foreground' : 'text-muted-foreground'
          }`}
        />
        <div className="flex-1 min-w-0">
          <p className="text-xs font-normal truncate">{session.title}</p>
          <p className="text-[10px] text-muted-foreground mt-0.5 flex items-center gap-1">
            <Clock className="w-2.5 h-2.5" />
            {fmtRelative(session.last_message_at)} · {session.message_count} msg
          </p>
        </div>
        <button
          className="opacity-0 group-hover:opacity-100 transition-opacity"
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          title="Eliminar"
        >
          <Trash2 className="w-3 h-3 text-muted-foreground hover:text-destructive" />
        </button>
      </div>
    </motion.div>
  );
}
