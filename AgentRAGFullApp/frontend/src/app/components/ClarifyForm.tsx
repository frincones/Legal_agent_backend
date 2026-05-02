import { useState } from 'react';
import { HelpCircle, Send } from 'lucide-react';
import { Button } from './ui/button';
import type { ClarifyQuestion } from '../App';

interface ClarifyFormProps {
  questions: ClarifyQuestion[];
  reason?: string;
  onSubmit: (formattedAnswer: string) => void;
}

/**
 * Renders the inline questionnaire the agent emits when it needs more
 * context (Phase 1.5 clarify gate). Builds a "Pregunta: respuesta\n..."
 * string that the parent forwards as the next user message — the backend
 * has no special endpoint for clarify answers, the case_state's regular
 * fact extraction absorbs them naturally on the next turn.
 */
export default function ClarifyForm({ questions, reason, onSubmit }: ClarifyFormProps) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);

  const allAnswered = questions.every(q => (answers[q.id] || '').trim().length > 0);

  const handleSubmit = () => {
    if (!allAnswered || submitted) return;
    const lines = questions.map(q => `${q.label} ${answers[q.id].trim()}`);
    setSubmitted(true);
    onSubmit(lines.join('\n'));
  };

  return (
    <div className="my-2 p-3 rounded-lg border border-amber-200 dark:border-amber-900/50 bg-amber-50/50 dark:bg-amber-950/20">
      <div className="flex items-start gap-2 mb-3">
        <HelpCircle className="w-4 h-4 text-amber-600 dark:text-amber-400 mt-0.5 flex-shrink-0" />
        <p className="text-xs text-foreground/80 leading-relaxed">
          {reason || 'Necesito un poco más de contexto para darte un dictamen preciso:'}
        </p>
      </div>

      <div className="space-y-3">
        {questions.map(q => (
          <div key={q.id} className="space-y-1.5">
            <label className="text-xs font-medium text-foreground/90">{q.label}</label>
            {q.type === 'radio' && q.options ? (
              <div className="flex flex-wrap gap-1.5">
                {q.options.map(opt => {
                  const selected = answers[q.id] === opt;
                  return (
                    <button
                      key={opt}
                      type="button"
                      disabled={submitted}
                      onClick={() => setAnswers(a => ({ ...a, [q.id]: opt }))}
                      className={`text-[11px] px-2.5 py-1 rounded-md border transition-colors
                        ${selected
                          ? 'bg-foreground text-background border-foreground'
                          : 'border-border hover:border-foreground/40 bg-background text-foreground/80'}
                        disabled:opacity-60 disabled:cursor-not-allowed`}
                    >
                      {opt}
                    </button>
                  );
                })}
              </div>
            ) : (
              <input
                type="text"
                value={answers[q.id] || ''}
                disabled={submitted}
                onChange={e => setAnswers(a => ({ ...a, [q.id]: e.target.value }))}
                placeholder="Escribe tu respuesta..."
                className="w-full text-xs px-2.5 py-1.5 rounded-md border border-border bg-background
                  focus:outline-none focus:ring-1 focus:ring-foreground/30 disabled:opacity-60"
              />
            )}
          </div>
        ))}
      </div>

      <div className="mt-3 flex items-center justify-between gap-2">
        <span className="text-[10px] text-muted-foreground">
          {submitted ? 'Respuestas enviadas' : `${Object.keys(answers).length}/${questions.length} respondidas`}
        </span>
        <Button
          size="sm"
          onClick={handleSubmit}
          disabled={!allAnswered || submitted}
          className="h-7 text-xs gap-1.5"
        >
          <Send className="w-3 h-3" />
          {submitted ? 'Enviado' : 'Enviar respuestas'}
        </Button>
      </div>
    </div>
  );
}
