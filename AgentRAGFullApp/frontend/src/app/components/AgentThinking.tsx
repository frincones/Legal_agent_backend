import { motion, AnimatePresence } from 'motion/react';
import { Loader2, Download, Search, Scale, Brain, Clock } from 'lucide-react';

export interface ThinkingStep {
  id: string;
  text: string;
  status: 'active' | 'completed';
  type: 'status' | 'ingest';
  timestamp: number;
}

interface AgentThinkingProps {
  steps: ThinkingStep[];
  isThinking: boolean;
  duration?: number;
  onOpenActivity?: () => void;
}

function getIcon(text: string, type: string) {
  if (type === 'ingest') return <Download className="w-3.5 h-3.5 text-blue-500 animate-bounce" />;
  const t = text.toLowerCase();
  if (t.includes('busca') || t.includes('consult')) return <Search className="w-3.5 h-3.5 text-amber-500 animate-pulse" />;
  if (t.includes('vigencia')) return <Scale className="w-3.5 h-3.5 text-purple-500 animate-pulse" />;
  if (t.includes('genera')) return <Brain className="w-3.5 h-3.5 text-pink-500 animate-pulse" />;
  return <Loader2 className="w-3.5 h-3.5 text-muted-foreground animate-spin" />;
}

export default function AgentThinking({ steps, isThinking, duration, onOpenActivity }: AgentThinkingProps) {
  if (steps.length === 0 && !isThinking) return null;

  // Done thinking — collapsed summary
  if (!isThinking && duration !== undefined) {
    return (
      <motion.button
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        onClick={onOpenActivity}
        className="flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors py-1.5 px-2 -ml-2 rounded-md hover:bg-muted/50"
      >
        <Clock className="w-3 h-3" />
        <span>Penso durante {duration}s</span>
        <span className="text-[10px] ml-0.5 opacity-60">&rsaquo;</span>
      </motion.button>
    );
  }

  // Get the LAST (current) step to show
  const currentStep = steps.length > 0 ? steps[steps.length - 1] : null;
  const displayText = currentStep
    ? (currentStep.type === 'ingest' ? `Descargando: ${currentStep.text}` : currentStep.text)
    : 'Analizando consulta...';
  const stepCount = steps.length;

  // Single animated line that updates as steps progress
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="py-2 flex items-center gap-2"
    >
      {/* Animated icon */}
      {currentStep ? getIcon(currentStep.text, currentStep.type) : (
        <Loader2 className="w-3.5 h-3.5 text-muted-foreground animate-spin" />
      )}

      {/* Single line — current step text with crossfade */}
      <AnimatePresence mode="wait">
        <motion.span
          key={currentStep?.id || 'init'}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.15 }}
          className={`text-xs ${
            currentStep?.type === 'ingest'
              ? 'text-blue-600 dark:text-blue-400 font-medium'
              : 'text-muted-foreground'
          }`}
        >
          {displayText}
        </motion.span>
      </AnimatePresence>

      {/* Step counter */}
      {stepCount > 1 && (
        <span className="text-[10px] text-muted-foreground/50 ml-auto tabular-nums">
          {stepCount}
        </span>
      )}

      {/* Trailing dots */}
      <div className="flex gap-0.5 ml-1">
        {[0, 1, 2].map(i => (
          <motion.div
            key={i}
            animate={{ opacity: [0.2, 0.8, 0.2] }}
            transition={{ repeat: Infinity, duration: 1.2, delay: i * 0.15 }}
            className="w-1 h-1 rounded-full bg-muted-foreground"
          />
        ))}
      </div>
    </motion.div>
  );
}
